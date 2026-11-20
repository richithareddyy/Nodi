from dataclasses import replace

import pytest

from nodi.demo import run
from nodi.gateway import Gateway
from nodi.models import Action, draft


@pytest.fixture(scope='module')
def scenarios():
    return {result['scenario']: result for result in run()}


def test_demo_mission_drift_is_semantic_not_tool_denial(scenarios):
    result = scenarios['mission-drift']
    assert result['decision'] == 'DENY' and not result['executed']
    assert result['policy_checks'] and all(check['passed'] for check in result['policy_checks'])
    assert result['intent']['recommended_decision'] == 'DENY'


def test_demo_delegation_reaches_budget_attenuation(scenarios):
    result = scenarios['delegation-escalation']
    assert result['decision'] == 'DENY'
    assert result['reason'] == 'delegation widens spending_limit'


def test_demo_normal_actions_and_one_time_approval(scenarios):
    assert scenarios['normal-search']['executed']
    assert scenarios['normal-comparison']['executed']
    assert scenarios['human-approval']['decision'] == 'ASK_HUMAN'
    assert not scenarios['human-approval']['executed']
    assert scenarios['after-human-approval']['executed']
    assert scenarios['after-human-approval']['human_intervention']
    assert scenarios['constraint-violation']['decision'] == 'DENY'
    assert scenarios['prompt-injection']['decision'] == 'DENY'
    assert any(not check['passed'] for check in scenarios['prompt-injection']['policy_checks'])


def test_three_agent_chain_charges_every_ancestor_and_inherits_revocation(tmp_path):
    gateway = Gateway(str(tmp_path / 'acceptance.db'))
    root_contract = replace(draft('Buy a monitor under $300.', 'a'), delegation_depth=2)
    root = gateway.issue(root_contract)['visa_id']
    child_contract = replace(root_contract, agent_id='b', delegation_depth=1,
                             spending_limit=20000, maximum_transaction=20000, approval_threshold=20000)
    child = gateway.delegate('a', root, child_contract)['visa_id']
    leaf_contract = replace(child_contract, agent_id='c', delegation_depth=0,
                            spending_limit=19000, maximum_transaction=19000, approval_threshold=19000)
    leaf = gateway.delegate('b', child, leaf_contract)['visa_id']
    action = Action('purchase', leaf, 'purchase', 'vendor:approved', {'product_id':'monitor-basic', 'quantity':1})
    assert gateway.execute('c', action)['executed']
    state = gateway.snapshot()
    assert all(row['spent'] == 18000 and row['actions'] == 1 for row in state['visas'])
    assert state['audit'][-1]['event']['delegation_chain'] == [leaf, child, root]
    gateway.revoke(root)
    result = gateway.execute('c', Action('after', leaf, 'search_products', 'catalog:monitors', {'query':'monitor'}))
    assert result['decision'] == 'DENY'
    assert len(gateway.snapshot()['effects']) == 1 and gateway.verify_audit()
