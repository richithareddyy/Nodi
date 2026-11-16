"""Optional PostgreSQL ledger with ancestor locks and a global audit-order lock.

Only static application SQL is adapted here; tool-supplied SQL never enters this
adapter. All data values remain driver parameters. No transaction/effect retries.
"""
from contextlib import contextmanager
from decimal import Decimal
import atexit
import os
import threading
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


class StoreError(sqlite3.OperationalError):
    """Retain the public storage-error contract without exposing DSNs or parameters."""


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if type(key) is int else super().__getitem__(key)


def rows(cursor):
    names=[column.name for column in cursor.description] if cursor.description else []
    def make(values):
        result=Row()
        for name,value in zip(names,values):
            if name == "rowid": continue
            if isinstance(value,Decimal):
                if value != value.to_integral_value(): raise StoreError("non-integer ledger aggregate")
                value=int(value)
            result[name]=value
        return result
    return make


def sql(statement):
    statement = re.sub(r"json_extract\(([\w.]+), '\$\.(\w+)'\)", r"(\1::jsonb ->> '\2')", statement)
    statement = statement.replace('INSERT OR IGNORE INTO', 'INSERT INTO') + (' ON CONFLICT DO NOTHING' if 'INSERT OR IGNORE INTO' in statement else '')
    if 'CREATE TABLE' in statement:
        statement = statement.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'BIGINT PRIMARY KEY')
        statement = re.sub(r'\bINTEGER\b', 'BIGINT', statement)
        statement = re.sub(r'(CREATE TABLE IF NOT EXISTS (?:visas|requests)\s*\()', r'\1 rowid BIGSERIAL UNIQUE,', statement)
    # Application statements use ? only for parameters, never within SQL literals.
    return statement.replace('?', '%s')


class Connection:
    def __init__(self, db, readonly=False):
        import psycopg
        self.driver=psycopg
        self.db,self.readonly,self.changed=db,readonly,0

    @property
    def lock_clause(self): return '' if self.readonly else ' FOR UPDATE'

    def execute(self, statement, parameters=()):
        if statement.strip()=='SELECT changes()':
            return self.db.execute('SELECT %s::bigint AS changes', (self.changed,))
        try:
            cursor=self.db.execute(sql(statement), parameters)
            self.changed=cursor.rowcount
            return cursor
        except self.driver.Error:
            raise StoreError('PostgreSQL statement failed') from None

    def executemany(self, statement, parameters):
        try:
            cursor=self.db.cursor()
            cursor.executemany(sql(statement),parameters)
            self.changed=cursor.rowcount
            return cursor
        except self.driver.Error:
            raise StoreError('PostgreSQL batch failed') from None

    def executescript(self, script):
        for statement in script.split(';'):
            if statement.strip(): self.execute(statement)


_pools = {}
_pool_lock = threading.Lock()


def close_pools():
    for (pid, _), pool in list(_pools.items()):
        if pid == os.getpid(): pool.close()
    _pools.clear()


atexit.register(close_pools)


def connection_pool(dsn, driver):
    from psycopg_pool import ConnectionPool
    identity = (os.getpid(), dsn)
    with _pool_lock:
        if identity not in _pools:
            if len(_pools) >= 8: raise StoreError('too many configured PostgreSQL pools')
            kwargs = {'connect_timeout':10, 'row_factory':rows,
                      'options':'-c statement_timeout=30000 -c lock_timeout=15000'}
            # Fail initial configuration synchronously, before background reconnects.
            with driver.connect(dsn, **kwargs): pass
            _pools[identity] = ConnectionPool(dsn, kwargs=kwargs, min_size=0, max_size=64,
                                              max_waiting=256, timeout=15, max_idle=60,
                                              reconnect_timeout=10, open=True)
        return _pools[identity]


class PostgresStore:
    dialect='postgres'

    def __init__(self, path):
        try:
            import psycopg
        except ImportError:
            raise ValueError('install the postgres extra to use PostgreSQL') from None
        parsed=urlsplit(path)
        values=parse_qsl(parsed.query,keep_blank_values=True)
        schemas=[v for k,v in values if k=='nodi_schema']
        self.schema=schemas[0] if schemas else 'nodi'
        if len(schemas)>1 or not re.fullmatch(r'nodi(?:_[a-z0-9]{1,56})?',self.schema):
            raise ValueError('nodi_schema must be nodi or nodi_<lowercase alphanumeric>')
        self.path=path
        self.dsn=urlunsplit(parsed._replace(query=urlencode([(k,v) for k,v in values if k!='nodi_schema'])))
        self.driver=psycopg
        # Serialize initial DDL across processes before the mutex table exists.
        try:
            with self.connect() as db:
                db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('nodi-init:'+self.schema,))
                db.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
                db.execute(f'SET LOCAL search_path TO "{self.schema}"')
                db.execute('CREATE TABLE IF NOT EXISTS ledger_mutex (id integer PRIMARY KEY CHECK(id=1))')
                db.execute('INSERT INTO ledger_mutex VALUES (1) ON CONFLICT DO NOTHING')
        except psycopg.Error:
            raise StoreError('PostgreSQL initialization failed') from None

    def connect(self):
        return connection_pool(self.dsn, self.driver).connection()

    @staticmethod
    def append_audit(db, payload, previous_hash, digest):
        # PostgreSQL sequences do not roll back. Allocate under the ledger mutex
        # so aborted writes cannot create gaps rejected by offline audit verification.
        sequence=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM audit').fetchone()[0]
        db.execute('INSERT INTO audit(sequence,event,previous_hash,event_hash) VALUES(?,?,?,?)',
                   (sequence,payload,previous_hash,digest))

    @contextmanager
    def transaction(self, *, readonly=False):
        try:
            with self.connect() as db:
                if readonly:
                    db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                else:
                    db.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
                db.execute(f'SET LOCAL search_path TO "{self.schema}"')
                if not readonly:
                    db.execute('SELECT id FROM ledger_mutex WHERE id=1 FOR UPDATE')
                yield Connection(db,readonly)
        except self.driver.Error:
            raise StoreError('PostgreSQL transaction failed; execution outcome may require reconciliation') from None
