"""Ledger transaction adapters. SQLite remains the dependency-free default."""
import os
import sqlite3
from contextlib import contextmanager

from .storage import private_database


class SQLiteStore:
    dialect = 'sqlite'

    def __init__(self, path):
        self.path = os.fspath(path)
        private_database(self.path, create=True)

    @staticmethod
    def append_audit(db, payload, previous_hash, digest):
        db.execute('INSERT INTO audit(event,previous_hash,event_hash) VALUES(?,?,?)', (payload,previous_hash,digest))

    @contextmanager
    def transaction(self, *, readonly=False):
        private_database(self.path)
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            if readonly: db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN' if readonly else 'BEGIN IMMEDIATE')
            yield db
            if readonly: db.rollback()
            else: db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def create_store(path):
    path = os.fspath(path)
    if path.startswith(('postgresql://','postgres://')):
        from .postgres import PostgresStore
        return PostgresStore(path)
    return SQLiteStore(path)
