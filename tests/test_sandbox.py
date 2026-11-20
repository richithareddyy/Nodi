from fastapi import FastAPI
from fastapi.testclient import TestClient

from nodi.sandbox import Sandbox


def setup(**kwargs):
    sandbox = Sandbox(**kwargs)
    app = FastAPI()
    app.mount('/sandbox', sandbox)
    return sandbox, TestClient(app)


def headers(data, role='admin'):
    return {'X-Sandbox-ID': data['workspace'], 'Authorization': 'Bearer ' + data[role]}


def test_seeded_demo_and_review_execute_once():
    manager, client = setup()
    response = client.post('/sandbox/start')
    assert response.status_code == 201
    assert response.headers['cache-control'] == 'no-store'
    data = response.json()
    assert client.get('/sandbox/api/approvals', headers=headers(data)).json()['items'] == []
    action = dict(request_id='sample-blocked', visa_id=data['visa_id'], tool='purchase', resource='vendor:approved', arguments={'product_id':'monitor-ultra', 'quantity':1})
    blocked = client.post('/sandbox/api/execute', headers=headers(data, 'agent'), json=action).json()
    assert blocked['decision'] == 'DENY'
    assert blocked['remaining_budget_cents'] == 30000
    action.update(request_id='sample-review', arguments={'product_id':'monitor-pro', 'quantity':1})
    pending = client.post('/sandbox/api/execute', headers=headers(data, 'agent'), json=action).json()
    assert pending['decision'] == 'ASK_HUMAN'
    assert pending['remaining_budget_cents'] == 30000
    assert client.get('/sandbox/api/visas', headers=headers(data)).json()['items'][0]['spent'] == 0
    review = client.get('/sandbox/api/approvals/inspect?agent=procurement&request_id=sample-review', headers=headers(data)).json()
    body = dict(agent_id='procurement', request_id='sample-review', approve=True, expected_fingerprint=review['fingerprint'])
    assert client.post('/sandbox/api/approvals', headers=headers(data, 'agent'), json=body).status_code == 403
    assert client.post('/sandbox/api/approvals', headers=headers(data), json=body).status_code == 200
    assert client.get('/sandbox/api/visas', headers=headers(data)).json()['items'][0]['spent'] == 0
    result = client.post('/sandbox/api/execute', headers=headers(data, 'agent'), json=review['action'])
    assert result.json()['executed']
    assert result.json()['remaining_budget_cents'] == 2500
    assert client.post('/sandbox/api/execute', headers=headers(data, 'agent'), json=review['action']).json()['replayed']
    assert client.get('/sandbox/api/visas', headers=headers(data)).json()['items'][0]['spent'] == 27500
    action.update(request_id='over-budget', arguments={'product_id':'monitor-basic', 'quantity':1})
    denied = client.post('/sandbox/api/execute', headers=headers(data, 'agent'), json=action).json()
    assert denied['decision'] == 'DENY'
    assert denied['remaining_budget_cents'] == 2500
    assert denied['cost_cents'] == 18000
    for workspace in manager.workspaces.values():
        workspace.directory.cleanup()


def test_workspace_isolation_and_expiration(tmp_path):
    now = [0]
    manager, client = setup(clock=lambda: now[0], ttl=10)
    a = client.post('/sandbox/start').json()
    b = client.post('/sandbox/start').json()
    wrong = headers(a) | {'X-Sandbox-ID': b['workspace']}
    assert client.get('/sandbox/api/visas', headers=wrong).status_code == 401
    assert client.get('/sandbox/api/visas').status_code == 410
    assert client.get('/sandbox/api/visas/inspect?visa_id=' + a['visa_id'], headers=headers(b)).status_code == 404
    from pathlib import Path
    folder = Path(manager.workspaces[a['workspace']].directory.name)
    now[0] = 11
    assert client.get('/sandbox/api/visas', headers=headers(a)).status_code == 410
    assert not folder.exists()
    assert not manager.workspaces


def test_capacity_request_and_creation_limits():
    manager, client = setup(capacity=1, request_limit=1)
    data = client.post('/sandbox/start').json()
    assert client.post('/sandbox/start').status_code == 429
    assert client.get('/sandbox/api/visas', headers=headers(data)).status_code == 200
    assert client.get('/sandbox/api/visas', headers=headers(data)).status_code == 429
    manager.workspaces[data['workspace']].directory.cleanup()


def test_sandbox_tokens_cannot_access_owner_app(monkeypatch, tmp_path):
    from nodi.hosted import create_hosted_app
    monkeypatch.setenv('NODI_ADMIN_TOKEN', 'a' * 40)
    monkeypatch.setenv('NODI_AGENT_TOKEN', 'b' * 40)
    monkeypatch.setenv('NODI_DB', str(tmp_path / 'owner.db'))
    client = TestClient(create_hosted_app())
    data = client.post('/sandbox/start').json()
    assert client.get('/visas', headers=headers(data)).status_code == 401
    owner = {'Authorization': 'Bearer ' + 'a' * 40}
    assert client.get('/visas', headers=owner).json()['items'] == []
    assert client.get('/sandbox/api/visas', headers=owner | {'X-Sandbox-ID': data['workspace']}).status_code == 401
