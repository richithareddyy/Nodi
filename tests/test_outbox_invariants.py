"""Generated interleavings independently compare provider charges and ledger holds."""

import contextlib
import tempfile
from dataclasses import replace

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from test_outbox import assert_conserved

from nodi.domains import get_domain
from nodi.gateway import Gateway
from nodi.mock_payments import MockPaymentProvider
from nodi.models import Action
from nodi.outbox import Outbox


class ProcessCrash(BaseException):
    pass


class PaymentMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.directory = tempfile.TemporaryDirectory()
        self.now = 2_000_000_000
        self.provider = MockPaymentProvider(self.directory.name + "/provider.db")
        self.gateway = Gateway(
            self.directory.name + "/ledger.db",
            payment_provider=self.provider,
            clock=lambda: self.now,
        )
        root = replace(
            get_domain("payments").default_contract("root", self.now),
            delegation_depth=4,
            spending_limit=90000,
            maximum_transaction=90000,
            approval_threshold=90000,
        )
        root_id = self.gateway.issue(root)["visa_id"]
        self.nodes = {root_id: root}
        self.actions = []

    def teardown(self):
        self.directory.cleanup()

    @rule(index=st.integers(0, 29), cap=st.integers(0, 90000))
    def delegate(self, index, cap):
        if len(self.nodes) >= 30:
            return
        parent_id = list(self.nodes)[index % len(self.nodes)]
        parent = self.nodes[parent_id]
        if not parent.delegation_depth:
            return
        cap = min(cap, parent.spending_limit)
        maximum = min(cap, parent.maximum_transaction)
        child = replace(
            parent,
            agent_id="child" + str(len(self.nodes)),
            spending_limit=cap,
            maximum_transaction=maximum,
            approval_threshold=min(maximum, parent.approval_threshold),
            delegation_depth=parent.delegation_depth - 1,
        )
        result = self.gateway.delegate(parent.agent_id, parent_id, child)
        if "visa_id" in result:
            self.nodes[result["visa_id"]] = child

    @rule(
        index=st.integers(0, 29),
        product=st.sampled_from(["monitor-basic", "monitor-pro", "monitor-ultra", "headphones"]),
        quantity=st.integers(1, 3),
    )
    def authorize(self, index, product, quantity):
        visa_id = list(self.nodes)[index % len(self.nodes)]
        principal = self.nodes[visa_id].agent_id
        action = Action(
            "request" + str(len(self.actions)),
            visa_id,
            "purchase",
            "vendor:approved",
            {"product_id": product, "quantity": quantity},
        )
        self.gateway.execute(principal, action)
        self.actions.append((principal, action))

    @rule(index=st.integers(0, 99), approve=st.booleans())
    def review(self, index, approve):
        pending = self.gateway.snapshot()["approvals"]
        if not pending:
            return
        row = pending[index % len(pending)]
        with contextlib.suppress(ValueError):
            self.gateway.approve(row["principal"], row["id"], approve)

    @rule(index=st.integers(0, 99), conflict=st.booleans())
    def replay(self, index, conflict):
        if not self.actions:
            return
        principal, action = self.actions[index % len(self.actions)]
        if conflict:
            action = replace(
                action, arguments={**action.arguments, "quantity": action.arguments["quantity"] + 1}
            )
        before = self.provider.snapshot()["payments"]
        response = self.gateway.execute(principal, action)
        if conflict:
            assert response["decision"] == "DENY"
        assert self.provider.snapshot()["payments"] == before

    @rule(
        mode=st.sampled_from(["success", "fail", "timeout", "lose_response", "unavailable"]),
        stage=st.sampled_from(["none", "after_claim", "after_provider", "before_commit"]),
    )
    def work(self, mode, stage):
        self.now += 31
        self.provider.set_mode(mode)

        def crash(current):
            if current == stage:
                raise ProcessCrash()

        with contextlib.suppress(ProcessCrash):
            Outbox(self.gateway, fault=crash).run_once()

    @rule(index=st.integers(0, 29))
    def revoke(self, index):
        visa_id = list(self.nodes)[index % len(self.nodes)]
        self.gateway.revoke(visa_id)

    @rule(index=st.integers(0, 29))
    def narrow(self, index):
        visa_id = list(self.nodes)[index % len(self.nodes)]
        contract = replace(self.nodes[visa_id], allowed_tools=[])
        self.gateway.modify(visa_id, contract)
        self.nodes[visa_id] = contract

    @rule(seconds=st.integers(0, 3601))
    def advance(self, seconds):
        self.now += seconds

    @invariant()
    def no_double_charge_no_unbacked_reservation_and_authorized_dispatch(self):
        assert_conserved(self.gateway, self.provider)
        nodes, dispatched = {}, set()
        for record in self.gateway.snapshot()["audit"]:
            event = record["event"]
            if event["event"] == "visa_issued":
                nodes[event["visa_id"]] = {"contract": event["contract"], "active": True}
            elif event["event"] == "visa_modified":
                nodes[event["visa_id"]]["contract"] = event["after"]
            elif event["event"] == "visa_revoked":
                nodes[event["visa_id"]]["active"] = False
            elif event["event"] == "outbox_dispatch":
                for visa_id in event["delegation_chain"]:
                    node = nodes[visa_id]
                    assert node["active"] and event["timestamp"] < node["contract"]["expires_at"]
                    assert "purchase" in node["contract"]["allowed_tools"]
                assert event["decision"] == "ALLOW"
                dispatched.add(event["outbox_id"])
        for payment in self.provider.snapshot()["payments"]:
            if payment["status"] == "COMMITTED":
                assert payment["key"] in dispatched


TestPaymentInvariants = PaymentMachine.TestCase
