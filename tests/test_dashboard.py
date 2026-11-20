from fastapi.testclient import TestClient

from nodi.api import create_app
from nodi.gateway import Gateway


def test_dashboard_has_no_credentials_or_authority(tmp_path):
    admin, agent = 'admin-' + 'x' * 40, 'agent-' + 'y' * 40
    client = TestClient(create_app(Gateway(str(tmp_path / 'dashboard.db')), admin, {agent: 'procurement'}))
    for path, mime in [('/', 'text/html'), ('/dashboard/app.js', 'text/javascript'), ('/dashboard/style.css', 'text/css')]:
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers['content-type'].startswith(mime)
        assert r.headers['cache-control'] == 'no-store'
        assert r.headers['x-frame-options'] == 'DENY'
        assert "connect-src 'self'" in r.headers['content-security-policy']
        assert "frame-ancestors 'none'" in r.headers['content-security-policy']
        assert admin not in r.text and agent not in r.text
    assert client.get('/visas').status_code == 401
    assert client.post('/approvals', json={}).status_code in (401, 422)
    assert client.get('/dashboard/.nodi.local.json').status_code == 404
    assert client.get('/dashboard/api.py').status_code == 404
    assert client.get('/dashboard/index.html').status_code == 404


def test_dashboard_review_flow_preserves_authority(tmp_path):
    admin, agent = 'admin-' + 'x' * 40, 'agent-' + 'y' * 40
    gateway = Gateway(str(tmp_path / 'flow.db'))
    client = TestClient(create_app(gateway, admin, {agent: 'procurement'}))
    human, machine = {'Authorization': 'Bearer ' + admin}, {'Authorization': 'Bearer ' + agent}
    contract = client.post('/drafts', headers=human, json={'mission': 'Buy a monitor under $300.', 'agent_id': 'procurement'}).json()['contract']
    visa = client.post('/visas', headers=human, json=contract).json()['visa_id']
    action = {'request_id': 'browser-request', 'visa_id': visa, 'tool': 'purchase', 'resource': 'vendor:approved', 'arguments': {'product_id': 'monitor-pro', 'quantity': 1}}
    assert client.post('/execute', headers=machine, json=action).json()['decision'] == 'ASK_HUMAN'
    inspected = client.get('/approvals/inspect?agent=procurement&request_id=browser-request', headers=human).json()
    review = {'agent_id': inspected['agent'], 'request_id': inspected['request_id'], 'approve': True, 'expected_fingerprint': inspected['fingerprint']}
    assert client.post('/approvals', headers=machine, json=review).status_code == 403
    assert client.post('/approvals', headers=human, json=review).status_code == 200
    assert not gateway.snapshot()['effects']
    assert client.post('/execute', headers=machine, json=inspected['action']).json()['executed']
    assert client.post('/execute', headers=machine, json=inspected['action']).json()['replayed']
    assert len(gateway.snapshot()['effects']) == 1


def test_share_assets_and_metadata(tmp_path):
    client = TestClient(create_app(Gateway(str(tmp_path / 'assets.db')), 'a' * 40, {'b' * 40: 'procurement'}))
    image = client.get('/dashboard/social-preview.png')
    assert image.status_code == 200 and image.content.startswith(b'\x89PNG')
    icon = client.get('/favicon.ico')
    assert icon.status_code == 200 and icon.content.startswith(b'\x00\x00\x01\x00')
    assert client.get('/dashboard/presentation.js').status_code == 200
    assert 'og:image' in client.get('/').text
