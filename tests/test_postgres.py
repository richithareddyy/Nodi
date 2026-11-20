"""PostgreSQL-specific adapter behavior; portable enforcement tests run separately."""

import os
from decimal import Decimal

import pytest

from nodi.postgres import StoreError, rows, sql


def test_static_dialect_translation():
    assert sql("SELECT * FROM visas WHERE id=?") == "SELECT * FROM visas WHERE id=%s"
    assert (
        sql("SELECT json_extract(contract, '$.agent_id') FROM visas")
        == "SELECT (contract::jsonb ->> 'agent_id') FROM visas"
    )
    assert sql("INSERT OR IGNORE INTO capability_epochs VALUES (?,0)").endswith(
        "ON CONFLICT DO NOTHING"
    )


def test_integer_aggregate_does_not_leak_decimal():
    class Column:
        def __init__(self, name):
            self.name = name

    class Cursor:
        description = [Column("total")]

    make = rows(Cursor())
    assert type(make([Decimal("18000")])[0]) is int
    with pytest.raises(StoreError):
        make([Decimal("0.1")])


def test_schema_name_is_not_sql():
    pytest.importorskip("psycopg")
    from nodi.postgres import PostgresStore

    with pytest.raises(ValueError):
        PostgresStore("postgresql://localhost/x?nodi_schema=public%3BDROP")


def test_postgres_fixture_is_really_postgres(tmp_path):
    if not os.environ.get("NODI_TEST_POSTGRES_DSN"):
        pytest.skip("PostgreSQL test matrix only")
    from nodi.gateway import Gateway

    gateway = Gateway(str(tmp_path / "ledger.db"))
    assert gateway.store.dialect == "postgres"
    with gateway.transaction() as db:
        assert (
            db.execute("SELECT current_setting('transaction_isolation')").fetchone()[0]
            == "read committed"
        )
        assert db.lock_clause == " FOR UPDATE"
    with gateway.read_transaction() as db:
        assert (
            db.execute("SELECT current_setting('transaction_isolation')").fetchone()[0]
            == "repeatable read"
        )
        assert db.execute("SELECT current_setting('transaction_read_only')").fetchone()[0] == "on"


def test_postgres_error_redacts_credentials():
    pytest.importorskip("psycopg")
    from nodi.postgres import PostgresStore

    with pytest.raises(StoreError) as error:
        PostgresStore("postgresql://user:never-print-this@127.0.0.1:1/no_database")
    assert "never-print-this" not in str(error.value)


def test_audit_export_remains_contiguous_after_rollback(tmp_path):
    from nodi.audit_verify import Verifier
    from nodi.gateway import Gateway
    from nodi.models import Contract
    from nodi.queries import ReadModel

    gateway = Gateway(str(tmp_path / "rollback.db"))
    gateway.issue(Contract("one", "Buy monitors"))
    with pytest.raises(RuntimeError):
        with gateway.transaction() as db:
            gateway._audit(db, {"event": "rolled_back"})
            raise RuntimeError("controlled transaction abort")
    gateway.issue(Contract("two", "Buy monitors"))
    verifier = Verifier(ReadModel(gateway).checkpoint())
    for row in gateway.snapshot()["audit"]:
        verifier.add(row)
    assert verifier.finish()["sequence"] == 2


def test_benchmark_summary_preserves_counts_and_errors():
    from benchmarks.concurrency_report import summarize

    row = {
        "backend": "sqlite",
        "workers": 10,
        "workload": "duplicates",
        "successful_operations_per_second": 50,
        "errors": 1,
        "samples": [{"latency_ms": i} for i in range(10)],
    }
    result = summarize({"trials": [row, row, row]})[0]
    assert result["n"] == 30 and result["trials"] == 3 and result["errors"] == 3
    assert result["bootstrap95"] == [50, 50]
