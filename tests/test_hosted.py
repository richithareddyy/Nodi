import pytest
from fastapi.testclient import TestClient

from nodi.hosted import create_hosted_app


def configure(monkeypatch, path):
    monkeypatch.setenv("NODI_ADMIN_TOKEN", "a" * 40)
    monkeypatch.setenv("NODI_AGENT_TOKEN", "b" * 40)
    monkeypatch.setenv("NODI_DB", str(path))


def test_hosted_restart_preserves_state_and_roles(monkeypatch, tmp_path):
    path = tmp_path / "state.db"
    configure(monkeypatch, path)
    human = {"Authorization": "Bearer " + "a" * 40}
    machine = {"Authorization": "Bearer " + "b" * 40}
    with TestClient(create_hosted_app()) as client:
        assert client.get("/").status_code == 200
        assert client.get("/visas").status_code == 401
        assert client.get("/visas", headers=machine).status_code == 403
        contract = client.post(
            "/drafts",
            headers=human,
            json={"agent_id": "procurement", "mission": "Buy a monitor under $300."},
        ).json()["contract"]
        visa = client.post("/visas", headers=human, json=contract).json()["visa_id"]
    with TestClient(create_hosted_app()) as client:
        assert client.get("/visas", headers=human).json()["items"][0]["id"] == visa
        assert client.get("/health").json()["status"] == "ok"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "key,value",
    [
        ("NODI_ADMIN_TOKEN", ""),
        ("NODI_AGENT_TOKEN", "short"),
        ("NODI_AGENT_TOKEN", "a" * 40),
        ("NODI_DB", "relative.db"),
        ("NODI_DB", ""),
    ],
)
def test_hosted_rejects_incomplete_configuration(monkeypatch, tmp_path, key, value):
    path = tmp_path / "state.db"
    configure(monkeypatch, path)
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError):
        create_hosted_app()
    assert not path.exists()


def test_hosted_dashboard_discloses_temporary_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("NODI_ADMIN_TOKEN", "a" * 40)
    monkeypatch.setenv("NODI_AGENT_TOKEN", "b" * 40)
    monkeypatch.setenv("NODI_DB", str(tmp_path / "demo.db"))
    with TestClient(create_hosted_app()) as client:
        page = client.get("/")
        assert "Temporary demo:" in page.text
        assert "HOSTED DEMO" in page.text
        assert "a" * 40 not in page.text
        assert "b" * 40 not in page.text
