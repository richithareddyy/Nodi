"""Bounded local generation and deterministic CI; override for deeper exploration."""

import hashlib
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from hypothesis import settings

settings.register_profile("nodi", max_examples=60, stateful_step_count=60, deadline=None)
settings.register_profile(
    "nodi-ci",
    max_examples=100,
    stateful_step_count=80,
    deadline=None,
    derandomize=True,
    database=None,
)
settings.register_profile("nodi-deep", max_examples=300, stateful_step_count=150, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "nodi"))


# Test-only routing: production never interprets this environment variable.
# File-permission/SQLite worker fixtures remain SQLite; portable ledger fixtures use
# isolated PostgreSQL schemas. Child crash workers receive the explicit DSN in g.path.
@pytest.fixture(autouse=True)
def postgres_ledgers(request, monkeypatch):
    target = os.environ.get("NODI_TEST_POSTGRES_DSN")
    if not target or request.node.path.name in ("test_storage.py", "test_hosted.py"):
        yield
        return
    import psycopg

    from nodi.gateway import Gateway

    original = Gateway.__init__
    schemas = set()

    def initialize(self, path, *args, **kwargs):
        value = os.fspath(path)
        provider = kwargs.get("payment_provider")
        same_provider = provider is not None and getattr(provider, "path", None) == value
        if not value.startswith(("postgresql://", "postgres://")) and not same_provider:
            schema = "nodi_t" + hashlib.sha256(value.encode()).hexdigest()[:40]
            schemas.add(schema)
            parsed = urlsplit(target)
            query = [(k, v) for k, v in parse_qsl(parsed.query) if k != "nodi_schema"]
            value = urlunsplit(parsed._replace(query=urlencode(query + [("nodi_schema", schema)])))
        original(self, value, *args, **kwargs)

    monkeypatch.setattr(Gateway, "__init__", initialize)
    yield
    # Release DDL locks after each schema; property tests create hundreds.
    # A single cleanup transaction can exhaust the default server lock table.
    with psycopg.connect(target, autocommit=True) as db:
        for schema in schemas:
            db.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema))
            )


_DOCKER_TESTS = {
    "tests/integration/test_docker_readonly.py::" + name
    for name in (
        "test_real_tools_via_authenticated_gateway",
        "test_real_worker_refuses_symlink_write_and_sql_mutation",
        "test_container_has_no_network_capabilities_or_writable_mounts",
        "test_proxy_rejects_private_url_without_network_request",
        "test_public_get_through_proxy",
    )
}
_POSTGRES_TEST = "tests/test_postgres.py::test_postgres_fixture_is_really_postgres"


class RequiredCoverage:
    """Only allow skips explicitly assigned to a different CI matrix job."""

    def __init__(self, profile):
        self.allowed = set()
        if profile in {"sqlite", "postgres"}:
            self.allowed.update(_DOCKER_TESTS)
        if profile == "sqlite":
            self.allowed.add(_POSTGRES_TEST)
        self.unexpected = set()

    def pytest_runtest_logreport(self, report):
        if report.skipped and report.nodeid not in self.allowed:
            self.unexpected.add(report.nodeid)

    def pytest_collectreport(self, report):
        # Missing optional dependencies can skip an entire module during collection.
        if report.skipped:
            self.unexpected.add(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        if self.unexpected:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter):
        if self.unexpected:
            terminalreporter.write_sep("=", "Unexpected CI skips (coverage required)", red=True)
            for nodeid in sorted(self.unexpected):
                terminalreporter.write_line(nodeid, red=True)


def pytest_configure(config):
    profile = os.environ.get("NODI_TEST_CI_PROFILE")
    if not profile and os.environ.get("GITHUB_ACTIONS") != "true":
        return  # Local opt-in behavior remains unchanged.
    if profile not in {"sqlite", "postgres", "docker", "postgres-docker"}:
        raise pytest.UsageError("CI requires an explicit valid NODI_TEST_CI_PROFILE")
    if profile in {"postgres", "postgres-docker"} and not os.environ.get("NODI_TEST_POSTGRES_DSN"):
        raise pytest.UsageError("PostgreSQL CI coverage requires NODI_TEST_POSTGRES_DSN")
    if profile in {"docker", "postgres-docker"}:
        if not os.environ.get("NODI_TEST_SANDBOX_IMAGE"):
            raise pytest.UsageError("Docker CI coverage requires NODI_TEST_SANDBOX_IMAGE")
        if os.environ.get("NODI_TEST_PUBLIC_HTTP") != "1":
            raise pytest.UsageError("Docker CI coverage requires NODI_TEST_PUBLIC_HTTP=1")
    config.pluginmanager.register(RequiredCoverage(profile), "nodi-required-coverage")
