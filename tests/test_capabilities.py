"""Offline attenuation, cryptographic rejection, and current-ledger enforcement."""

import asyncio
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from nodi.api import create_app
from nodi.capabilities import (
    CapabilityError,
    Keys,
    attenuate,
    chain_mac,
    describe,
    from_config,
    pack,
    unpack,
)
from nodi.domains import get_domain
from nodi.gateway import Gateway
from nodi.mock_payments import MockPaymentProvider
from nodi.models import Action
from nodi.outbox import Outbox
from nodi.queries import ReadModel

NOW = 2000000000
KEY = b"k" * 32


@pytest.fixture
def system(tmp_path):
    now = [NOW]
    keys = Keys({"v1": KEY})
    gateway = Gateway(str(tmp_path / "ledger.db"), clock=lambda: now[0], capability_keys=keys)
    contract = replace(get_domain("procurement").default_contract("buyer", NOW), delegation_depth=4)
    visa = gateway.issue(contract)["visa_id"]
    token = gateway.issue_capability(visa, "v1")["token"]
    return gateway, keys, now, contract, visa, token


def action(token, request="buy", product="monitor-basic"):
    return Action(
        request,
        describe(token)["visa_id"],
        "purchase",
        "vendor:approved",
        {"product_id": product, "quantity": 1},
    )


def child(token, contract, **changes):
    scope = replace(contract, delegation_depth=contract.delegation_depth - 1, **changes)
    return attenuate(token, scope), scope


def test_offline_children_share_prefix_and_budget(system):
    g, keys, now, c, visa, root = system
    token, c = child(root, c, agent_id="helper")
    first, _ = child(token, c, agent_id="one")
    second, _ = child(token, c, agent_id="two")
    assert len(g.snapshot()["visas"]) == 1  # Attenuation never contacted the gateway.
    result = g.execute_capability(first, action(first))
    assert result["executed"]
    assert g.execute_capability(first, action(first))["effect_id"] == result["effect_id"]
    assert g.execute_capability(second, action(second))["decision"] == "DENY"
    state = g.snapshot()
    assert len(state["effects"]) == 1 and len(state["visas"]) == 4
    assert next(v for v in state["visas"] if v["id"] == visa)["spent"] == 18000
    assert (
        next(v for v in state["visas"] if v["id"] == describe(token)["visa_id"])["spent"] == 18000
    )
    assert g.verify_audit()


@pytest.mark.parametrize("mutation", ["remove", "reorder", "root", "signature", "append_wider"])
def test_signature_and_semantic_forgery_rejected(system, mutation):
    g, keys, now, c, visa, root = system
    one, c = child(root, c, agent_id="one")
    two, c = child(one, c, agent_id="two")
    data = unpack(two)
    if mutation == "remove":
        data["caveats"].pop()
    if mutation == "reorder":
        data["caveats"].reverse()
    if mutation == "root":
        data["root"]["contract"]["mission"] = "Buy unrelated goods"
    if mutation == "signature":
        data["signature"] = "0" * 64
    if mutation == "append_wider":
        caveat = {
            "id": "a" * 32,
            "contract": replace(c, delegation_depth=0, spending_limit=60000).dictionary(),
        }
        data["signature"] = chain_mac(bytes.fromhex(data["signature"]), b"caveat", caveat).hex()
        data["caveats"].append(caveat)
    before = g.snapshot()
    with pytest.raises(CapabilityError):
        g.execute_capability(pack(data), action(two))
    assert g.snapshot() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"spending_limit": 60000},
        {"maximum_transaction": 30001, "spending_limit": 30001},
        {"approval_threshold": 26000},
        {"expires_at": NOW + 7200},
        {"max_actions": 31},
        {"allowed_tools": ["upload_file"]},
        {"allowed_resources": ["storage:external"]},
        {"forbidden_resources": []},
        {"forbidden_operations": []},
        {"mission": "Different task"},
        {"purpose": "research_assistant"},
    ],
)
def test_offline_cannot_widen(system, changes):
    g, keys, now, c, visa, root = system
    with pytest.raises(ValueError):
        child(root, c, **changes)


def test_append_always_consumes_depth(system):
    g, keys, now, c, visa, token = system
    with pytest.raises(ValueError):
        attenuate(token, c)
    for _ in range(4):
        token, c = child(token, c)
    with pytest.raises(ValueError):
        attenuate(token, c)
    assert keys.verify(token).visa_id == describe(token)["visa_id"]


