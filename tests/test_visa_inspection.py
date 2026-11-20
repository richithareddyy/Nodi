from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from nodi.api import create_app
from nodi.gateway import Gateway
from nodi.models import Action, draft
from nodi.operator import parser, run
from nodi.queries import ReadModel


@pytest.fixture
def system(tmp_path):
    now = [2000000000]
    gateway = Gateway(str(tmp_path / 'state.db'), clock=lambda: now[0])
    contract = draft('Buy a monitor under $300.', 'parent', now[0])
    parent = gateway.issue(contract)['visa_id']
    child = gateway.delegate('parent', parent, replace(contract, agent_id='child', delegation_depth=0))['visa_id']
    return gateway, ReadModel(gateway), contract, parent, child, now


def test_shared_usage_and_read_only_snapshot(system):
    gateway, reads, _, parent, child, _ = system
    before = gateway.snapshot()
    view = reads.inspect_visa(child)
    assert [node['id'] for node in view['chain']] == [child, parent]
    assert gateway.snapshot() == before
    assert view['effective_limits']['remaining_budget_cents'] == 30000
    assert not view['blockers']
    gateway.execute('parent', Action('buy', parent, 'purchase', 'vendor:approved', {'product_id': 'monitor-basic', 'quantity': 1}))
    view = reads.inspect_visa(child)
    assert view['effective_limits']['remaining_budget_cents'] == 12000
    assert view['chain'][0]['spent'] == 0 and view['chain'][1]['spent'] == 18000
    assert view['effective_limits']['remaining_actions'] == 29


def test_pending_review_does_not_reserve_budget(system):
    gateway, reads, _, _, child, _ = system
    result = gateway.execute('child', Action('buy', child, 'purchase', 'vendor:approved', {'product_id': 'monitor-pro', 'quantity': 1}))
    assert result['decision'] == 'ASK_HUMAN'
    assert reads.inspect_visa(child)['effective_limits']['remaining_budget_cents'] == 30000


def test_parent_restrictions_and_expiry(system):
    gateway, reads, contract, parent, child, now = system
    gateway.modify(parent, replace(contract, allowed_tools=['search_products'],
                                   allowed_resources=['catalog:monitors'], delegation_depth=0,
                                   expires_at=now[0]+10, maximum_transaction=15000, approval_threshold=10000))
    now[0] += 10
    view = reads.inspect_visa(child)
    assert view['effective_limits']['allowed_tools'] == ['search_products']
    assert view['effective_limits']['allowed_resources'] == ['catalog:monitors']
    assert view['effective_limits']['maximum_transaction_cents'] == 15000
    assert view['effective_limits']['approval_threshold_cents'] == 10000
    assert view['blockers'] == [{'visa_id': parent, 'reason': 'expired'}, {'visa_id': parent, 'reason': 'delegation_depth'}]
    gateway.revoke(parent)
    assert {'visa_id': parent, 'reason': 'inactive'} in reads.inspect_visa(child)['blockers']


def test_usage_above_edited_caps_clamps_to_zero(system):
    gateway, reads, contract, parent, child, _ = system
    gateway.execute('child', Action('buy', child, 'purchase', 'vendor:approved', {'product_id': 'monitor-basic', 'quantity': 1}))
    gateway.modify(parent, replace(contract, spending_limit=10000, maximum_transaction=10000, approval_threshold=10000, max_actions=1))
    view = reads.inspect_visa(child)
    assert view['effective_limits']['remaining_budget_cents'] == 0
    assert view['effective_limits']['remaining_actions'] == 0
    assert {'visa_id': parent, 'reason': 'action_limit'} in view['blockers']


def test_forbidden_sets_override_allowed_sets(system):
    gateway, reads, contract, parent, child, _ = system
    gateway.modify(parent, replace(contract, forbidden_operations=['purchase'], forbidden_resources=['vendor:approved']))
    view = reads.inspect_visa(child)
    assert 'purchase' not in view['effective_limits']['allowed_tools']
    assert 'vendor:approved' not in view['effective_limits']['allowed_resources']


def test_admin_api_and_operator(system):
    gateway, reads, _, _, child, _ = system
    client = TestClient(create_app(gateway, 'a'*40, {'b'*40: 'child'}))
    path = '/visas/inspect?visa_id=' + child
    assert client.get(path).status_code == 401
    assert client.get(path, headers={'Authorization': 'Bearer ' + 'b'*40}).status_code == 403
    class Client:
        def request(self, method, path):
            return client.request(method, path, headers={'Authorization': 'Bearer ' + 'a'*40}).json()
    assert run(parser().parse_args(['inspect-visa', '--visa', child]), Client()) == reads.inspect_visa(child)
    assert client.get('/visas/inspect?visa_id=unknown', headers={'Authorization': 'Bearer ' + 'a'*40}).status_code == 404
    assert client.get('/visas/inspect?visa_id=', headers={'Authorization': 'Bearer ' + 'a'*40}).status_code == 422
