from dataclasses import replace

import pytest

from nodi.gateway import Gateway
from nodi.intent import ProcurementEvaluator
from nodi.models import Action, draft


@pytest.fixture
def system(tmp_path):
    now = [2000000000]
    gateway = Gateway(str(tmp_path / 'state.db'), clock=lambda: now[0])
    contract = replace(draft('Buy a monitor under $300.', 'a', now[0]), expires_at=now[0] + 1000)
    visa = gateway.issue(contract)['visa_id']
    return gateway, contract, visa, now


def slow(gateway, now, finish, fail=False):
    class Evaluator:
        def evaluate(self, contract, action, prepared):
            now[0] = finish
            if fail:
                raise TimeoutError('service unavailable')
            return ProcurementEvaluator().evaluate(contract, action, prepared)
    gateway.evaluator = Evaluator()


def search(visa, context=''):
    return Action('search', visa, 'search_products', 'catalog:monitors', {'query': 'monitor'}, context)


def assert_no_effect(gateway, response):
    assert response['decision'] == 'DENY' and not response['executed']
    state = gateway.snapshot()
    assert not state['effects']
    assert all(v['actions'] == 0 and v['spent'] == 0 for v in state['visas'])
    assert state['audit'][-1]['event']['decision'] == 'DENY'
    assert gateway.verify_audit()


@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_visa_expiry_after_evaluation(system, offset):
    gateway, contract, visa, now = system
    slow(gateway, now, contract.expires_at + offset)
    result = gateway.execute('a', search(visa))
    if offset < 0:
        assert result['executed']
    else:
        assert_no_effect(gateway, result)
        assert any(c['check'] == 'unexpired_after_evaluation' and not c['passed'] for c in result['policy_checks'])
        assert gateway.execute('a', search(visa))['replayed']


@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_approved_request_expires_during_evaluation(system, offset):
    gateway, _, visa, now = system
    action = Action('buy', visa, 'purchase', 'vendor:approved', {'product_id': 'monitor-pro', 'quantity': 1})
    pending = gateway.execute('a', action)
    gateway.approve('a', 'buy', True)
    slow(gateway, now, pending['approval_deadline'] + offset)
    result = gateway.execute('a', action)
    if offset < 0:
        assert result['executed'] and result['human_intervention']
    else:
        assert_no_effect(gateway, result)
        assert not result['human_intervention']


@pytest.mark.parametrize('elapsed', [299, 300, 301])
def test_failed_evaluator_cannot_create_expired_review(system, elapsed):
    gateway, _, visa, now = system
    slow(gateway, now, now[0] + elapsed, fail=True)
    result = gateway.execute('a', search(visa))
    if elapsed < 300:
        assert result['decision'] == 'ASK_HUMAN'
    else:
        assert_no_effect(gateway, result)
        assert not gateway.snapshot()['approvals']


def test_ancestor_shortened_expiry_checked_after_evaluation(system):
    gateway, contract, parent, now = system
    child = gateway.delegate('a', parent, replace(contract, agent_id='b', delegation_depth=0))['visa_id']
    gateway.modify(parent, replace(contract, expires_at=now[0] + 20))
    slow(gateway, now, now[0] + 20)
    result = gateway.execute('b', search(child))
    assert_no_effect(gateway, result)
    checks = {c['visa_id']: c['passed'] for c in result['policy_checks'] if c['check'] == 'unexpired_after_evaluation'}
    assert checks == {parent: False, child: True}
