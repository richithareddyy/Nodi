"""Durable test provider, independent of the authorization ledger. No real money."""
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

from .storage import private_database


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def payload_hash(payload):
    if (type(payload) is not dict or set(payload) != {'amount_cents', 'merchant', 'product_id', 'quantity'}
            or type(payload['amount_cents']) is not int or payload['amount_cents'] <= 0
            or payload['merchant'] != 'approved.example'
            or type(payload['product_id']) is not str or not re.fullmatch(r'[a-z-]{1,50}', payload['product_id'])
            or type(payload['quantity']) is not int or not 1 <= payload['quantity'] <= 100):
        raise ValueError('invalid payment payload')
    return hashlib.sha256(encoded(payload).encode()).hexdigest()


class MockPaymentProvider:
    modes = {'success', 'fail', 'timeout', 'lose_response', 'unavailable'}

    def __init__(self, path):
        self.path = os.fspath(path)
        private_database(self.path, create=True)
        self.identity = None
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS provider_identity (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS payments (
                    key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS modes (key TEXT PRIMARY KEY, mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls (sequence INTEGER PRIMARY KEY, key TEXT, method TEXT);
            ''')
            db.execute('INSERT INTO provider_identity SELECT ? WHERE NOT EXISTS (SELECT 1 FROM provider_identity)', (uuid4().hex,))
            self.identity = db.execute('SELECT id FROM provider_identity').fetchone()[0]

    @contextmanager
    def transaction(self):
        private_database(self.path)
        db = sqlite3.connect(self.path, isolation_level=None, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            if self.identity is not None and db.execute('SELECT id FROM provider_identity').fetchone()[0] != self.identity:
                raise ValueError('provider store identity changed')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def set_mode(self, mode, key='*'):
        if mode not in self.modes or (key != '*' and not re.fullmatch(r'[a-f0-9]{32}', key)):
            raise ValueError('invalid test provider mode or key')
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO modes VALUES (?,?)', (key, mode))

    @staticmethod
    def _mode(db, key):
        row = db.execute("SELECT mode FROM modes WHERE key IN (?, '*') ORDER BY key='*' LIMIT 1", (key,)).fetchone()
        return row[0] if row else 'success'

    def _operation(self, method, key, payload):
        if type(key) is not str or not re.fullmatch(r'[a-f0-9]{32}', key):
            raise ValueError('invalid idempotency key')
        digest = payload_hash(payload)
        lost = False
        with self.transaction() as db:
            mode = self._mode(db, key)
            if mode == 'unavailable':
                raise TimeoutError('mock provider unavailable')
            db.execute('INSERT INTO calls(key,method) VALUES (?,?)', (key, method))
            previous = db.execute('SELECT * FROM payments WHERE key=?', (key,)).fetchone()
            if previous:
                if previous['payload_hash'] != digest:
                    raise ValueError('idempotency key is bound to another payment')
                result = json.loads(previous['result'])
            elif method == 'lookup' or (method == 'charge' and mode == 'timeout'):
                result = {'status': 'ABSENT', 'key': key, 'payload_hash': digest}
                lost = method == 'charge'
            else:
                state = 'FAILED' if method == 'cancel' or mode == 'fail' else 'COMMITTED'
                result = {'status': state, 'key': key, 'payload_hash': digest,
                          'charged_cents': payload['amount_cents'] if state == 'COMMITTED' else 0,
                          'receipt': uuid4().hex, 'mock': True}
                # A terminal cancellation tombstone fences a late charge with this key.
                db.execute('INSERT INTO payments VALUES (?,?,?)', (key, digest, encoded(result)))
                lost = method == 'charge' and mode == 'lose_response'
        if lost:
            raise TimeoutError('mock provider response lost')
        return result

    def charge(self, key, payload): return self._operation('charge', key, payload)
    def lookup(self, key, payload): return self._operation('lookup', key, payload)
    def cancel(self, key, payload): return self._operation('cancel', key, payload)

    def snapshot(self):
        with self.transaction() as db:
            return {'payments': [json.loads(r['result']) for r in db.execute('SELECT * FROM payments ORDER BY key')],
                    'calls': [dict(r) for r in db.execute('SELECT * FROM calls ORDER BY sequence')]}


def from_config(value):
    if value is None:
        return None
    if type(value) is not dict or set(value) != {'database'} or type(value['database']) is not str:
        raise ValueError('mock_payments requires only a separate database path')
    return MockPaymentProvider(value['database'])
