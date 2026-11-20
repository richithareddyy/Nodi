"""Durable reservations, crash windows, reconciliation and shared-budget races."""

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from nodi.api import create_app
from nodi.domains import get_domain
from nodi.gateway import Gateway
from nodi.mock_payments import MockPaymentProvider
from nodi.models import Action
from nodi.outbox import Outbox
from nodi.queries import ReadModel

NOW = 2_000_000_000


@pytest.fixture
def system(tmp_path):
    now = [NOW]
    provider = MockPaymentProvider(tmp_path / "provider.db")
    gateway = Gateway(str(tmp_path / "ledger.db"), payment_provider=provider, clock=lambda: now[0])
    contract = get_domain("payments").default_contract("buyer", NOW)
    visa = gateway.issue(contract)["visa_id"]
    action = Action(
        "buy", visa, "purchase", "vendor:approved", {"product_id": "monitor-basic", "quantity": 1}
    )
    return gateway, provider, now, contract, action


def assert_conserved(gateway, provider):
    state = gateway.snapshot()
    outbox = {r["id"]: r for r in state["outbox"]}
    effects = {r["request_id"]: json.loads(r["result"]) for r in state["effects"]}
    payments = {p["key"]: p for p in provider.snapshot()["payments"]}
    for row in state["outbox"]:
        holds = [r for r in state["reservations"] if r["outbox_id"] == row["id"]]
        assert bool(holds) == (row["state"] in ("PENDING", "UNKNOWN"))
        if row["state"] == "COMMITTED":
            assert payments[row["id"]]["status"] == "COMMITTED"
            assert row["request_id"] in effects
        if row["state"] == "FAILED":
            assert payments.get(row["id"], {}).get("status") != "COMMITTED"
    for node in state["visas"]:

        def under(leaf):
            nodes = {v["id"]: v for v in state["visas"]}
            while leaf:
                if leaf == node["id"]:
                    return True
                leaf = nodes[leaf]["parent"]
            return False

        descendants = [r for r in outbox.values() if under(r["visa_id"])]
        committed = [r for r in descendants if r["state"] == "COMMITTED"]
        pending = [r for r in descendants if r["state"] in ("PENDING", "UNKNOWN")]
        assert node["spent"] == sum(r["amount"] for r in committed)
        assert node["reserved_cents"] == sum(r["amount"] for r in pending)
        assert node["actions"] == len(committed)
        assert node["reserved_actions"] == len(pending)
        assert node["spent"] + node["reserved_cents"] <= node["contract"]["spending_limit"]
        assert node["actions"] + node["reserved_actions"] <= node["contract"]["max_actions"]
        actual = sum(payments[r["id"]]["charged_cents"] for r in descendants if r["id"] in payments)
        assert actual <= node["spent"] + node["reserved_cents"]
    assert gateway.verify_audit()


def test_reserve_then_commit_and_replay(system):
    gateway, provider, now, contract, action = system
    result = gateway.execute("buyer", action)
    assert (
        result["decision"] == "ALLOW"
        and result["execution_state"] == "PENDING"
        and not result["executed"]
    )
    assert result["remaining_budget_cents"] == 12000
    assert not provider.snapshot()["payments"]
    assert not gateway.snapshot()["effects"]
    limits = ReadModel(gateway).inspect_visa(action.visa_id)["effective_limits"]
    assert limits["remaining_budget_cents"] == 12000 and limits["remaining_actions"] == 29
    assert gateway.execute("buyer", action)["outbox_id"] == result["outbox_id"]
    assert len(gateway.snapshot()["outbox"]) == 1
    assert_conserved(gateway, provider)
    Outbox(gateway).run_once()
    result = gateway.execute("buyer", action)
    assert result["execution_state"] == "COMMITTED" and result["executed"]
    assert result["result"]["mock"] and result["replayed"]
    assert Outbox(gateway).run_once() is None
    assert_conserved(gateway, provider)


@pytest.mark.parametrize("mode", ["fail", "timeout", "lose_response", "unavailable"])
def test_terminal_failure_or_unknown_keeps_exact_reservation(system, mode):
    gateway, provider, now, _, action = system
    key = gateway.execute("buyer", action)["outbox_id"]
    provider.set_mode(mode)
    Outbox(gateway).run_once()
    state = gateway.execute("buyer", action)["execution_state"]
    assert state == ("FAILED" if mode == "fail" else "UNKNOWN")
    assert_conserved(gateway, provider)
    provider.set_mode("success")
    now[0] += 31
    Outbox(gateway).run_once()
    assert gateway.execute("buyer", action)["execution_state"] == (
        "FAILED" if mode == "fail" else "COMMITTED"
    )
    assert not gateway.snapshot()["reservations"]
    assert_conserved(gateway, provider)
    calls = [c["method"] for c in provider.snapshot()["calls"] if c["key"] == key]
    if mode == "lose_response":
        assert calls.count("charge") == 1 and "lookup" in calls


