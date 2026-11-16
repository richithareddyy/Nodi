"""Gateway/backend ordering, denial finality, failures and launch constraints."""
from dataclasses import replace
import json
import subprocess
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from nodi.api import create_app
from nodi.domains import get_domain
from nodi.gateway import Gateway
from nodi.models import Action, draft
from nodi.execution.docker import DockerBackend, ExecutionError, from_config

NOW = 2_000_000_000


class Backend:
    def __init__(self): self.calls = []
    def execute(self, action, expires_at):
        self.calls.append((action, expires_at))
        return {'simulation': False, 'backend': 'test-double', 'content': 'bounded read'}


def setup(tmp_path, backend=None):
    backend = backend or Backend()
    gateway = Gateway(str(tmp_path / 'ledger.db'), backend=backend, clock=lambda: NOW)
    contract = get_domain('sandbox').default_contract('reader', NOW)
    visa = gateway.issue(contract)['visa_id']
    action = Action('read', visa, 'filesystem.read', 'workspace:research', {'path': 'notes/source.txt'})
    return gateway, contract, action, backend


def test_real_reads_require_review_then_execute_once(tmp_path):
    gateway, contract, action, backend = setup(tmp_path)
    result = gateway.execute('reader', action)
    assert result['decision'] == 'ASK_HUMAN' and not backend.calls
    gateway.approve('reader', action.request_id, True)
    result = gateway.execute('reader', action)
    assert result['decision'] == 'ALLOW' and result['result']['simulation'] is False
    assert backend.calls[0][1] == NOW + 300
    assert gateway.execute('reader', action)['replayed']
    assert len(backend.calls) == 1 and gateway.verify_audit()
    assert gateway.snapshot()['visas'][0]['actions'] == 1


@pytest.mark.parametrize('change', ['revoke', 'forbid', 'expire', 'resource', 'identity', 'write', 'conflict'])
def test_hard_denials_never_reach_backend_after_approval(tmp_path, change):
    gateway, contract, action, backend = setup(tmp_path)
    gateway.execute('reader', action)
    gateway.approve('reader', action.request_id, True)
    principal = 'reader'
    if change == 'revoke': gateway.revoke(action.visa_id)
    elif change == 'forbid': gateway.modify(action.visa_id, replace(contract, forbidden_operations=['filesystem.read']))
    elif change == 'expire': gateway.clock = lambda: contract.expires_at
    elif change == 'resource': action = replace(action, request_id='other', resource='database:test')
    elif change == 'identity': principal = 'intruder'
    elif change == 'write': action = replace(action, request_id='other', tool='filesystem.write')
    elif change == 'conflict': action = replace(action, arguments={'path': 'other.txt'})
    assert gateway.execute(principal, action)['decision'] == 'DENY'
    assert not backend.calls and not gateway.snapshot()['effects']


def test_ancestor_restriction_and_usage_apply_to_real_reads(tmp_path):
    gateway, contract, action, backend = setup(tmp_path)
    parent = action.visa_id
    child = gateway.delegate('reader', action.visa_id, replace(contract, agent_id='child', delegation_depth=0))['visa_id']
    action = replace(action, visa_id=child)
    gateway.execute('child', action)
    gateway.approve('child', action.request_id, True)
    assert gateway.execute('child', action)['executed']
    assert all(v['actions'] == 1 for v in gateway.snapshot()['visas'])
    gateway.modify(parent, replace(contract, allowed_tools=[]))
    assert gateway.execute('child', replace(action, request_id='again'))['decision'] == 'DENY'
    assert len(backend.calls) == 1


def test_simulation_never_turns_real_and_disabled_backend_denies(tmp_path):
    gateway = Gateway(str(tmp_path / 'ledger.db'), clock=lambda: NOW)
    contract = get_domain('sandbox').default_contract('reader', NOW)
    visa = gateway.issue(contract)['visa_id']
    action = Action('system', visa, 'shell.read', 'runtime:info', {'command': 'system'})
    assert gateway.execute('reader', action)['decision'] == 'DENY'
    backend = Backend()
    gateway.backend = backend
    visa = gateway.issue(draft('Buy a monitor under $300.', 'reader', NOW))['visa_id']
    result = gateway.execute('reader', Action('search', visa, 'search_products', 'catalog:monitors', {'query': 'monitor'}))
    assert result['result']['simulation'] and not backend.calls


def test_execution_failure_is_503_without_false_allow_or_consumption(tmp_path):
    class Failure(Backend):
        def execute(self, *args): raise ExecutionError('read failed')
    gateway, contract, action, backend = setup(tmp_path, Failure())
    action = replace(action, tool='shell.read', resource='runtime:info', arguments={'command': 'system'})
    client = TestClient(create_app(gateway, 'a' * 32, {'b' * 32: 'reader'}))
    assert client.post('/execute', json=action.dictionary()).status_code == 401
    assert client.post('/execute', json=action.dictionary(), headers={'Authorization': 'Bearer ' + 'b' * 32}).status_code == 503
    state = gateway.snapshot()
    assert not state['effects'] and state['visas'][0]['actions'] == 0
    assert gateway.verify_audit()


def test_audit_failure_rolls_back_receipt_but_read_can_repeat(tmp_path, monkeypatch):
    gateway, contract, action, backend = setup(tmp_path)
    action = replace(action, tool='shell.read', resource='runtime:info', arguments={'command': 'system'})
    original = gateway._audit
    monkeypatch.setattr(gateway, '_audit', lambda *args: (_ for _ in ()).throw(RuntimeError('disk failure')))
    with pytest.raises(RuntimeError): gateway.execute('reader', action)
    assert len(backend.calls) == 1 and not gateway.snapshot()['effects']
    monkeypatch.setattr(gateway, '_audit', original)
    assert gateway.execute('reader', action)['executed']
    assert len(backend.calls) == 2  # Deliberately not an exactly-once claim for external reads.


def test_expiry_during_read_discards_result(tmp_path):
    gateway, contract, action, backend = setup(tmp_path)
    def execute(*args):
        gateway.clock = lambda: contract.expires_at
        return {'simulation': False}
    backend.execute = execute
    action = replace(action, tool='shell.read', resource='runtime:info', arguments={'command': 'system'})
    with pytest.raises(ExecutionError): gateway.execute('reader', action)
    assert not gateway.snapshot()['effects']


def test_docker_constraints_and_timeout_cleanup(tmp_path, monkeypatch):
    backend = DockerBackend(image='sha256:' + 'a' * 64, workspace=str(tmp_path), database=str(tmp_path), egress_volume='nodi-egress-test')
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'run': raise subprocess.TimeoutExpired(command, 15)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, 'run', run)
    action = Action('id', 'visa', 'filesystem.read', 'workspace:research', {'path': 'source.txt'})
    with pytest.raises(ExecutionError): backend.execute(action, NOW)
    command = calls[0]
    for flag in ('--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                 '--user=65532:65532', '--cpus=0.5', '--memory=128m', '--pids-limit=16', '--pull=never'):
        assert flag in command
    assert any('/workspace,readonly' in flag for flag in command)
    assert not any('/egress' in flag for flag in command)
    assert calls[1][:3] == ['docker', 'rm', '-f']


@pytest.mark.parametrize('value', [{}, {'image': 'latest'}, [], {'unknown': True}])
def test_invalid_operator_config_rejected(value):
    with pytest.raises(ValueError): from_config(value)
