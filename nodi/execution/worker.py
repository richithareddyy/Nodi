"""One bounded read in a networkless, read-only Docker container."""
import json
import os
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time

from ..json_input import validate_json
from .validation import COMMANDS, MAX_CONTENT, MAX_RESULT, relative_path, validate


def read_file(root, relative, limit=MAX_CONTENT):
    """Open every component relative to held directory fds; never follow symlinks."""
    parts = relative_path(relative).split('/')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise ValueError('only bounded regular files without hard links may be read')
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError('file exceeds limit')
            return data
    finally:
        os.close(fd)


def query_database(root, query):
    # A bounded snapshot avoids URI/path races and SQLite sidecar writes.
    data = read_file(root, 'test.db', 1024 * 1024)
    db = sqlite3.connect(':memory:')
    try:
        db.deserialize(data)
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_CONTENT)
        db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 2000)
        def authorize(op, first, second, database, trigger):
            if op == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if op == sqlite3.SQLITE_READ and (database == 'main' or (database is None and second == '')) and first == 'articles' and trigger is None:
                return sqlite3.SQLITE_OK
            if op == sqlite3.SQLITE_FUNCTION and second in ('count', 'length', 'lower', 'upper'):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        db.set_authorizer(authorize)
        steps = [0]
        def stop():
            steps[0] += 1
            return steps[0] > 1000
        db.set_progress_handler(stop, 100)
        cursor = db.execute(query)
        rows = cursor.fetchmany(101)
        if len(rows) > 100:
            raise ValueError('query exceeds row limit')
        result = {'columns': [c[0] for c in cursor.description], 'rows': rows}
        if len(json.dumps(result, allow_nan=False).encode()) > MAX_CONTENT:
            raise ValueError('query result exceeds byte limit')
        return result
    finally:
        db.close()


def proxy_get(url, expires_at):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(8)
        connection.connect('/egress/proxy.sock')
        connection.sendall(json.dumps({'url': url, 'expires_at': expires_at}).encode() + b'\n')
        with connection.makefile('rb') as stream:
            raw = stream.readline(MAX_RESULT + 1)
        if len(raw) > MAX_RESULT:
            raise ValueError('proxy response exceeds limit')
        validate_json(raw)
        result = json.loads(raw)
        if result.get('ok') is not True:
            raise ValueError('egress proxy rejected or failed the GET')
        return result['result']


def execute(tool, arguments, *, workspace='/workspace', database='/database', expires_at=None):
    validate(tool, arguments)
    if tool == 'filesystem.read':
        return {'path': arguments['path'], 'content': read_file(workspace, arguments['path']).decode('utf-8')}
    if tool == 'sqlite.query':
        return query_database(database, arguments['query'])
    if tool == 'http.get':
        return proxy_get(arguments['url'], expires_at if expires_at is not None else int(time.time()) + 10)
    output = subprocess.run(COMMANDS[arguments['command']], check=True, capture_output=True, timeout=2,
                            env={'PATH': '/usr/bin', 'LANG': 'C'}, cwd='/', shell=False)
    if len(output.stdout) > MAX_CONTENT:
        raise ValueError('command result too large')
    return {'command': arguments['command'], 'stdout': output.stdout.decode('utf-8')}


def main():
    # Wall-clock bound also survives loss of the gateway/docker CLI process.
    signal.alarm(10)
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('request too large')
        validate_json(raw)
        request = json.loads(raw)
        if type(request) is not dict or set(request) != {'tool', 'arguments', 'expires_at'}:
            raise ValueError('invalid worker request')
        expires_at = request.pop('expires_at')
        if type(expires_at) is not int or time.time() >= expires_at:
            raise ValueError('authority expired before read')
        remaining = expires_at - time.time()
        if remaining <= 0:
            raise ValueError('authority expired before read')
        signal.setitimer(signal.ITIMER_REAL, min(10, remaining))
        result = execute(**request, expires_at=expires_at)
        if time.time() >= expires_at:
            raise ValueError('authority expired during read')
        payload = json.dumps({'ok': True, 'result': result}, allow_nan=False)
        if len(payload.encode()) > MAX_RESULT:
            raise ValueError('result too large')
    except Exception:
        payload = json.dumps({'ok': False, 'error': 'sandbox read rejected or failed'})
    print(payload)


if __name__ == '__main__':
    main()
