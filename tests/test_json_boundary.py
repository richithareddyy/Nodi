import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from nodi.api import BodyLimit, create_app
from nodi.gateway import Gateway
from nodi.json_input import validate_json


@pytest.fixture
def system(tmp_path):
    gateway = Gateway(str(tmp_path / 'state.db'))
    return gateway, TestClient(create_app(gateway, 'a'*40, {'b'*40: 'agent'}))


@pytest.mark.parametrize('raw', [
    b'{"mission":"first","mission":"second"}',
    b'{"arguments":{"quantity":1,"quantity":2}}',
    b'{"mission":1,"missi\\u006fn":2}',
    b'{"score":NaN}', b'{"score":Infinity}', b'{"score":-Infinity}', b'{"score":1e999}',
    b'{"secret":"not-to-be-echoed",}', b'{"x":"\\ud800"}', b'{"\\udfff":1}',
    b'{"x":"\xff"}', b'[' * 65 + b'0' + b']' * 65,
    b'[' * 2000 + b'0' + b']' * 2000,
])
def test_invalid_json_never_reaches_state(system, raw):
    gateway, client = system
    response = client.post('/visas', content=raw, headers={'Authorization': 'Bearer ' + 'a'*40, 'Content-Type': 'application/json'})
    assert response.status_code == 400
    assert response.json() == {'detail': 'invalid or ambiguous JSON request'}
    assert gateway.snapshot() == {'visas': [], 'approvals': [], 'effects': [], 'audit': []}


@pytest.mark.parametrize('content_type', [None, 'text/plain', 'application/octet-stream'])
def test_non_json_body_rejected(system, content_type):
    _, client = system
    headers = {'Authorization': 'Bearer ' + 'a'*40}
    if content_type: headers['Content-Type'] = content_type
    assert client.post('/drafts', content=b'{}', headers=headers).status_code == 415


@pytest.mark.parametrize('content_type', ['application/json', 'application/json; charset=utf-8', 'application/vnd.nodi+json'])
def test_supported_json_and_unicode(system, content_type):
    _, client = system
    response = client.post('/drafts', content=json.dumps({'mission': 'Buy a monitor under $300.', 'agent_id': 'café'}).encode(), headers={'Content-Type': content_type, 'Authorization': 'Bearer ' + 'a'*40})
    assert response.status_code == 200
    assert response.json()['contract']['agent_id'] == 'café'


def test_limits_and_valid_repeated_values():
    validate_json(b'[' * 64 + b'0' + b']' * 64)
    validate_json(b'{"a":{"x":1},"b":{"x":1},"emoji":"\\ud83d\\ude00"}')


def test_chunked_duplicate_rejected_before_app():
    called, sent = [], []
    async def app(scope, receive, send): called.append(True)
    chunks = iter([{'type': 'http.request', 'body': b'{"a":1,', 'more_body': True},
                   {'type': 'http.request', 'body': b'"a":2}', 'more_body': False}])
    async def receive(): return next(chunks)
    async def send(message): sent.append(message)
    asyncio.run(BodyLimit(app)({'type': 'http', 'headers': [(b'content-type', b'application/json')]}, receive, send))
    assert not called and sent[0]['status'] == 400


def test_duplicate_content_type_rejected(system):
    _, client = system
    response = client.post('/drafts', content=b'{}', headers=[('Content-Type', 'application/json'), ('Content-Type', 'text/plain')])
    assert response.status_code == 415