@pytest.mark.parametrize("event", ["revoke_root", "revoke_prefix", "expiry", "edit_restore"])
def test_lifecycle_cannot_be_replayed_away(system, event):
    g, keys, now, c, visa, root = system
    token, c = child(root, c)
    nested, _ = child(token, c)
    request = action(nested)
    g.execute_capability(nested, request)
    if event == "revoke_root":
        g.revoke(visa)
    elif event == "revoke_prefix":
        g.revoke(describe(token)["visa_id"])
    elif event == "expiry":
        now[0] += 3601
    else:
        old = next(v for v in g.snapshot()["visas"] if v["id"] == visa)["contract"]
        from nodi.models import Contract

        g.modify(visa, replace(Contract(**old), max_actions=29))
        g.modify(visa, Contract(**old))
    with pytest.raises(CapabilityError):
        g.execute_capability(nested, request)
    assert len(g.snapshot()["effects"]) == 1
    assert g.verify_audit()


def test_scopes_cannot_be_mutated_or_used_with_ordinary_credentials(system):
    g, keys, now, c, visa, root = system
    token, c = child(root, c)
    g.execute_capability(token, action(token))
    with pytest.raises(ValueError):
        g.modify(describe(token)["visa_id"], replace(c, max_actions=1))
    with pytest.raises(CapabilityError):
        g.execute(c.agent_id, action(token, "raw"))
    assert (
        g.delegate(c.agent_id, describe(token)["visa_id"], replace(c, delegation_depth=0))[
            "decision"
        ]
        == "DENY"
    )
    with pytest.raises(CapabilityError):
        g.execute_capability(token, replace(action(token, "other"), visa_id=visa))


@pytest.mark.parametrize("workers", [10, 50])
def test_concurrent_siblings_and_copied_tokens_do_not_multiply_budget(system, workers):
    g, keys, now, c, visa, root = system

    def run(i):
        token, _ = child(root, c, agent_id=f"helper-{i}")
        return g.execute_capability(token, action(token, f"buy-{i}"))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, range(workers)))
    assert sum(r["executed"] for r in results) == 1
    assert sum(v["spent"] for v in g.snapshot()["visas"] if v["id"] == visa) == 18000
    assert g.verify_audit()


def test_review_is_exact_and_hard_denial_remains_final(system):
    g, keys, now, c, visa, root = system
    token, _ = child(root, c)
    request = action(token, product="monitor-pro")
    assert g.execute_capability(token, request)["decision"] == "ASK_HUMAN"
    row = ReadModel(g).request_status(c.agent_id, request.request_id)
    g.approve(c.agent_id, request.request_id, True, row["fingerprint"])
    assert g.execute_capability(token, request)["executed"]
    denied = g.execute_capability(token, action(token, "ultra", "monitor-ultra"))
    assert denied["decision"] == "DENY"
    with pytest.raises(ValueError):
        g.approve(c.agent_id, "ultra", True)
    assert (
        g.execute_capability(token, action(token, "ultra", "monitor-ultra"))["decision"] == "DENY"
    )


def test_key_retirement_and_foreign_ledger_fail_closed(system, tmp_path):
    g, keys, now, c, visa, root = system
    other = Gateway(str(tmp_path / "other.db"), clock=lambda: NOW, capability_keys=keys)
    with pytest.raises(CapabilityError):
        other.execute_capability(root, action(root))
    restarted = Gateway(g.path, clock=lambda: NOW, capability_keys=Keys({"v2": b"z" * 32}))
    with pytest.raises(CapabilityError):
        restarted.execute_capability(root, action(root))
    restored = Gateway(
        g.path, clock=lambda: NOW, capability_keys=Keys({"v1": KEY, "v2": b"z" * 32})
    )
    assert restored.execute_capability(root, action(root))["executed"]


def test_audit_failure_rolls_back_materialization_and_effect(system, monkeypatch):
    g, keys, now, c, visa, root = system
    token, _ = child(root, c)
    before = g.snapshot()

    def fail(*args):
        raise RuntimeError("controlled audit failure")

    monkeypatch.setattr(g, "_audit", fail)
    with pytest.raises(RuntimeError):
        g.execute_capability(token, action(token))
    assert g.snapshot() == before


