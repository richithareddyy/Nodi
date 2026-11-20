"""Bounded operator reads using keyset pagination and a captured upper bound."""

import json

from .gateway import Gateway
from .outbox_store import reserved

MAX_CURSOR = 2**63 - 1


class ReadModel:
    def __init__(self, gateway: Gateway):
        self.gateway = gateway
        # Idempotent additive indexes work with existing milestone databases.
        with gateway.transaction() as db:
            db.execute("CREATE INDEX IF NOT EXISTS requests_approval ON requests(approval)")
            db.execute(
                (
                    "CREATE INDEX IF NOT EXISTS visas_agent ON "
                    "visas(json_extract(contract, '$.agent_id'))"
                )
            )
            db.execute(
                (
                    "CREATE INDEX IF NOT EXISTS audit_agent ON "
                    "audit(json_extract(event, '$.agent'), sequence)"
                )
            )
            db.execute(
                (
                    "CREATE INDEX IF NOT EXISTS audit_decision ON "
                    "audit(json_extract(event, '$.decision'), sequence)"
                )
            )

    @staticmethod
    def bounds(after, limit, through):
        if (
            type(after) is not int
            or not 0 <= after <= MAX_CURSOR
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("after must be a nonnegative 64-bit integer and limit must be 1..100")
        if through is not None and (type(through) is not int or not 0 <= through <= MAX_CURSOR):
            raise ValueError("through must be a nonnegative 64-bit integer")

    @staticmethod
    def page(rows, limit, through, decode):
        has_more = len(rows) > limit
        selected = rows[:limit]
        return {
            "items": [decode(row) for row in selected],
            "has_more": has_more,
            "next_after": selected[-1]["cursor"] if has_more else None,
            "through": through,
        }

    def audit(
        self, after=0, limit=25, through=None, agent=None, visa=None, decision=None, blocked=False
    ):
        self.bounds(after, limit, through)
        if decision is not None and decision not in ("ALLOW", "DENY", "ASK_HUMAN"):
            raise ValueError("invalid decision filter")
        where, values = [], []
        for value, expression in (
            (agent, "json_extract(event, '$.agent')"),
            (visa, "json_extract(event, '$.visa_id')"),
            (decision, "json_extract(event, '$.decision')"),
        ):
            if value is not None:
                where.append(expression + " = ?")
                values.append(value)
        if blocked:
            where.append(
                (
                    "(json_extract(event, '$.decision') = 'DENY' OR "
                    "json_extract(event, '$.event') = 'delegation_denied')"
                )
            )
        with self.gateway.read_transaction() as db:
            high = (
                db.execute("SELECT COALESCE(MAX(sequence),0) FROM audit").fetchone()[0]
                if through is None
                else through
            )
            sql = "SELECT sequence AS cursor, * FROM audit WHERE sequence > ? AND sequence <= ?"
            if where:
                sql += " AND " + " AND ".join(where)
            rows = db.execute(
                sql + " ORDER BY sequence LIMIT ?", [after, high, *values, limit + 1]
            ).fetchall()
            return self.page(
                rows, limit, high, lambda r: {**dict(r), "event": json.loads(r["event"])}
            )

    def checkpoint(self):
        with self.gateway.read_transaction() as db:
            row = db.execute(
                "SELECT sequence, event_hash FROM audit ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            return {
                "version": 1,
                "sequence": row["sequence"] if row else 0,
                "event_hash": row["event_hash"] if row else "0" * 64,
            }

    def visas(self, after=0, limit=25, through=None, agent=None):
        self.bounds(after, limit, through)
        with self.gateway.read_transaction() as db:
            high = (
                db.execute("SELECT COALESCE(MAX(rowid),0) FROM visas").fetchone()[0]
                if through is None
                else through
            )
            sql = "SELECT rowid AS cursor, * FROM visas WHERE rowid > ? AND rowid <= ?"
            values = [after, high]
            if agent is not None:
                sql += " AND json_extract(contract, '$.agent_id') = ?"
                values.append(agent)
            rows = db.execute(sql + " ORDER BY rowid LIMIT ?", [*values, limit + 1]).fetchall()
            return self.page(
                rows, limit, high, lambda r: {**dict(r), "contract": json.loads(r["contract"])}
            )

    def request_status(self, principal, request_id, *, capability=None):
        with self.gateway.read_transaction() as db:
            if capability is not None:
                from .capabilities import CapabilityError
                from .capability_store import request_binding, root_binding

                root_binding(self.gateway, db, capability.data["root"])
                root_id = request_binding(self.gateway, db, principal, request_id)
                if root_id != capability.data["root"]["id"]:
                    raise LookupError("request not found")
                for item in capability.data["caveats"]:
                    if int(self.gateway.clock()) >= item["contract"]["expires_at"]:
                        raise CapabilityError("capability expired")
                if (
                    db.execute("SELECT 1 FROM visas WHERE id=?", (capability.visa_id,)).fetchone()
                    is None
                ):
                    raise LookupError("request not found")
                for depth, (node, scope) in enumerate(self.gateway._chain(db, capability.visa_id)):
                    if (
                        node["status"] != "active"
                        or int(self.gateway.clock()) >= scope.expires_at
                        or depth > scope.delegation_depth
                    ):
                        raise CapabilityError("capability inactive")
            row = db.execute(
                (
                    "SELECT fingerprint,response,approval,deadline FROM requests "
                    "WHERE principal=? AND id=?"
                ),
                (principal, request_id),
            ).fetchone()
            if row is None or (
                capability is not None
                and json.loads(row["response"])["visa_id"] != capability.visa_id
            ):
                raise LookupError("request not found")
            now = int(self.gateway.clock())
            outstanding = row["approval"] in ("pending", "approved")
            return {
                "request_id": request_id,
                "fingerprint": row["fingerprint"],
                "state": row["approval"],
                "last_response": json.loads(row["response"]),
                "review_deadline": row["deadline"] if outstanding else None,
                "review_expired": outstanding and now >= row["deadline"],
                "server_time": now,
                "scope": "stored request snapshot; not a new execution or authorization",
            }

    def inspect_visa(self, visa_id):
        with self.gateway.read_transaction() as db:
            if db.execute("SELECT 1 FROM visas WHERE id=?", (visa_id,)).fetchone() is None:
                raise LookupError("visa not found")
            chain = self.gateway._chain(db, visa_id)
            now = int(self.gateway.clock())
            nodes, blockers = [], []
            for depth, (row, contract) in enumerate(chain):
                money, actions = reserved(db, row["id"])
                node = {
                    **dict(row),
                    "contract": contract.dictionary(),
                    "depth_from_leaf": depth,
                    "reserved_cents": money,
                    "reserved_actions": actions,
                    "remaining_budget_cents": max(
                        0, contract.spending_limit - row["spent"] - money
                    ),
                    "remaining_actions": max(0, contract.max_actions - row["actions"] - actions),
                    "expired": now >= contract.expires_at,
                }
                nodes.append(node)
                for failed, reason in (
                    (row["status"] != "active", "inactive"),
                    (node["expired"], "expired"),
                    (depth > contract.delegation_depth, "delegation_depth"),
                    (node["remaining_actions"] == 0, "action_limit"),
                ):
                    if failed:
                        blockers.append({"visa_id": row["id"], "reason": reason})
            contracts = [contract for _, contract in chain]

            def permitted(allowed, forbidden):
                common = set(getattr(contracts[0], allowed))
                excluded = set()
                for contract in contracts:
                    common.intersection_update(getattr(contract, allowed))
                    excluded.update(getattr(contract, forbidden))
                return sorted(common - excluded)

            return {
                "visa_id": visa_id,
                "server_time": now,
                "chain": nodes,
                "blockers": blockers,
                "effective_limits": {
                    "remaining_budget_cents": min(node["remaining_budget_cents"] for node in nodes),
                    "remaining_actions": min(node["remaining_actions"] for node in nodes),
                    "maximum_transaction_cents": min(c.maximum_transaction for c in contracts),
                    "approval_threshold_cents": min(c.approval_threshold for c in contracts),
                    "expires_at": min(c.expires_at for c in contracts),
                    "allowed_tools": permitted("allowed_tools", "forbidden_operations"),
                    "allowed_resources": permitted("allowed_resources", "forbidden_resources"),
                },
                "scope": (
                    "read-only snapshot, not authorization; shared usage can "
                    "change and pending reviews reserve no budget"
                ),
            }

    @staticmethod
    def review(row, now):
        action, response, contract = (
            json.loads(row[key]) for key in ("action", "response", "contract")
        )
        return {
            "agent": row["principal"],
            "request_id": row["id"],
            "fingerprint": row["fingerprint"],
            "visa_id": action["visa_id"],
            "mission": contract["mission"],
            "visa_status": row["visa_status"],
            "approval_status": row["approval"],
            "deadline": row["deadline"],
            "expired": now >= row["deadline"],
            "action": action,
            "cost_cents": response.get("cost_cents"),
            "impact": response.get("estimated_impact"),
            "reason": response["reason"],
            "intent": response.get("intent"),
            "policy_checks": response.get("policy_checks", []),
        }

    def approvals(self, after=0, limit=25, through=None, agent=None, include_expired=False):
        self.bounds(after, limit, through)
        with self.gateway.read_transaction() as db:
            now = int(self.gateway.clock())
            high = (
                db.execute("SELECT COALESCE(MAX(rowid),0) FROM requests").fetchone()[0]
                if through is None
                else through
            )
            sql = """SELECT r.rowid AS cursor, r.*, v.contract, v.status AS visa_status
                     FROM requests r JOIN visas v ON v.id = json_extract(r.action, '$.visa_id')
                     WHERE r.rowid > ? AND r.rowid <= ? AND r.approval IN ('pending','approved')"""
            values = [after, high]
            if agent is not None:
                sql += " AND r.principal = ?"
                values.append(agent)
            if not include_expired:
                sql += " AND r.deadline > ?"
                values.append(now)
            rows = db.execute(sql + " ORDER BY r.rowid LIMIT ?", [*values, limit + 1]).fetchall()
            result = self.page(
                rows, limit, high, lambda r: {"cursor": r["cursor"], **self.review(r, now)}
            )
            return {**result, "server_time": now}

    def inspect(self, agent, request_id):
        with self.gateway.read_transaction() as db:
            row = db.execute(
                """SELECT r.*, v.contract, v.status AS visa_status
                FROM requests r JOIN visas v ON v.id = json_extract(r.action, '$.visa_id')
                WHERE r.principal=? AND r.id=? AND r.approval IN ('pending','approved')""",
                (agent, request_id),
            ).fetchone()
            if row is None:
                raise LookupError("outstanding approval not found")
            now = int(self.gateway.clock())
            return {**self.review(row, now), "server_time": now}
