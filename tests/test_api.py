import pytest
from fastapi.testclient import TestClient

from nodi.api import create_app
from nodi.gateway import Gateway

ADMIN, AGENT, OTHER = "admin-" + "x" * 40, "agent-" + "y" * 40, "other-" + "z" * 40


def auth(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Gateway(str(tmp_path / "api.db")), ADMIN, {AGENT: "a", OTHER: "b"}))


def issue(client):
    response = client.post("/drafts", json={"mission": "Buy a monitor under $300.", "agent_id": "a"}, headers=auth(ADMIN))
    return client.post("/visas", json=response.json()["contract"], headers=auth(ADMIN)).json()["visa_id"]


def test_admin_agent_separation(client):
    assert client.get("/control").status_code == 401
    assert client.get("/control", headers=auth(AGENT)).status_code == 403
    assert client.post("/visas", json={}, headers=auth(AGENT)).status_code == 403
    assert client.post("/approvals", json={}, headers=auth(AGENT)).status_code == 403
    assert client.get("/tools/purchase").status_code == 404


def test_end_to_end_approval(client):
    v = issue(client)
    body = {"request_id": "one", "visa_id": v, "tool": "purchase", "resource": "vendor:approved", "arguments": {"product_id": "monitor-pro", "quantity": 1}}
    assert client.post("/execute", json=body, headers=auth(OTHER)).json()["decision"] == "DENY"
    assert client.post("/execute", json=body, headers=auth(ADMIN)).status_code == 403
    assert client.post("/execute", json=body, headers=auth(AGENT)).json()["decision"] == "ASK_HUMAN"
    inspected = client.get("/approvals/inspect?agent=a&request_id=one", headers=auth(ADMIN)).json()
    review = {"agent_id": "a", "request_id": "one", "approve": True, "expected_fingerprint": inspected["fingerprint"]}
    assert client.post("/approvals", json=review, headers=auth(ADMIN)).status_code == 200
    assert client.post("/execute", json=body, headers=auth(AGENT)).json()["executed"]
    assert len(client.get("/control", headers=auth(ADMIN)).json()["effects"]) == 1


def test_extra_fields_and_body_limit(client):
    v = issue(client)
    body = {"request_id": "one", "visa_id": v, "tool": "purchase", "resource": "vendor:approved", "arguments": {"product_id": "monitor-pro", "quantity": 1}, "agent_id": "a"}
    assert client.post("/execute", json=body, headers=auth(AGENT)).status_code == 422
    assert client.post("/execute", content=b"x" * 70000, headers=auth(AGENT)).status_code == 413


def test_no_insecure_defaults(tmp_path):
    with pytest.raises(ValueError):
        create_app(Gateway(str(tmp_path / "bad.db")), "short", {AGENT: "a"})


def test_invalid_contract(client):
    assert client.post("/visas", json={"agent_id": "a", "mission": "Buy monitor", "spending_limit": True}, headers=auth(ADMIN)).status_code == 422


def test_approval_fingerprint_checked_by_api(client):
    visa = issue(client)
    action = {"request_id": "review", "visa_id": visa, "tool": "purchase", "resource": "vendor:approved", "arguments": {"product_id": "monitor-pro", "quantity": 1}}
    assert client.post("/execute", json=action, headers=auth(AGENT)).json()["decision"] == "ASK_HUMAN"
    request = client.get("/control", headers=auth(ADMIN)).json()["approvals"][0]
    body = {"agent_id": "a", "request_id": "review", "approve": True, "expected_fingerprint": "0" * 64}
    assert client.post("/approvals", json=body, headers=auth(ADMIN)).status_code == 422
    body["expected_fingerprint"] = request["fingerprint"]
    assert client.post("/approvals", json=body, headers=auth(AGENT)).status_code == 403
    assert client.post("/approvals", json=body, headers=auth(ADMIN)).status_code == 200


@pytest.mark.parametrize("path", ["/audit/events", "/visas", "/approvals", "/approvals/inspect?agent=a&request_id=one"])
def test_bounded_reads_require_admin(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers=auth(AGENT)).status_code == 403