def test_http_and_status_scope(system):
    g, keys, now, c, visa, root = system
    one, _ = child(root, c)
    two, _ = child(root, c)
    client = TestClient(create_app(g, "a" * 32, {"b" * 32: "buyer"}))

    def headers(token):
        return {"Authorization": "Bearer " + token}

    minted = client.post(
        f"/visas/{visa}/capability", headers=headers("a" * 32), json={"key_id": "v1"}
    )
    assert minted.status_code == 200 and minted.headers["cache-control"] == "no-store"
    assert (
        client.post(
            f"/visas/{visa}/capability", headers=headers(root), json={"key_id": "v1"}
        ).status_code
        == 401
    )
    assert client.post("/execute", headers=headers(one), json=action(one).dictionary()).json()[
        "executed"
    ]
    assert client.get("/requests/status?request_id=buy", headers=headers(one)).status_code == 200
    # A same-identity sibling cannot read the first scope's result.
    result = client.get("/requests/status?request_id=buy", headers=headers(two))
    assert result.status_code == 404, result.text
    assert (
        client.post(
            "/execute", headers=headers("b" * 32), json=action(one, "raw").dictionary()
        ).status_code
        == 401
    )
    assert client.get("/control", headers=headers(one)).status_code == 401
    assert (
        client.post(
            "/approvals",
            headers=headers(one),
            json={
                "agent_id": "buyer",
                "request_id": "buy",
                "approve": True,
                "expected_fingerprint": "0" * 64,
            },
        ).status_code
        == 401
    )
    g.revoke(visa)
    assert client.get("/requests/status?request_id=buy", headers=headers(one)).status_code == 401


def test_mcp_uses_capability_without_new_tool_surface(system):
    httpx = pytest.importorskip("httpx2")
    from mcp import Client

    from nodi.mcp_server import GatewayClient, create_mcp

    g, keys, now, c, visa, root = system
    token, _ = child(root, c)
    server = create_mcp(
        GatewayClient(
            "http://127.0.0.1",
            token,
            transport=httpx.ASGITransport(app=create_app(g, "a" * 32, {"b" * 32: "buyer"})),
        )
    )

    async def run():
        async with Client(server) as client:
            result = await client.call_tool(
                "execute_action", {"action": action(token).dictionary()}
            )
            assert not result.is_error and result.structured_content["executed"]
            status = await client.call_tool(
                "get_request_status", {"request": {"request_id": "buy"}}
            )
            assert status.structured_content["found"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode", ["pending_edit", "unknown_edit", "unknown_revoke", "committed_revoke"]
)
def test_outbox_capability_lifecycle(tmp_path, mode):
    from test_outbox import assert_conserved

    now = [NOW]
    provider = MockPaymentProvider(tmp_path / "provider.db")
    g = Gateway(
        str(tmp_path / "ledger.db"),
        clock=lambda: now[0],
        capability_keys=Keys({"v1": KEY}),
        payment_provider=provider,
    )
    c = get_domain("payments").default_contract("buyer", NOW)
    visa = g.issue(c)["visa_id"]
    root = g.issue_capability(visa, "v1")["token"]
    token, _ = child(root, c)
    g.execute_capability(token, action(token))
    if mode != "pending_edit":
        provider.set_mode("lose_response" if mode == "committed_revoke" else "timeout")
        Outbox(g).run_once()
    if mode.endswith("edit"):
        g.modify(visa, c)
    else:
        g.revoke(visa)
    provider.set_mode("success")
    now[0] += 31
    Outbox(g).run_once()
    assert g.snapshot()["outbox"][0]["state"] == (
        "COMMITTED" if mode == "committed_revoke" else "FAILED"
    )
    assert_conserved(g, provider)


def test_offline_cli_writes_private_token(system, tmp_path):
    g, keys, now, c, visa, root = system
    parent = tmp_path / "parent.token"
    parent.write_text(root)
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps(replace(c, delegation_depth=0).dictionary()))
    output = tmp_path / "child.token"
    args = [
        sys.executable,
        "-m",
        "nodi.capabilities",
        "attenuate",
        "--input",
        str(parent),
        "--contract",
        str(scope),
        "--output",
        str(output),
    ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert output.stat().st_mode & 0o777 == 0o600
    assert keys.verify(output.read_text().strip())
    assert output.read_text().strip() not in result.stdout
    assert subprocess.run(args, capture_output=True, timeout=10).returncode != 0


@pytest.mark.parametrize("value", [{}, {"v1": "x" * 64}, {"v1": "00" * 31}, {"v1": True}, []])
def test_invalid_key_config(value):
    with pytest.raises(ValueError):
        from_config(value)


@pytest.mark.parametrize(
    "value", ["nodi1.", "nodi1.====", "nodi1." + "A" * 32768, "nodi1.e30", "not-a-token"]
)
def test_malformed_token(value):
    with pytest.raises(CapabilityError):
        Keys({"v1": KEY}).verify(value)
