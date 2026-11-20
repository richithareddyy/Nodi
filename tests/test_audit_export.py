import copy
import hashlib
import json
import os
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from nodi.api import create_app
from nodi.audit_export import export_audit, save_checkpoint
from nodi.audit_verify import ROOT, Verifier, checkpoint, load_checkpoint, strict_json, verify
from nodi.gateway import Gateway
from nodi.models import draft
from nodi.operator import parser, run
from nodi.queries import ReadModel


@pytest.fixture
def system(tmp_path):
    gateway = Gateway(str(tmp_path / 'state.db'))
    reads = ReadModel(gateway)
    class Client:
        def request(self, method, path):
            if path == '/audit/checkpoint':
                return reads.checkpoint()
            params = {k: int(v[0]) for k, v in parse_qs(urlsplit(path).query).items()}
            return reads.audit(**params)
    return gateway, reads, Client()


def test_paginated_export_and_offline_cli(system, tmp_path):
    gateway, reads, client = system
    for _ in range(30):
        gateway.issue(draft('Buy a monitor under $300.', 'procurement'))
    anchor, output = tmp_path / 'checkpoint.json', tmp_path / 'audit.ndjson'
    run(parser().parse_args(['checkpoint', '--output', str(anchor)]), client)
    gateway.issue(draft('Buy a monitor under $300.', 'procurement'))
    result = run(parser().parse_args(['export-audit', '--checkpoint', str(anchor), '--output', str(output)]), client)
    assert result['sequence'] == 30
    assert reads.checkpoint()['sequence'] == 31
    assert anchor.stat().st_mode & 0o777 == output.stat().st_mode & 0o777 == 0o600
    env = {k: v for k, v in os.environ.items() if not k.startswith('NODI')}
    proc = subprocess.run([sys.executable, '-m', 'nodi.audit_verify', '--checkpoint', str(anchor), '--audit', str(output)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0 and json.loads(proc.stdout)['valid']
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        export_audit(client, anchor, output)
    assert output.read_bytes() == original
    assert not list(tmp_path.glob('.audit-*'))
    output.write_bytes(b'')
    proc = subprocess.run([sys.executable, '-m', 'nodi.audit_verify', '--checkpoint', str(anchor), '--audit', str(output)], env=env, capture_output=True)
    assert proc.returncode == 1


def records():
    rows, previous = [], ROOT
    for sequence in range(1, 4):
        event = {'timestamp': sequence, 'message': 'Monitor café'}
        digest = hashlib.sha256((previous + json.dumps(event, sort_keys=True, separators=(',', ':'))).encode()).hexdigest()
        rows.append(dict(sequence=sequence, event=event, previous_hash=previous, event_hash=digest))
        previous = digest
    return rows, dict(version=1, sequence=3, event_hash=previous)


@pytest.mark.parametrize('mutation', ['edit', 'rehash', 'missing', 'duplicate', 'reorder', 'truncate', 'extra', 'bool'])
def test_tampering(mutation):
    rows, anchor = records()
    if mutation in ('edit', 'rehash'):
        rows[0]['event']['message'] = 'Changed'
        if mutation == 'rehash':
            previous = ROOT
            for row in rows:
                row['previous_hash'] = previous
                row['event_hash'] = hashlib.sha256((previous + json.dumps(row['event'], sort_keys=True, separators=(',', ':'))).encode()).hexdigest()
                previous = row['event_hash']
    elif mutation == 'missing': del rows[1]
    elif mutation == 'duplicate': rows.insert(1, copy.deepcopy(rows[0]))
    elif mutation == 'reorder': rows.reverse()
    elif mutation == 'truncate': rows.pop()
    elif mutation == 'extra': rows.append(copy.deepcopy(rows[-1]))
    elif mutation == 'bool': rows[0]['sequence'] = True
    with pytest.raises(ValueError):
        verifier = Verifier(anchor)
        for row in rows: verifier.add(row)
        verifier.finish()


@pytest.mark.parametrize('raw', ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_strict_json(raw):
    with pytest.raises(ValueError): strict_json(raw)


@pytest.mark.parametrize('value', [dict(version=True, sequence=0, event_hash=ROOT), dict(version=1, sequence=True, event_hash=ROOT), dict(version=1, sequence=0, event_hash='f'*64), dict(version=1, sequence=-1, event_hash=ROOT)])
def test_invalid_checkpoint(value):
    with pytest.raises(ValueError): checkpoint(value)


def test_empty_and_size_limits(system, tmp_path):
    _, _, client = system
    anchor, output = tmp_path / 'checkpoint.json', tmp_path / 'audit.ndjson'
    save_checkpoint(client, anchor)
    export_audit(client, anchor, output)
    assert verify(output, load_checkpoint(anchor))['sequence'] == 0
    output.write_bytes(b'x' * (1024 * 1024 + 1))
    with pytest.raises(ValueError): verify(output, load_checkpoint(anchor))
    anchor.write_bytes(b' ' * 4097)
    with pytest.raises(ValueError): load_checkpoint(anchor)


def test_failed_export_not_published(system, tmp_path):
    gateway, _, client = system
    gateway.issue(draft('Buy a monitor under $300.', 'procurement'))
    anchor, output = tmp_path / 'checkpoint.json', tmp_path / 'audit.ndjson'
    save_checkpoint(client, anchor)
    with gateway.transaction() as db: db.execute('DELETE FROM audit')
    with pytest.raises(ValueError): export_audit(client, anchor, output)
    assert not output.exists() and not list(tmp_path.glob('.audit-*'))


def test_checkpoint_requires_admin(system):
    gateway, reads, _ = system
    app = TestClient(create_app(gateway, 'a'*40, {'b'*40: 'procurement'}))
    assert app.get('/audit/checkpoint').status_code == 401
    assert app.get('/audit/checkpoint', headers={'Authorization': 'Bearer ' + 'b'*40}).status_code == 403
    assert app.get('/audit/checkpoint', headers={'Authorization': 'Bearer ' + 'a'*40}).json() == reads.checkpoint()
