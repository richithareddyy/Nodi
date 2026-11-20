"""Opt-in real-container checks. CI enables these after building the worker image."""

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from nodi.api import create_app
from nodi.domains import get_domain
from nodi.execution.docker import DockerBackend
from nodi.gateway import Gateway
from nodi.models import Action

IMAGE = os.environ.get("NODI_TEST_SANDBOX_IMAGE")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="set NODI_TEST_SANDBOX_IMAGE to a locally built image ID"
)


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path / "workspace"
    database = tmp_path / "database"
    workspace.mkdir(mode=0o755)
    database.mkdir(mode=0o755)
    (workspace / "source.txt").write_text("Read-only research source.\n")
    (workspace / "escape").symlink_to("/etc/passwd")
    with sqlite3.connect(database / "test.db") as db:
        db.execute("CREATE TABLE articles(title TEXT)")
        db.execute("INSERT INTO articles VALUES ('SQLite')")
    backend = DockerBackend(
        image=IMAGE,
        workspace=str(workspace),
        database=str(database),
        egress_volume=os.environ.get("NODI_TEST_EGRESS_VOLUME", "nodi-egress-local"),
    )
    gateway = Gateway(str(tmp_path / "ledger.db"), backend=backend)
    contract = get_domain("sandbox").default_contract("researcher", int(time.time()))
    visa = gateway.issue(contract)["visa_id"]
    client = TestClient(create_app(gateway, "a" * 32, {"b" * 32: "researcher"}))
    return gateway, backend, client, visa


def submit(runtime, request_id, tool, resource, arguments):
    gateway, _, client, visa = runtime
    action = Action(request_id, visa, tool, resource, arguments)
    headers = {"Authorization": "Bearer " + "b" * 32}
    result = client.post("/execute", headers=headers, json=action.dictionary())
    if result.status_code == 200 and result.json()["decision"] == "ASK_HUMAN":
        review = client.get(
            "/approvals/inspect",
            params={"agent": "researcher", "request_id": request_id},
            headers={"Authorization": "Bearer " + "a" * 32},
        ).json()
        response = client.post(
            "/approvals",
            json={
                "agent_id": "researcher",
                "request_id": request_id,
                "approve": True,
                "expected_fingerprint": review["fingerprint"],
            },
            headers={"Authorization": "Bearer " + "a" * 32},
        )
        assert response.status_code == 200
        result = client.post("/execute", headers=headers, json=action.dictionary())
    return result


def test_real_tools_via_authenticated_gateway(runtime):
    gateway, backend, client, visa = runtime
    source = Path(backend.workspace) / "source.txt"
    before = source.read_bytes()
    cases = [
        ("file", "filesystem.read", "workspace:research", {"path": "source.txt"}),
        ("sql", "sqlite.query", "database:test", {"query": "SELECT title FROM articles"}),
        ("system", "shell.read", "runtime:info", {"command": "system"}),
        ("date", "shell.read", "runtime:info", {"command": "date"}),
    ]
    for request_id, tool, resource, arguments in cases:
        response = submit(runtime, request_id, tool, resource, arguments)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["executed"] and result["result"]["simulation"] is False
        assert submit(runtime, request_id, tool, resource, arguments).json()["replayed"]
    assert len(gateway.snapshot()["effects"]) == 4
    assert source.read_bytes() == before and gateway.verify_audit()


def test_real_worker_refuses_symlink_write_and_sql_mutation(runtime):
    gateway, _, client, visa = runtime
    assert (
        submit(
            runtime, "link", "filesystem.read", "workspace:research", {"path": "escape"}
        ).status_code
        == 503
    )
    assert (
        submit(
            runtime,
            "sqlbad",
            "sqlite.query",
            "database:test",
            {"query": "SELECT load_extension('/tmp/x')"},
        ).status_code
        == 503
    )
    for tool, args in [
        ("filesystem.write", {"path": "source.txt", "content": "changed"}),
        ("shell.read", {"command": "system; cat /etc/passwd"}),
    ]:
        result = submit(runtime, uuid4().hex, tool, "workspace:research", args)
        assert result.status_code == 200 and result.json()["decision"] == "DENY"
    assert not gateway.snapshot()["effects"]


def test_container_has_no_network_capabilities_or_writable_mounts(runtime):
    _, backend, _, _ = runtime
    name = "nodi-read-probe-" + uuid4().hex
    command = backend.command(name, "filesystem.read")
    # Trusted test code, never reachable through an agent action.
    code = """
import json, os, socket
result = {'uid': os.getuid(), 'routes': open('/proc/net/route').read().splitlines()[1:]}
for address in [('1.1.1.1', 443), ('169.254.169.254', 80)]:
    try:
        with socket.create_connection(address, timeout=1):
            result[address[0]] = 'connected'
    except OSError:
        result[address[0]] = 'blocked'
for control in ('memory.max', 'pids.max', 'cpu.max'):
    result[control] = open('/sys/fs/cgroup/' + control).read().strip()
status = open('/proc/self/status').read()
result['no_privileges'] = 'NoNewPrivs:\\t1' in status
result['no_capabilities'] = 'CapEff:\\t0000000000000000' in status
for path in ['/probe', '/workspace/probe']:
    try:
        open(path, 'w').close()
        result[path] = 'writable'
    except OSError:
        result[path] = 'read-only'
print(json.dumps(result))
"""
    command = command[:-3] + ["-B", "-c", code]
    response = subprocess.run(command, capture_output=True, text=True, timeout=20, check=True)
    result = json.loads(response.stdout)
    assert result["uid"] == 65532 and result["routes"] == []
    assert result["1.1.1.1"] == result["169.254.169.254"] == "blocked"
    assert result["memory.max"] == str(128 * 1024 * 1024)
    assert result["pids.max"] == "16"
    quota, period = map(int, result["cpu.max"].split())
    assert quota / period == 0.5
    assert result["no_privileges"] and result["no_capabilities"]
    assert result["/probe"] == result["/workspace/probe"] == "read-only"


def test_proxy_rejects_private_url_without_network_request(runtime):
    _, backend, _, _ = runtime
    command = backend.command("nodi-read-proxy-" + uuid4().hex, "http.get")
    code = """
import json, socket, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
s.connect('/egress/proxy.sock')
s.sendall(json.dumps({'url': 'https://127.0.0.1/', 'expires_at': int(time.time()) + 10}).encode() + b'\\n')
print(s.makefile().readline())
"""
    response = subprocess.run(
        command[:-3] + ["-B", "-c", code], capture_output=True, text=True, timeout=20, check=True
    )
    assert json.loads(response.stdout)["ok"] is False


@pytest.mark.skipif(
    os.environ.get("NODI_TEST_PUBLIC_HTTP") != "1",
    reason="public internet smoke test is explicitly opt-in",
)
def test_public_get_through_proxy(runtime):
    result = submit(
        runtime,
        "web",
        "http.get",
        "web:www.sqlite.org",
        {"url": "https://www.sqlite.org/index.html"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["result"]["status"] == 200
    assert "SQLite" in result.json()["result"]["content"]
