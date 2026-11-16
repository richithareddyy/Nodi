import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
import pytest
from fastapi.testclient import TestClient
from nodi.api import create_app
from nodi.gateway import Gateway
from nodi.models import Action, draft
from nodi.queries import ReadModel
from nodi.operator import ControlClient, OperatorError, parser, run

ADMIN = "admin-" + "a" * 40
AGENT = "agent-" + "b" * 40


@pytest.fixture
def system(tmp_path):
    gateway = Gateway(str(tmp_path / "state.db"))
    api = TestClient(create_app(gateway, ADMIN, {AGENT: "procurement"}))
    class Client:
        def __init__(self):
            self.calls = []
        def request(self, method, path, body=None):
            self.calls.append((method, path, body))
            response = api.request(method, path, json=body, headers={"Authorization": "Bearer " + ADMIN})
            if response.status_code != 200:
                raise OperatorError("HTTP error")
            return response.json()
    return gateway, Client()


def pending(gateway):
    visa = gateway.issue(draft("Buy a monitor under $300.", "procurement"))["visa_id"]
    action = Action("buy", visa, "purchase", "vendor:approved", {"product_id": "monitor-pro", "quantity": 1})
    assert gateway.execute("procurement", action)["decision"] == "ASK_HUMAN"
    return action


def test_draft_issue_and_modify(system, tmp_path):
    gateway, client = system
    path = tmp_path / "contract.json"
    run(parser().parse_args(["draft", "--agent", "procurement", "--mission", "Buy a monitor under $300.", "--output", str(path)]), client)
    assert path.stat().st_mode & 0o777 == 0o600
    result = run(parser().parse_args(["issue", "--contract", str(path)]), client)
    contract = json.loads(path.read_text()); contract["max_actions"] = 10
    path.write_text(json.dumps(contract))
    run(parser().parse_args(["modify", "--visa", result["visa_id"], "--contract", str(path)]), client)
    assert gateway.snapshot()["visas"][0]["contract"]["max_actions"] == 10


def test_draft_never_overwrites(system, tmp_path):
    _, client = system
    path = tmp_path / "contract.json"; path.write_text("existing")
    with pytest.raises(OperatorError):
        run(parser().parse_args(["draft", "--agent", "procurement", "--mission", "Buy a monitor under $300.", "--output", str(path)]), client)
    assert path.read_text() == "existing"
    assert not client.calls


def test_inspect_and_fingerprint_bound_approval(system):
    gateway, client = system
    action = pending(gateway)
    args = ["--agent", "procurement", "--request-id", "buy"]
    review = run(parser().parse_args(["inspect", *args]), client)
    assert review["mission"] == "Buy a monitor under $300."
    assert review["cost_cents"] == 27500
    assert review["impact"] == "irreversible"
    assert review["action"] == action.dictionary()
    result = run(parser().parse_args(["approve", *args, "--fingerprint", review["fingerprint"]]), client)
    assert result["result"]["approval"] == "approved"
    assert not gateway.snapshot()["effects"]
    assert gateway.execute("procurement", action)["executed"]
    review_events = [r["event"] for r in gateway.snapshot()["audit"] if r["event"]["event"] == "human_review"]
    assert review_events[0]["fingerprint"] == review["fingerprint"]


def test_wrong_fingerprint_cannot_approve(system):
    gateway, client = system
    pending(gateway)
    with pytest.raises(OperatorError):
        run(parser().parse_args(["approve", "--agent", "procurement", "--request-id", "buy", "--fingerprint", "0" * 64]), client)
    assert not any(path == "/approvals" for _, path, _ in client.calls)
    with pytest.raises(ValueError):
        gateway.approve("procurement", "buy", True, "0" * 64)
    assert gateway.snapshot()["approvals"][0]["approval"] == "pending"


def test_expired_review_cannot_approve(system):
    gateway, client = system
    pending(gateway)
    with gateway.transaction() as db:
        db.execute("UPDATE requests SET deadline=1")
    review = ReadModel(gateway).inspect("procurement", "buy")
    assert review["expired"]
    with pytest.raises(OperatorError):
        run(parser().parse_args(["approve", "--agent", "procurement", "--request-id", "buy", "--fingerprint", review["fingerprint"]]), client)


