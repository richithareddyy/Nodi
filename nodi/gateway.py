"""Transactional authorization, approvals, budgets and simulated tool effects."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
import sqlite3
import time
from typing import Callable
from uuid import uuid4
from .intent import Evaluator, Intent, DomainEvaluator
from .models import Action, Contract
from .policy import attenuation, checks
from .domains import get_domain
from .storage import private_database

logger = logging.getLogger("nodi.decisions")


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Gateway:
    def __init__(self, path: str, evaluator: Evaluator | None = None, clock: Callable = time.time):
        self.path, self.evaluator, self.clock = path, evaluator or DomainEvaluator(), clock
        private_database(self.path, create=True)
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS visas (
                    id TEXT PRIMARY KEY, parent TEXT REFERENCES visas(id), contract TEXT NOT NULL,
                    issued_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                    spent INTEGER NOT NULL DEFAULT 0, actions INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS visas_parent ON visas(parent);
                CREATE TABLE IF NOT EXISTS requests (
                    principal TEXT NOT NULL, id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    action TEXT NOT NULL, response TEXT NOT NULL, approval TEXT NOT NULL,
                    deadline INTEGER NOT NULL, PRIMARY KEY(principal,id));
                CREATE TABLE IF NOT EXISTS effects (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, request_id TEXT NOT NULL,
                    tool TEXT NOT NULL, result TEXT NOT NULL,
                    UNIQUE(principal,request_id));
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL,
                    previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL);
            ''')

    @contextmanager
    def transaction(self):
        private_database(self.path)
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def read_transaction(self):
        """Consistent read snapshot without reserving the SQLite writer lock."""
        private_database(self.path)
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db
        finally:
            db.rollback()
            db.close()

    def _audit(self, db, event: dict) -> None:
        payload = encode({"timestamp": int(self.clock()), **event})
        previous = db.execute("SELECT event_hash FROM audit ORDER BY sequence DESC LIMIT 1").fetchone()
        previous_hash = previous[0] if previous else "0" * 64
        digest = hashlib.sha256((previous_hash + payload).encode()).hexdigest()
        db.execute("INSERT INTO audit(event,previous_hash,event_hash) VALUES(?,?,?)", (payload, previous_hash, digest))

    def _chain(self, db, visa_id: str) -> list:
        chain, seen = [], set()
        while visa_id:
            if visa_id in seen or len(chain) > 8:
                raise ValueError("invalid delegation chain")
            seen.add(visa_id)
            row = db.execute("SELECT * FROM visas WHERE id=?", (visa_id,)).fetchone()
            if row is None:
                raise ValueError("unknown visa")
            chain.append((row, Contract(**json.loads(row["contract"]))))
            visa_id = row["parent"]
        return chain

    def issue(self, contract: Contract) -> dict:
        if contract.expires_at <= int(self.clock()):
            raise ValueError("cannot issue expired visa")
        with self.transaction() as db:
            return self._insert(db, contract, None)

    def _insert(self, db, contract: Contract, parent: str | None) -> dict:
        visa_id, now = uuid4().hex, int(self.clock())
        db.execute("INSERT INTO visas(id,parent,contract,issued_at) VALUES(?,?,?,?)", (visa_id, parent, encode(contract.dictionary()), now))
        self._audit(db, {"event": "visa_issued", "visa_id": visa_id, "parent": parent, "contract": contract.dictionary()})
        return {"visa_id": visa_id, "parent": parent, "issued_at": now, "status": "active", **contract.dictionary()}

    def delegate(self, principal: str, parent: str, child: Contract) -> dict:
        with self.transaction() as db:
            try:
                chain = self._chain(db, parent)
                if chain[0][1].agent_id != principal:
                    raise ValueError("parent visa does not belong to caller")
                for depth, (row, contract) in enumerate(chain):
                    if depth + 1 > contract.delegation_depth:
                        raise ValueError("ancestor delegation depth exceeded")
                    if row["status"] != "active" or contract.expires_at <= int(self.clock()):
                        raise ValueError("ancestor is inactive or expired")
                attenuation(chain[0][1], child)
                if child.expires_at <= int(self.clock()):
                    raise ValueError("child is expired")
                result = self._insert(db, child, parent)
            except ValueError as exc:
                self._audit(db, {"event": "delegation_denied", "agent": principal, "parent": parent, "reason": str(exc)})
                result = {"decision": "DENY", "reason": str(exc)}
            return result

    @staticmethod
    def _invalidate_reviews(db, visa_id: str) -> int:
        # UNION deduplicates nodes, including in a malformed cyclic stored graph.
        db.execute("""WITH RECURSIVE subtree(id) AS (
            SELECT id FROM visas WHERE id=?
            UNION
            SELECT v.id FROM visas v JOIN subtree s ON v.parent=s.id
        ) UPDATE requests SET approval='invalidated'
          WHERE approval IN ('pending','approved')
          AND json_extract(action, '$.visa_id') IN (SELECT id FROM subtree)""", (visa_id,))
        return db.execute("SELECT changes()").fetchone()[0]

    def revoke(self, visa_id: str) -> None:
        with self.transaction() as db:
            self._chain(db, visa_id)
            db.execute("UPDATE visas SET status='revoked' WHERE id=?", (visa_id,))
            invalidated = self._invalidate_reviews(db, visa_id)
            self._audit(db, {"event": "visa_revoked", "visa_id": visa_id, "invalidated_reviews": invalidated})

    def modify(self, visa_id: str, contract: Contract) -> None:
        """Human edits are rechecked on all subsequent actions, including approvals."""
        with self.transaction() as db:
            chain = self._chain(db, visa_id)
            old = chain[0][1]
            if (contract.agent_id, contract.mission, contract.purpose) != (old.agent_id, old.mission, old.purpose):
                raise ValueError("issue a new visa to change identity or mission")
            if len(chain) > 1:
                attenuation(chain[1][1], contract)
            db.execute("UPDATE visas SET contract=? WHERE id=?", (encode(contract.dictionary()), visa_id))
            invalidated = self._invalidate_reviews(db, visa_id)
            self._audit(db, {"event": "visa_modified", "visa_id": visa_id, "before": old.dictionary(),
                             "after": contract.dictionary(), "invalidated_reviews": invalidated})

    def approve(self, principal: str, request_id: str, approve: bool, expected_fingerprint: str | None = None) -> dict:
        if type(approve) is not bool:
            raise ValueError("approve must be boolean")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM requests WHERE principal=? AND id=?", (principal, request_id)).fetchone()
            if row is None or row["approval"] != "pending" or int(self.clock()) >= row["deadline"]:
                raise ValueError("request is not pending or approval has expired")
            if expected_fingerprint is not None and not hmac.compare_digest(row["fingerprint"], expected_fingerprint):
                raise ValueError("review fingerprint does not match the stored action")
            state = "approved" if approve else "rejected"
            db.execute("UPDATE requests SET approval=? WHERE principal=? AND id=?", (state, principal, request_id))
            self._audit(db, {"event": "human_review", "agent": principal, "request_id": request_id, "outcome": state, "fingerprint": row["fingerprint"]})
            return {"approval": state, "request_id": request_id}

    def execute(self, principal: str, action: Action) -> dict:
        start = time.perf_counter()
        fingerprint = hashlib.sha256(encode(action.dictionary()).encode()).hexdigest()
        with self.transaction() as db:
            previous = db.execute("SELECT * FROM requests WHERE principal=? AND id=?", (principal, action.request_id)).fetchone()
            if previous and previous["fingerprint"] != fingerprint:
                response = {"request_id": action.request_id, "visa_id": action.visa_id, "decision": "DENY", "reason": "request_id is bound to a different action", "executed": False}
                self._audit(db, {"event": "replay_conflict", "agent": principal, "action": action.dictionary(), **response})
                return response
            if previous and previous["approval"] == "completed":
                response = {**json.loads(previous["response"]), "replayed": True}
                self._audit(db, {"event": "idempotent_replay", "agent": principal, "request_id": action.request_id})
                return response
            chain, policy_checks, intent, prepared = [], [], None, None
            policy_ms = 0.0
            decision, reason = "DENY", "authorization failed"
            now = int(self.clock())
            approved = bool(previous and previous["approval"] == "approved" and now < previous["deadline"])
            try:
                chain = self._chain(db, action.visa_id)
                if chain[0][1].agent_id != principal:
                    raise ValueError("visa identity does not match authenticated caller")
                prepared = get_domain(chain[0][1].purpose).prepare(action)
                policy_start = time.perf_counter()
                for depth, (row, contract) in enumerate(chain):
                    policy_checks.append({"visa_id": row["id"], "check": "delegation_depth", "passed": depth <= contract.delegation_depth})
                    policy_checks.extend({"visa_id": row["id"], **check} for check in checks(contract, row["status"], row["spent"], row["actions"], action, prepared, now))
                policy_ms = (time.perf_counter() - policy_start) * 1000
                failures = [p["check"] for p in policy_checks if not p["passed"]]
                if failures:
                    reason = "Hard policy violation: " + ", ".join(sorted(set(failures)))
                elif previous and (previous["approval"] in ("rejected", "invalidated") or now >= previous["deadline"]):
                    reason = "approval rejected, invalidated, or expired; submit a new request"
                else:
                    try:
                        intent = self.evaluator.evaluate(chain[0][1], action, prepared)
                        if not isinstance(intent, Intent):
                            raise ValueError("invalid evaluator result")
                        intent.validate()
                        # Ensure diagnostics can be persisted before an effect is executed.
                        encode(intent.dictionary())
                    except Exception:
                        intent = Intent(0, 1, "Intent evaluator unavailable or invalid", ["evaluation failure"], "ASK_HUMAN")
                    if intent.recommended_decision == "DENY":
                        reason = intent.reason
                    else:
                        needs_review = (intent.recommended_decision == "ASK_HUMAN" or intent.mission_alignment_score < .9 or intent.risk_score >= .7 or any(prepared.cost > c.approval_threshold for _, c in chain))
                        decision = "ASK_HUMAN" if needs_review and not approved else "ALLOW"
                        reason = "Exact action requires human approval" if decision == "ASK_HUMAN" else ("Human approval applied after policy recheck" if approved else intent.reason)
            except (ValueError, TypeError, KeyError) as exc:
                reason = str(exc)
            deadline = previous["deadline"] if previous else min(now + 300, *(c.expires_at for _, c in chain)) if chain else now
            # Evaluation can take time while the transaction protects mutable policy state.
            # Recheck time-dependent authority before granting an effect or a review.
            if decision in ("ALLOW", "ASK_HUMAN"):
                final_now = int(self.clock())
                final_checks = [{"visa_id": row["id"], "check": "unexpired_after_evaluation",
                                 "passed": final_now < contract.expires_at} for row, contract in chain]
                policy_checks.extend(final_checks)
                if any(not check["passed"] for check in final_checks):
                    decision, reason = "DENY", "Visa expired during intent evaluation"
                    approved = False
                elif (previous or decision == "ASK_HUMAN") and final_now >= deadline:
                    decision, reason = "DENY", "Review deadline elapsed during intent evaluation; submit a new request"
                    approved = False
            response = {"request_id": action.request_id, "visa_id": action.visa_id, "decision": decision,
                        "reason": reason, "executed": decision == "ALLOW", "policy_checks": policy_checks,
                        "intent": intent.dictionary() if intent else None,
                        "human_intervention": approved, "estimated_impact": prepared.impact if prepared else None,
                        "cost_cents": prepared.cost if prepared else None, "policy_ms": round(policy_ms, 4)}
            if decision == "ALLOW":
                effect_id = uuid4().hex
                result = {"simulation": True, **prepared.result}
                db.execute("INSERT INTO effects VALUES(?,?,?,?,?)", (effect_id, principal, action.request_id, action.tool, encode(result)))
                for row, _ in chain:
                    db.execute("UPDATE visas SET spent=spent+?, actions=actions+1 WHERE id=?", (prepared.cost, row["id"]))
                response.update({"effect_id": effect_id, "result": result})
            if chain and chain[0][1].agent_id == principal:
                remaining = min(c.spending_limit - row["spent"] for row, c in chain)
                response["remaining_budget_cents"] = max(0, remaining - (prepared.cost if decision == "ALLOW" else 0))
            state = "pending" if decision == "ASK_HUMAN" else "completed"
            if decision == "ASK_HUMAN":
                response["approval_deadline"] = deadline
            response["evaluation_ms"] = round((time.perf_counter() - start) * 1000, 3)
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?) ON CONFLICT(principal,id) DO UPDATE SET response=excluded.response, approval=excluded.approval", (principal, action.request_id, fingerprint, encode(action.dictionary()), encode(response), state, deadline))
            self._audit(db, {"event": "authorization", "agent": principal, "action": action.dictionary(),
                            "mission": chain[0][1].mission if chain else None,
                            "delegation_chain": [r["id"] for r, _ in chain], **response})
        logger.info(encode({"request_id": action.request_id, "agent": principal, "decision": decision, "evaluation_ms": response["evaluation_ms"]}))
        return response

    def snapshot(self) -> dict:
        with self.transaction() as db:
            return {
                "visas": [{**dict(r), "contract": json.loads(r["contract"])} for r in db.execute("SELECT * FROM visas ORDER BY issued_at,id")],
                "approvals": [{**dict(r), "action": json.loads(r["action"]), "response": json.loads(r["response"])} for r in db.execute("SELECT * FROM requests WHERE approval IN ('pending','approved')")],
                "effects": [dict(r) for r in db.execute("SELECT * FROM effects")],
                "audit": [{**dict(r), "event": json.loads(r["event"])} for r in db.execute("SELECT * FROM audit ORDER BY sequence")],
            }

    def verify_audit(self) -> bool:
        with self.read_transaction() as db:
            previous = "0" * 64
            for row in db.execute("SELECT * FROM audit ORDER BY sequence"):
                digest = hashlib.sha256((previous + row["event"]).encode()).hexdigest()
                if row["previous_hash"] != previous or row["event_hash"] != digest:
                    return False
                previous = digest
        return True
