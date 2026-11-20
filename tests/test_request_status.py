from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from nodi.api import create_app
from nodi.gateway import Gateway
from nodi.models import Action, draft


@pytest.fixture
def system(tmp_path):
    now = [2000000000]
    gateway = Gateway(str(tmp_path / 'state.db'), clock=lambda: now[0])
    contract = draft('Buy a monitor under $300.', 'a', now[0])
    visa = gateway.issue(contract)['visa_id']
    client = TestClient(create_app(gateway, 'x'*40, {'a'*40: 'a', 'b'*40: 'b'}))
    return gateway, client, contract, visa, now


def status(client, key='buy', token='a', extra=''):
    return client.get('/requests/status?request_id=' + key + extra, headers={'Authorization': 'Bearer ' + token*40})


def purchase(visa):
    return Action('buy', visa, 'purchase', 'vendor:approved', {'product_id': 'monitor-pro', 'quantity': 1})


def test_identity_scope_and_unknown_request(system):
    gateway, client, _, visa, _ = system
    gateway.execute('a', purchase(visa))
    assert client.get('/requests/status?request_id=buy').status_code == 401
    assert status(client, token='x').status_code == 403
    assert status(client, token='b').status_code == 404
    assert status(client, key='missing').status_code == 404
    assert status(client, token='b', extra='&agent_id=a').status_code == 404
    assert status(client).headers['cache-control'] == 'no-store'
    assert status(client, token='b').headers['cache-control'] == 'no-store'


def test_same_id_different_principals(system):
    gateway, client, contract, visa, _ = system
    other = gateway.issue(replace(contract, agent_id='b'))['visa_id']
    gateway.execute('a', purchase(visa))
    gateway.execute('b', Action('buy', other, 'search_products', 'catalog:monitors', {'query': 'monitor'}))
    assert status(client).json()['state'] == 'pending'
    assert status(client, token='b').json()['last_response']['visa_id'] == other
    assert status(client, token='b').json()['state'] == 'completed'


@pytest.mark.parametrize('change,expected', [('approve', 'approved'), ('reject', 'rejected'), ('revoke', 'invalidated'), ('modify', 'invalidated'), ('expire', 'pending')])
def test_review_state_and_read_without_effects(system, change, expected):
    gateway, client, contract, visa, now = system
    gateway.execute('a', purchase(visa))
    if change == 'approve': gateway.approve('a', 'buy', True)
    elif change == 'reject': gateway.approve('a', 'buy', False)
    elif change == 'revoke': gateway.revoke(visa)
    elif change == 'modify': gateway.modify(visa, replace(contract, max_actions=10))
    else: now[0] += 300
    before = gateway.snapshot()
    view = status(client).json()
    assert view['state'] == expected
    assert view['last_response']['decision'] == 'ASK_HUMAN'
    assert view['review_expired'] == (change == 'expire')
    assert view['review_deadline'] is not None if expected in ('pending', 'approved') else view['review_deadline'] is None
    assert gateway.snapshot() == before and not before['effects']


def test_completed_result_after_revocation_is_historical(system):
    gateway, client, _, visa, _ = system
    action = Action('buy', visa, 'search_products', 'catalog:monitors', {'query': 'monitor'})
    result = gateway.execute('a', action)
    gateway.revoke(visa)
    before = gateway.snapshot()
    view = status(client).json()
    assert view['state'] == 'completed' and view['last_response'] == result
    assert view['review_deadline'] is None and not view['review_expired']
    assert gateway.snapshot() == before and len(before['effects']) == 1


def test_completed_denial_is_available(system):
    gateway, client, _, visa, _ = system
    gateway.execute('a', Action('buy', visa, 'shell', 'vendor:approved', {}))
    view = status(client).json()
    assert view['state'] == 'completed' and view['last_response']['decision'] == 'DENY'


@pytest.mark.parametrize('key', ['', 'a.b', 'x'*101, '%27OR%201=1'])
def test_invalid_identifiers(system, key):
    assert status(system[1], key=key).status_code == 422