def test_reject_revoke_audit_and_delegations(system):
    gateway, client = system
    action = pending(gateway)
    review = ReadModel(gateway).inspect("procurement", "buy")
    run(parser().parse_args(["reject", "--agent", "procurement", "--request-id", "buy", "--fingerprint", review["fingerprint"]]), client)
    assert gateway.execute("procurement", action)["decision"] == "DENY"
    blocked = run(parser().parse_args(["audit", "--blocked", "--limit", "1"]), client)
    assert len(blocked["audit"]) == 1
    assert blocked["audit"][0]["event"]["decision"] == "DENY"
    assert run(parser().parse_args(["integrity"]), client)["valid"]
    assert run(parser().parse_args(["delegations"]), client)["delegations"][0]["parent"] is None
    run(parser().parse_args(["revoke", "--visa", action.visa_id]), client)
    assert gateway.snapshot()["visas"][0]["status"] == "revoked"


@pytest.mark.parametrize("status", [302, 307, 401, 403, 503])
def test_control_transport_does_not_leak_upstream_errors(status):
    client = ControlClient("https://gateway.example", ADMIN)
    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, status, ADMIN, {}, None)
    client.opener = Opener()
    with pytest.raises(OperatorError) as error:
        client.request("GET", "/control")
    assert ADMIN not in str(error.value)


def test_control_timeout_reports_unknown_outcome():
    client = ControlClient("https://gateway.example", ADMIN)
    class Opener:
        def open(self, *args, **kwargs):
            raise URLError(ADMIN)
    client.opener = Opener()
    with pytest.raises(OperatorError, match="outcome unknown"):
        client.request("POST", "/visas", {})


def test_missing_operator_config_fails_cleanly(tmp_path):
    from nodi.operator import main
    with pytest.raises(SystemExit) as error:
        main(["--config", str(tmp_path / "missing.json"), "visas"])
    assert error.value.code == 1


def test_cli_against_real_server(tmp_path):
    import httpx
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"admin_token": ADMIN, "agent_tokens": {AGENT: "procurement"}, "database": Gateway(str(tmp_path / "http.db")).path}))
    operator_config = tmp_path / "operator.json"
    operator_config.write_text(json.dumps({"admin_token": ADMIN}))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    server = subprocess.Popen([sys.executable, "-m", "nodi", "serve", "--config", str(config), "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    def command(*args):
        result = subprocess.run([sys.executable, "-m", "nodi.operator", "--url", origin, "--config", str(operator_config), *args], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert "\x1b" not in result.stdout
        assert ADMIN not in result.stdout + result.stderr
        assert AGENT not in result.stdout + result.stderr
        return json.loads(result.stdout)
    try:
        with httpx.Client(base_url=origin, trust_env=False) as client:
            for _ in range(100):
                try:
                    if client.get('/health').status_code == 200: break
                except httpx.ConnectError: pass
                time.sleep(.05)
            else: pytest.fail("gateway did not start")
            path = tmp_path / "draft.json"
            command("draft", "--agent", "procurement", "--mission", "Buy a monitor under $300.", "--output", str(path))
            visa = command("issue", "--contract", str(path))["visa_id"]
            action = {"request_id": "live", "visa_id": visa, "tool": "purchase", "resource": "vendor:approved", "arguments": {"product_id": "monitor-pro", "quantity": 1}, "context": "untrusted\u001b[31mtext"}
            headers = {"Authorization": "Bearer " + AGENT}
            assert client.post('/execute', json=action, headers=headers).json()["decision"] == "ASK_HUMAN"
            review = command("inspect", "--agent", "procurement", "--request-id", "live")
            command("approve", "--agent", "procurement", "--request-id", "live", "--fingerprint", review["fingerprint"])
            assert client.post('/execute', json=action, headers=headers).json()["executed"]
            command("revoke", "--visa", visa)
            assert command("integrity")["valid"]
            assert len(command("audit", "--limit", "2")["audit"]) == 2
            denied = subprocess.run([sys.executable, "-m", "nodi.operator", "--url", origin, "visas"], env={**os.environ, "NODI_ADMIN_TOKEN": AGENT}, capture_output=True, text=True)
            assert denied.returncode == 1
            assert "403" in denied.stderr and AGENT not in denied.stderr
    finally:
        server.terminate(); server.wait(timeout=10)


def test_operator_uses_pages_not_snapshot(system):
    gateway, client = system
    pending(gateway)
    for args in (["visas", "--limit", "1"], ["approvals", "--limit", "1"], ["audit", "--limit", "1", "--agent", "procurement"], ["delegations", "--limit", "1"], ["inspect", "--agent", "procurement", "--request-id", "buy"]):
        run(parser().parse_args(args), client)
    assert all(path != "/control" for _, path, _ in client.calls)
    first = run(parser().parse_args(["audit", "--limit", "1"]), client)
    assert first["has_more"]
    second = run(parser().parse_args(["audit", "--limit", "1", "--after", str(first["next_after"]), "--through", str(first["through"])]), client)
    assert first["audit"][0]["sequence"] < second["audit"][0]["sequence"]
