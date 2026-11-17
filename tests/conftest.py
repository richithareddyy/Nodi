"""Bounded local generation and deterministic CI; override for deeper exploration."""
import os
from hypothesis import settings

settings.register_profile("nodi", max_examples=60, stateful_step_count=60, deadline=None)
settings.register_profile("nodi-ci", max_examples=100, stateful_step_count=80,
                          deadline=None, derandomize=True, database=None)
settings.register_profile("nodi-deep", max_examples=300, stateful_step_count=150, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "nodi"))


# Test-only routing: production never interprets this environment variable.
# File-permission/SQLite worker fixtures remain SQLite; portable ledger fixtures use
# isolated PostgreSQL schemas. Child crash workers receive the explicit DSN in g.path.
import hashlib
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import pytest


@pytest.fixture(autouse=True)
def postgres_ledgers(request, monkeypatch):
    target=os.environ.get('NODI_TEST_POSTGRES_DSN')
    if not target or request.node.path.name in ('test_storage.py','test_hosted.py'):
        yield
        return
    from nodi.gateway import Gateway
    import psycopg
    original=Gateway.__init__
    schemas=set()
    def initialize(self,path,*args,**kwargs):
        value=os.fspath(path)
        provider=kwargs.get('payment_provider')
        same_provider=provider is not None and getattr(provider,'path',None)==value
        if not value.startswith(('postgresql://','postgres://')) and not same_provider:
            schema='nodi_t'+hashlib.sha256(value.encode()).hexdigest()[:40]
            schemas.add(schema)
            parsed=urlsplit(target)
            query=[(k,v) for k,v in parse_qsl(parsed.query) if k!='nodi_schema']
            value=urlunsplit(parsed._replace(query=urlencode(query+[('nodi_schema',schema)])))
        original(self,value,*args,**kwargs)
    monkeypatch.setattr(Gateway,'__init__',initialize)
    yield
    # Release DDL locks after each schema; property tests create hundreds.
    # A single cleanup transaction can exhaust the default server lock table.
    with psycopg.connect(target, autocommit=True) as db:
        for schema in schemas:
            db.execute(psycopg.sql.SQL('DROP SCHEMA {} CASCADE').format(psycopg.sql.Identifier(schema)))
