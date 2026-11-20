"""Materialize verified offline scopes under shared ledger ancestors, atomically."""

import json

from .capabilities import CapabilityError, contract, encoded, node_ids

SCHEMA = """
CREATE TABLE IF NOT EXISTS capability_epochs (visa_id TEXT PRIMARY KEY REFERENCES visas(id), epoch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS capability_roots (id TEXT PRIMARY KEY, root TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capability_nodes (visa_id TEXT PRIMARY KEY REFERENCES visas(id), root_id TEXT NOT NULL REFERENCES capability_roots(id));
CREATE TABLE IF NOT EXISTS capability_requests (principal TEXT NOT NULL, request_id TEXT NOT NULL,
    root_id TEXT NOT NULL REFERENCES capability_roots(id), PRIMARY KEY(principal,request_id));
"""


def is_node(db, visa_id):
    return (
        db.execute("SELECT 1 FROM capability_nodes WHERE visa_id=?", (visa_id,)).fetchone()
        is not None
    )


def root_binding(gateway, db, root):
    stored = db.execute("SELECT root FROM capability_roots WHERE id=?", (root["id"],)).fetchone()
    epoch = db.execute(
        "SELECT epoch FROM capability_epochs WHERE visa_id=?", (root["visa_id"],)
    ).fetchone()
    if (
        stored is None
        or stored["root"] != encoded(root).decode()
        or epoch is None
        or epoch[0] != root["epoch"]
    ):
        raise CapabilityError("capability root is unknown or invalidated")
    chain = gateway._chain(db, root["visa_id"])
    if chain[0][1].dictionary() != root["contract"]:
        raise CapabilityError("capability source contract changed")
    for depth, (row, scope) in enumerate(chain):
        if (
            row["status"] != "active"
            or int(gateway.clock()) >= scope.expires_at
            or depth > scope.delegation_depth
        ):
            raise CapabilityError("capability ancestor is inactive, expired or too deep")
    return chain


def request_binding(gateway, db, principal, request_id):
    row = db.execute(
        "SELECT root_id FROM capability_requests WHERE principal=? AND request_id=?",
        (principal, request_id),
    ).fetchone()
    if row:
        root = json.loads(
            db.execute("SELECT root FROM capability_roots WHERE id=?", (row[0],)).fetchone()[0]
        )
        root_binding(gateway, db, root)
    return row[0] if row else None


def materialize(gateway, db, verified, action):
    data, root = verified.data, verified.data["root"]
    if action.visa_id != verified.visa_id:
        raise CapabilityError("action does not match capability scope")
    source_chain = root_binding(gateway, db, root)
    contracts = [contract(root["contract"])] + [contract(c["contract"]) for c in data["caveats"]]
    ids = node_ids(data)
    for depth, (_, scope) in enumerate(source_chain):
        if depth + len(data["caveats"]) > scope.delegation_depth:
            raise CapabilityError("capability exceeds current ancestor depth")
    for index, scope in enumerate(contracts):
        if int(gateway.clock()) >= scope.expires_at:
            raise CapabilityError("capability expired")
        if index:
            row = db.execute(
                "SELECT parent,contract,status FROM visas WHERE id=?", (ids[index],)
            ).fetchone()
            if row is None:
                gateway._insert(db, scope, ids[index - 1], visa_id=ids[index])
                db.execute("INSERT INTO capability_nodes VALUES (?,?)", (ids[index], root["id"]))
            elif (
                not is_node(db, ids[index])
                or row["parent"] != ids[index - 1]
                or json.loads(row["contract"]) != scope.dictionary()
                or row["status"] != "active"
            ):
                raise CapabilityError("capability scope is revoked or mismatched")
    prior = request_binding(gateway, db, verified.principal, action.request_id)
    if prior is not None and prior != root["id"]:
        raise CapabilityError("request belongs to another capability root")
    if prior is None:
        if db.execute(
            "SELECT 1 FROM requests WHERE principal=? AND id=?",
            (verified.principal, action.request_id),
        ).fetchone():
            raise CapabilityError("cannot rebind an existing credential request")
        db.execute(
            "INSERT INTO capability_requests VALUES (?,?,?)",
            (verified.principal, action.request_id, root["id"]),
        )


def issue(gateway, visa_id, key_id):
    if gateway.capabilities is None:
        raise CapabilityError("capabilities are not enabled")
    with gateway.transaction() as db:
        if is_node(db, visa_id):
            raise CapabilityError(
                "mint from an ordinary source visa; attenuate token scopes offline"
            )
        chain = gateway._chain(db, visa_id)
        for depth, (row, scope) in enumerate(chain):
            if (
                row["status"] != "active"
                or int(gateway.clock()) >= scope.expires_at
                or depth > scope.delegation_depth
            ):
                raise CapabilityError("cannot mint from inactive authority")
        db.execute("INSERT OR IGNORE INTO capability_epochs VALUES (?,0)", (visa_id,))
        epoch = db.execute(
            "SELECT epoch FROM capability_epochs WHERE visa_id=?", (visa_id,)
        ).fetchone()[0]
        token = gateway.capabilities.mint(key_id, visa_id, epoch, chain[0][1])
        root = gateway.capabilities.verify(token).data["root"]
        db.execute(
            "INSERT INTO capability_roots VALUES (?,?)", (root["id"], encoded(root).decode())
        )
        gateway._audit(
            db,
            {
                "event": "capability_issued",
                "visa_id": visa_id,
                "root_id": root["id"],
                "key_id": key_id,
            },
        )
        return {
            "token": token,
            "visa_id": visa_id,
            "root_id": root["id"],
            "contract": root["contract"],
        }
