import os
import stat
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from nodi.api import create_app
from nodi.gateway import Gateway
from nodi.models import draft
from nodi.storage import UnsafeStoreError, private_database


@pytest.mark.skipif(os.name != 'posix', reason='POSIX file security')
def test_private_creation_under_permissive_umask(tmp_path):
    path = tmp_path / 'state.db'
    code = 'import os,sys; from nodi.gateway import Gateway; os.umask(0); Gateway(sys.argv[1])'
    result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    Gateway(str(path))


@pytest.mark.parametrize('mode', [0o644, 0o660, 0o604, 0o602, 0o601])
def test_existing_exposed_file_unchanged(tmp_path, mode):
    path = tmp_path / 'state.db'
    path.write_bytes(b'preserve this content')
    path.chmod(mode)
    with pytest.raises(UnsafeStoreError): Gateway(str(path))
    assert path.read_bytes() == b'preserve this content'
    assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'directory', 'fifo'])
def test_unsafe_file_types(tmp_path, kind):
    path = tmp_path / 'state.db'
    target = tmp_path / 'target'
    target.write_bytes(b'untouched')
    target.chmod(0o600)
    if kind == 'symlink': path.symlink_to(target)
    elif kind == 'hardlink': os.link(target, path)
    elif kind == 'directory': path.mkdir()
    else: os.mkfifo(path, 0o600)
    with pytest.raises(UnsafeStoreError): Gateway(str(path))
    assert target.read_bytes() == b'untouched'


@pytest.mark.parametrize('suffix', ['-journal', '-wal', '-shm'])
def test_unsafe_existing_sidecar(tmp_path, suffix):
    path = tmp_path / 'state.db'
    Gateway(str(path))
    sidecar = tmp_path / ('state.db' + suffix)
    sidecar.write_bytes(b'preserved')
    sidecar.chmod(0o644)
    with pytest.raises(UnsafeStoreError): Gateway(str(path))
    assert sidecar.read_bytes() == b'preserved'


def test_permission_change_rechecked_and_api_sanitized(tmp_path):
    path = tmp_path / 'state.db'
    gateway = Gateway(str(path))
    client = TestClient(create_app(gateway, 'a'*40, {'b'*40: 'agent'}))
    path.chmod(0o644)
    with pytest.raises(UnsafeStoreError): gateway.issue(draft('Buy a monitor under $300.', 'agent'))
    with pytest.raises(UnsafeStoreError): gateway.verify_audit()
    response = client.get('/audit/checkpoint', headers={'Authorization': 'Bearer ' + 'a'*40})
    assert response.status_code == 503
    assert str(path) not in response.text
    path.chmod(0o600)
    assert gateway.snapshot()['visas'] == []


def test_deleted_database_not_silently_recreated(tmp_path):
    path = tmp_path / 'state.db'
    gateway = Gateway(str(path))
    path.unlink()
    with pytest.raises(UnsafeStoreError): gateway.verify_audit()
    assert not path.exists()


def test_sqlite_journal_inherits_private_mode(tmp_path):
    path = tmp_path / 'state.db'
    gateway = Gateway(str(path))
    with gateway.transaction() as db:
        db.execute("INSERT INTO audit(event,previous_hash,event_hash) VALUES ('{}','a','b')")
        journal = tmp_path / 'state.db-journal'
        assert journal.exists()
        assert journal.stat().st_mode & 0o077 == 0


def test_wrong_owner_rejected(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    Gateway(str(path))
    monkeypatch.setattr(os, 'getuid', lambda: path.stat().st_uid + 1)
    with pytest.raises(UnsafeStoreError): private_database(path)


@pytest.mark.parametrize('replacement', [False, True])
def test_unlinked_optional_journal_reopens_and_validates_replacement(tmp_path, monkeypatch, replacement):
    from nodi.storage import _check
    sidecar = tmp_path / 'ledger.db-journal'
    sidecar.write_bytes(b'journal')
    sidecar.chmod(0o600)
    original = os.fstat
    first = [True]
    def race(fd):
        if first[0]:
            first[0] = False
            sidecar.unlink()
            if replacement:
                sidecar.write_bytes(b'unsafe replacement')
                sidecar.chmod(0o644)
        return original(fd)
    monkeypatch.setattr(os, 'fstat', race)
    if replacement:
        with pytest.raises(UnsafeStoreError): _check(sidecar, optional=True)
    else:
        _check(sidecar, optional=True)