@pytest.mark.parametrize(
    "stage", ["after_reserve", "after_claim", "after_provider", "before_commit"]
)
def test_actual_process_death_and_restart(system, stage):
    gateway, provider, now, _, action = system
    # Child exits without unwinding Python or closing its transaction normally.
    code = """
import json, os, sys
from nodi.gateway import Gateway
from nodi.mock_payments import MockPaymentProvider
from nodi.models import Action
from nodi.outbox import Outbox
ledger, provider, action, stage = sys.argv[1:]
g = Gateway(ledger, payment_provider=MockPaymentProvider(provider), clock=lambda: 2000000000)
g.execute('buyer', Action(**json.loads(action)))
if stage == 'after_reserve': os._exit(71)
def crash(current):
    if current == stage: os._exit(71)
Outbox(g, fault=crash).run_once()
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            gateway.path,
            provider.path,
            json.dumps(action.dictionary()),
            stage,
        ],
        capture_output=True,
        timeout=15,
    )
    assert child.returncode == 71, child.stderr.decode()
    restarted_provider = MockPaymentProvider(provider.path)
    restarted = Gateway(gateway.path, payment_provider=restarted_provider, clock=lambda: NOW + 31)
    row = restarted.snapshot()["outbox"][0]
    assert row["state"] == ("PENDING" if stage == "after_reserve" else "UNKNOWN")
    assert_conserved(restarted, restarted_provider)
    Outbox(restarted).run_once()
    assert restarted.execute("buyer", action)["execution_state"] == "COMMITTED"
    assert_conserved(restarted, restarted_provider)
    assert len(restarted_provider.snapshot()["payments"]) == 1
    assert sum(c["method"] == "charge" for c in restarted_provider.snapshot()["calls"]) == 1


@pytest.mark.parametrize("workers", [10, 50])
def test_sibling_reservation_race_and_duplicate_delivery(system, workers):
    gateway, provider, now, contract, action = system
    contract = replace(
        contract,
        spending_limit=90000,
        maximum_transaction=90000,
        approval_threshold=90000,
        max_actions=30,
    )
    gateway.modify(action.visa_id, contract)
    children = [
        gateway.delegate(
            "buyer", action.visa_id, replace(contract, agent_id=f"child{n}", delegation_depth=0)
        )["visa_id"]
        for n in range(workers)
    ]
    barrier = threading.Barrier(workers)

    def reserve(n):
        barrier.wait()
        return gateway.execute(
            f"child{n}", replace(action, request_id=f"buy{n}", visa_id=children[n])
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(reserve, range(workers)))
    assert sum(r["decision"] == "ALLOW" for r in results) == 5
    assert_conserved(gateway, provider)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda _: Outbox(gateway).run_once(), range(workers)))
    assert len(provider.snapshot()["payments"]) == 5
    assert not gateway.snapshot()["reservations"]
    assert_conserved(gateway, provider)


def test_retry_storm_reconciles_lost_response_without_recharging(system):
    gateway, provider, now, _, action = system
    key = gateway.execute("buyer", action)["outbox_id"]
    provider.set_mode("lose_response")
    Outbox(gateway).run_once()
    now[0] += 31
    with ThreadPoolExecutor(max_workers=50) as pool:
        list(pool.map(lambda _: Outbox(gateway).run_once(key), range(50)))
    assert sum(c["method"] == "charge" for c in provider.snapshot()["calls"]) == 1
    assert gateway.execute("buyer", action)["executed"]
    assert_conserved(gateway, provider)


@pytest.mark.parametrize("change", ["revoke", "edit", "expire"])
def test_pending_does_not_dispatch_after_policy_change(system, change):
    gateway, provider, now, contract, action = system
    gateway.execute("buyer", action)
    if change == "revoke":
        gateway.revoke(action.visa_id)
    elif change == "edit":
        gateway.modify(action.visa_id, replace(contract, allowed_tools=[]))
    else:
        now[0] = contract.expires_at
    Outbox(gateway).run_once()
    assert gateway.execute("buyer", action)["execution_state"] == "FAILED"
    assert not provider.snapshot()["payments"]
    assert_conserved(gateway, provider)


def test_unknown_absent_after_revoke_is_fenced_before_release(system):
    gateway, provider, now, _, action = system
    key = gateway.execute("buyer", action)["outbox_id"]
    provider.set_mode("timeout")
    Outbox(gateway).run_once()
    gateway.revoke(action.visa_id)
    now[0] += 31
    Outbox(gateway).run_once()
    row = gateway.snapshot()["outbox"][0]
    provider.set_mode("success")
    assert provider.charge(key, json.loads(row["payload"]))["status"] == "FAILED"
    assert_conserved(gateway, provider)


def test_late_stale_worker_cannot_charge_after_fenced_cancellation(system):
    gateway, provider, now, _, action = system
    key = gateway.execute("buyer", action)["outbox_id"]
    arrived, resume = threading.Event(), threading.Event()
    charge = provider.charge

    def delayed(*args):
        arrived.set()
        assert resume.wait(10)
        return charge(*args)

    provider.charge = delayed
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(Outbox(gateway).run_once)
        assert arrived.wait(5)
        now[0] += 31
        gateway.revoke(action.visa_id)
        Outbox(gateway).run_once(key)
        resume.set()
        future.result(timeout=5)
    assert provider.snapshot()["payments"][0]["status"] == "FAILED"
    assert_conserved(gateway, provider)


def test_unknown_success_after_revoke_is_accounted_not_refunded(system):
    gateway, provider, now, _, action = system
    gateway.execute("buyer", action)
    provider.set_mode("lose_response")
    Outbox(gateway).run_once()
    gateway.revoke(action.visa_id)
    now[0] += 31
    Outbox(gateway).run_once()
    assert gateway.execute("buyer", action)["executed"]
    assert_conserved(gateway, provider)


def test_cap_reduction_cannot_erase_uncertain_exposure(system):
    gateway, provider, now, contract, action = system
    gateway.execute("buyer", action)
    provider.set_mode("timeout")
    Outbox(gateway).run_once()
    with pytest.raises(ValueError, match="reserved"):
        gateway.modify(
            action.visa_id,
            replace(
                contract, spending_limit=10000, maximum_transaction=10000, approval_threshold=10000
            ),
        )
    assert_conserved(gateway, provider)


def test_approval_is_exact_and_rechecked_at_dispatch(system):
    gateway, provider, now, _, action = system
    action = replace(action, arguments={"product_id": "monitor-pro", "quantity": 1})
    assert gateway.execute("buyer", action)["decision"] == "ASK_HUMAN"
    assert not gateway.snapshot()["reservations"]
    gateway.approve("buyer", action.request_id, True)
    assert gateway.execute("buyer", action)["execution_state"] == "PENDING"
    now[0] += 300
    Outbox(gateway).run_once()
    assert gateway.execute("buyer", action)["execution_state"] == "FAILED"
    assert not provider.snapshot()["payments"]


def test_request_conflict_and_provider_key_conflict(system):
    gateway, provider, _, _, action = system
    result = gateway.execute("buyer", action)
    conflict = replace(action, arguments={"product_id": "monitor-pro", "quantity": 1})
    assert gateway.execute("buyer", conflict)["decision"] == "DENY"
    Outbox(gateway).run_once()
    payload = json.loads(gateway.snapshot()["outbox"][0]["payload"])
    with pytest.raises(ValueError, match="bound"):
        provider.charge(result["outbox_id"], {**payload, "amount_cents": 1})
    assert_conserved(gateway, provider)


def test_provider_identity_change_never_retries_against_empty_provider(system, tmp_path):
    gateway, provider, now, _, action = system
    gateway.execute("buyer", action)
    provider.set_mode("lose_response")
    Outbox(gateway).run_once()
    now[0] += 31
    gateway.payment_provider = MockPaymentProvider(tmp_path / "replacement.db")
    with pytest.raises(ValueError, match="identity"):
        Outbox(gateway).run_once()
    assert_conserved(gateway, provider)


def test_http_exposes_authorization_separately_from_execution(system):
    gateway, provider, _, _, action = system
    client = TestClient(create_app(gateway, "a" * 32, {"b" * 32: "buyer"}))
    assert client.post("/execute", json=action.dictionary()).status_code == 401
    result = client.post(
        "/execute", json=action.dictionary(), headers={"Authorization": "Bearer " + "b" * 32}
    ).json()
    assert result["decision"] == "ALLOW" and not result["executed"]
    Outbox(gateway).run_once()
    status = client.get(
        "/requests/status",
        params={"request_id": "buy"},
        headers={"Authorization": "Bearer " + "b" * 32},
    ).json()
    assert status["last_response"]["execution_state"] == "COMMITTED"
    assert_conserved(gateway, provider)


def test_disabled_domain_does_not_silently_simulate(tmp_path):
    gateway = Gateway(str(tmp_path / "ledger.db"), clock=lambda: NOW)
    visa = gateway.issue(get_domain("payments").default_contract("buyer", NOW))["visa_id"]
    result = gateway.execute(
        "buyer",
        Action(
            "x", visa, "purchase", "vendor:approved", {"product_id": "monitor-basic", "quantity": 1}
        ),
    )
    assert result["decision"] == "DENY" and not gateway.snapshot().get("outbox", [])


def test_invalid_provider_lookup_keeps_reservation(system, monkeypatch):
    gateway, provider, now, _, action = system
    gateway.execute("buyer", action)
    provider.set_mode("timeout")
    Outbox(gateway).run_once()
    now[0] += 31
    monkeypatch.setattr(provider, "lookup", lambda *args: {"status": "FAILED", "charged_cents": 0})
    Outbox(gateway).run_once()
    assert gateway.execute("buyer", action)["execution_state"] == "UNKNOWN"
    assert_conserved(gateway, provider)


def test_reservation_transaction_rolls_back_on_audit_failure(system, monkeypatch):
    gateway, provider, _, _, action = system

    def fail(*args):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(gateway, "_audit", fail)
    with pytest.raises(RuntimeError):
        gateway.execute("buyer", action)
    snapshot = gateway.snapshot()
    assert not snapshot["outbox"] and not snapshot["reservations"]
    assert not provider.snapshot()["payments"]


def test_settlement_audit_failure_reconciles_without_second_charge(system, monkeypatch):
    gateway, provider, now, _, action = system
    gateway.execute("buyer", action)
    original = gateway._audit

    def audit(db, event):
        if event["event"] == "outbox_settled":
            raise RuntimeError("audit unavailable")
        return original(db, event)

    monkeypatch.setattr(gateway, "_audit", audit)
    Outbox(gateway).run_once()
    assert gateway.execute("buyer", action)["execution_state"] == "UNKNOWN"
    assert_conserved(gateway, provider)
    monkeypatch.setattr(gateway, "_audit", original)
    now[0] += 31
    Outbox(gateway).run_once()
    assert sum(c["method"] == "charge" for c in provider.snapshot()["calls"]) == 1
    assert_conserved(gateway, provider)


def test_pending_ancestor_edit_releases_all_subtree_reservations(system):
    gateway, provider, _, contract, action = system
    child = gateway.delegate(
        "buyer", action.visa_id, replace(contract, agent_id="child", delegation_depth=0)
    )["visa_id"]
    gateway.execute("child", replace(action, visa_id=child))
    assert len(gateway.snapshot()["reservations"]) == 2
    gateway.modify(action.visa_id, replace(contract, allowed_tools=[]))
    assert not gateway.snapshot()["reservations"]
    assert_conserved(gateway, provider)


def test_cli_worker_uses_same_durable_provider(system, tmp_path):
    gateway, provider, now, _, action = system
    # CLI uses the real clock; this fixture's future deadline remains valid.
    gateway.execute("buyer", action)
    config = tmp_path / "worker.json"
    config.write_text(
        json.dumps({"database": gateway.path, "mock_payments": {"database": provider.path}})
    )
    result = subprocess.run(
        [sys.executable, "-m", "nodi.outbox", "--config", str(config), "--limit", "10"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)["processed"]) == 1
    assert gateway.execute("buyer", action)["executed"]
    assert_conserved(gateway, provider)


def test_restart_with_wrong_provider_store_fails_closed(system, tmp_path):
    gateway, provider, _, _, action = system
    gateway.execute("buyer", action)
    replacement = MockPaymentProvider(tmp_path / "wrong.db")
    with pytest.raises(ValueError, match="original provider"):
        Gateway(gateway.path, payment_provider=replacement)
    assert_conserved(gateway, provider)


def test_provider_rejects_symlink_store_and_shared_ledger(system, tmp_path):
    from nodi.storage import UnsafeStoreError

    gateway, provider, _, _, _ = system
    link = tmp_path / "linked.db"
    link.symlink_to(provider.path)
    with pytest.raises(UnsafeStoreError):
        MockPaymentProvider(link)
    with pytest.raises(ValueError, match="separate"):
        Gateway(provider.path, payment_provider=provider)


def test_expired_lease_duplicate_delivery_uses_same_provider_key(system):
    gateway, provider, now, _, action = system
    key = gateway.execute("buyer", action)["outbox_id"]
    arrived, resume = threading.Event(), threading.Event()
    charge = provider.charge
    first = [True]
    lock = threading.Lock()

    def delayed(*args):
        with lock:
            pause = first[0]
            first[0] = False
        if pause:
            arrived.set()
            assert resume.wait(10)
        return charge(*args)

    provider.charge = delayed
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(Outbox(gateway).run_once)
        assert arrived.wait(5)
        now[0] += 31
        Outbox(gateway).run_once(key)
        resume.set()
        old.result(timeout=5)
    calls = provider.snapshot()["calls"]
    assert sum(c["method"] == "charge" for c in calls) == 2
    assert len(provider.snapshot()["payments"]) == 1
    assert_conserved(gateway, provider)
