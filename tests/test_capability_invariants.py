"""Generated offline token trees and lifecycle histories preserve ledger invariants."""

import contextlib
import tempfile
from dataclasses import replace

import pytest
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule
from test_invariants import NOW, PRICES, RemoteResults, assert_narrower, narrow

from nodi.capabilities import CapabilityError, Keys, attenuate, describe, pack, unpack
from nodi.gateway import Gateway
from nodi.models import Action, Contract
from nodi.semantic import SemanticReviewer


class CapabilityHistory(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.directory = tempfile.TemporaryDirectory(prefix="nodi-token-invariants-")
        self.now = NOW
        self.remote = RemoteResults()
        reviewer = SemanticReviewer("https://reviewer.example/intent", "test-only")
        reviewer.opener = self.remote
        self.gateway = Gateway(
            self.directory.name + "/ledger.db",
            clock=lambda: self.now,
            evaluator=reviewer,
            capability_keys=Keys({"test": b"k" * 32}),
        )
        self.tokens = []
        self.requests = []
        self.serial = 0
        self.effects = {}

    def teardown(self):
        self.directory.cleanup()

    @initialize(budget=st.integers(30000, 180000))
    def start(self, budget):
        self.scope = Contract(
            "root",
            "Buy monitors within the reviewed budget.",
            spending_limit=budget,
            maximum_transaction=budget,
            delegation_depth=4,
            max_actions=100,
            expires_at=NOW + 3600,
        )
        self.root = self.gateway.issue(self.scope)["visa_id"]
        self.tokens.append((self.gateway.issue_capability(self.root, "test")["token"], self.scope))

    @rule(data=st.data(), arbitrary=st.booleans())
    def derive(self, data, arbitrary):
        candidates = [
            pair
            for pair in self.tokens
            if pair[1].delegation_depth > 0 and pair[1].expires_at > self.now
        ]
        if not candidates or len(self.tokens) >= 30:
            return
        parent, scope = data.draw(st.sampled_from(candidates))
        child = (
            narrow(data, scope, f"agent_{len(self.tokens)}", self.now)
            if arbitrary
            else replace(
                scope,
                agent_id=f"agent_{len(self.tokens)}",
                delegation_depth=scope.delegation_depth - 1,
            )
        )
        before = self.gateway.snapshot()
        token = attenuate(parent, child)
        assert_narrower(scope, child)
        assert self.gateway.snapshot() == before
        self.tokens.append((token, child))

    def submit(self, token, scope, action):
        before = self.gateway.snapshot()
        nodes = {v["id"]: v for v in before["visas"]}
        key = (scope.agent_id, action.request_id)
        try:
            result = self.gateway.execute_capability(token, action)
        except CapabilityError:
            assert self.gateway.snapshot() == before
            return
        after = self.gateway.snapshot()
        if result.get("replayed"):
            assert after["effects"] == before["effects"]
            return
        if any(not c["passed"] for c in result["policy_checks"]):
            assert result["decision"] == "DENY"
        if result["executed"]:
            assert result["decision"] == "ALLOW" and key not in self.effects
            cost = PRICES[action.arguments["product_id"]]
            chain = []
            after_nodes = {v["id"]: v for v in after["visas"]}
            current = action.visa_id
            while current:
                node = after_nodes[current]
                scope_at_execution = Contract(**node["contract"])
                assert node["status"] == "active" and self.now < scope_at_execution.expires_at
                assert len(chain) <= scope_at_execution.delegation_depth
                assert "purchase" in scope_at_execution.allowed_tools
                assert "purchase" not in scope_at_execution.forbidden_operations
                assert cost <= scope_at_execution.maximum_transaction
                assert nodes.get(current, {"spent": 0})["spent"] + cost == node["spent"]
                assert node["spent"] <= scope_at_execution.spending_limit
                if cost > scope_at_execution.approval_threshold:
                    assert result["human_intervention"]
                chain.append(current)
                current = node["parent"]
            self.effects[key] = {"id": result["effect_id"], "cost": cost, "chain": chain}
        else:
            assert after["effects"] == before["effects"]
        self.requests.append((token, scope, action))

    @rule(data=st.data(), product=st.sampled_from(tuple(PRICES)))
    def purchase(self, data, product):
        token, scope = data.draw(st.sampled_from(self.tokens))
        self.serial += 1
        self.submit(
            token,
            scope,
            Action(
                f"call_{self.serial}",
                describe(token)["visa_id"],
                "purchase",
                "vendor:approved",
                {"product_id": product, "quantity": 1},
            ),
        )

    @rule(data=st.data(), conflict=st.booleans())
    def retry(self, data, conflict):
        if not self.requests:
            return
        token, scope, action = data.draw(st.sampled_from(self.requests))
        if conflict:
            before = self.gateway.snapshot()["effects"]
            with contextlib.suppress(CapabilityError):
                assert (
                    self.gateway.execute_capability(token, replace(action, context="different"))[
                        "decision"
                    ]
                    == "DENY"
                )
            assert self.gateway.snapshot()["effects"] == before
        else:
            self.submit(token, scope, action)

    @rule(data=st.data(), approve=st.booleans())
    def review(self, data, approve):
        with self.gateway.read_transaction() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM requests")]
        if not rows:
            return
        row = data.draw(st.sampled_from(rows))
        before = self.gateway.snapshot()["effects"]
        if row["approval"] == "pending" and self.now < row["deadline"]:
            self.gateway.approve(row["principal"], row["id"], approve, row["fingerprint"])
        else:
            with pytest.raises(ValueError):
                self.gateway.approve(row["principal"], row["id"], approve, row["fingerprint"])
        assert self.gateway.snapshot()["effects"] == before

    @rule(data=st.data())
    def revoke(self, data):
        nodes = self.gateway.snapshot()["visas"]
        self.gateway.revoke(data.draw(st.sampled_from(nodes))["id"])

    @rule(seconds=st.integers(0, 4000))
    def clock(self, seconds):
        self.now += seconds

    @rule(decision=st.sampled_from(["ALLOW", "ASK_HUMAN", "DENY"]))
    def semantic(self, decision):
        self.remote.decision = decision

    @rule(data=st.data())
    def forgery(self, data):
        token, scope = data.draw(st.sampled_from(self.tokens))
        forged = unpack(token)
        forged["signature"] = "0" * 64
        before = self.gateway.snapshot()
        with pytest.raises(CapabilityError):
            self.gateway.execute_capability(
                pack(forged),
                Action(
                    "forged",
                    describe(token)["visa_id"],
                    "purchase",
                    "vendor:approved",
                    {"product_id": "monitor-basic", "quantity": 1},
                ),
            )
        assert self.gateway.snapshot() == before

    @rule()
    def edit_source(self):
        self.gateway.modify(self.root, self.scope)
        root = next(v for v in self.gateway.snapshot()["visas"] if v["id"] == self.root)
        if (
            root["status"] == "active"
            and self.now < self.scope.expires_at
            and len(self.tokens) < 30
        ):
            self.tokens.append(
                (self.gateway.issue_capability(self.root, "test")["token"], self.scope)
            )

    @invariant()
    def conservation_soundness_audit(self):
        state = self.gateway.snapshot()
        assert len(state["effects"]) == len(self.effects)
        for node in state["visas"]:
            effects = [e for e in self.effects.values() if node["id"] in e["chain"]]
            assert (
                node["spent"]
                == sum(e["cost"] for e in effects)
                <= node["contract"]["spending_limit"]
            )
            assert node["actions"] == len(effects) <= node["contract"]["max_actions"]
        for effect in self.effects.values():
            records = [
                r["event"] for r in state["audit"] if r["event"].get("effect_id") == effect["id"]
            ]
            assert len(records) == 1
            assert records[0]["decision"] == "ALLOW" and records[0]["executed"]
            assert records[0]["delegation_chain"] == effect["chain"]
        assert self.gateway.verify_audit()


TestCapabilityHistory = CapabilityHistory.TestCase