@pytest.mark.parametrize("query", ["limit=101", "limit=0", "after=-1", "through=-1", "after=9223372036854775808", "decision=PERMIT"])
def test_bad_audit_query_is_rejected(client, query):
    assert client.get("/audit/events?" + query, headers=auth(ADMIN)).status_code == 422


def test_single_review_lookup_is_exact_and_missing_is_404(client):
    issue(client)
    assert client.get("/approvals/inspect?agent=a&request_id=missing", headers=auth(ADMIN)).status_code == 404
    page = client.get("/visas?limit=1", headers=auth(ADMIN)).json()
    assert len(page["items"]) == 1 and page["next_after"] is None


@pytest.mark.parametrize('approve', [True, False])
@pytest.mark.parametrize('fingerprint', ['missing', None, '', 'A'*64, 'a'*63, 123, '0'*64])
def test_http_review_requires_exact_fingerprint_without_mutation(client, approve, fingerprint):
    visa = issue(client)
    action = {'request_id': 'bound', 'visa_id': visa, 'tool': 'purchase', 'resource': 'vendor:approved',
              'arguments': {'product_id': 'monitor-pro', 'quantity': 1}}
    assert client.post('/execute', json=action, headers=auth(AGENT)).json()['decision'] == 'ASK_HUMAN'
    before = client.get('/control', headers=auth(ADMIN)).json()
    body = {'agent_id': 'a', 'request_id': 'bound', 'approve': approve}
    if fingerprint != 'missing': body['expected_fingerprint'] = fingerprint
    assert client.post('/approvals', json=body, headers=auth(ADMIN)).status_code == 422
    assert client.get('/control', headers=auth(ADMIN)).json() == before


@pytest.mark.parametrize('approve', [True, False])
def test_inspected_review_applies_once_and_records_hash(client, approve):
    visa = issue(client)
    action = {'request_id': 'review-once', 'visa_id': visa, 'tool': 'purchase', 'resource': 'vendor:approved',
              'arguments': {'product_id': 'monitor-pro', 'quantity': 1}}
    client.post('/execute', json=action, headers=auth(AGENT))
    inspected = client.get('/approvals/inspect?agent=a&request_id=review-once', headers=auth(ADMIN)).json()
    body = {'agent_id': 'a', 'request_id': 'review-once', 'approve': approve, 'expected_fingerprint': inspected['fingerprint']}
    assert client.post('/approvals', json=body, headers=auth(ADMIN)).status_code == 200
    assert client.post('/approvals', json=body, headers=auth(ADMIN)).status_code == 422
    state = client.get('/control', headers=auth(ADMIN)).json()
    assert not state['effects']
    reviews = [row['event'] for row in state['audit'] if row['event']['event'] == 'human_review']
    assert len(reviews) == 1 and reviews[0]['fingerprint'] == inspected['fingerprint']
    result = client.post('/execute', json=action, headers=auth(AGENT)).json()
    assert result['decision'] == ('ALLOW' if approve else 'DENY')


def test_fingerprint_for_another_action_is_rejected(client):
    visa = issue(client)
    for request_id in ('first', 'second'):
        action = {'request_id': request_id, 'visa_id': visa, 'tool': 'purchase', 'resource': 'vendor:approved',
                  'arguments': {'product_id': 'monitor-pro', 'quantity': 1}}
        client.post('/execute', json=action, headers=auth(AGENT))
    inspected = client.get('/approvals/inspect?agent=a&request_id=first', headers=auth(ADMIN)).json()
    body = {'agent_id': 'a', 'request_id': 'second', 'approve': True, 'expected_fingerprint': inspected['fingerprint']}
    assert client.post('/approvals', json=body, headers=auth(ADMIN)).status_code == 422
    state = client.get('/control', headers=auth(ADMIN)).json()
    assert all(row['approval'] == 'pending' for row in state['approvals'])
    assert not state['effects']
