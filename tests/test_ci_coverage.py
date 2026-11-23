"""Exercise the CI skip guard in isolated pytest processes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def run_ci_pytest(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text(Path(__file__).with_name("conftest.py").read_text())

    def run(source, *, profile="sqlite", filename="test_probe.py", overrides=None):
        (tests / filename).write_text(source)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("NODI_TEST_") and key != "PYTEST_ADDOPTS"
        }
        env["GITHUB_ACTIONS"] = "true"
        if profile is not None:
            env["NODI_TEST_CI_PROFILE"] = profile
        env.update(overrides or {})
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    return run


@pytest.mark.parametrize(
    "profile,overrides,required",
    [
        (None, {}, "NODI_TEST_CI_PROFILE"),
        ("unknown", {}, "NODI_TEST_CI_PROFILE"),
        ("postgres", {}, "NODI_TEST_POSTGRES_DSN"),
        ("docker", {}, "NODI_TEST_SANDBOX_IMAGE"),
        ("docker", {"NODI_TEST_SANDBOX_IMAGE": "test-image"}, "NODI_TEST_PUBLIC_HTTP=1"),
    ],
)
def test_ci_requires_declared_prerequisites(run_ci_pytest, profile, overrides, required):
    result = run_ci_pytest("def test_ok(): pass\n", profile=profile, overrides=overrides)
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert required in result.stderr


def test_unexpected_runtime_skip_fails_ci(run_ci_pytest):
    result = run_ci_pytest("import pytest\ndef test_probe(): pytest.skip('missing prerequisite')\n")
    assert result.returncode == pytest.ExitCode.TESTS_FAILED
    assert "Unexpected CI skips" in result.stdout
    assert "test_probe.py::test_probe" in result.stdout


def test_missing_optional_dependency_cannot_skip_a_module(run_ci_pytest):
    result = run_ci_pytest(
        "import pytest\npytest.importorskip('nodi_deliberately_missing_module')\n"
    )
    assert result.returncode == pytest.ExitCode.TESTS_FAILED
    assert "Unexpected CI skips" in result.stdout


def test_sqlite_job_allows_only_explicit_postgres_skip(run_ci_pytest):
    result = run_ci_pytest(
        "import pytest\n"
        "def test_postgres_fixture_is_really_postgres():\n"
        "    pytest.skip('PostgreSQL test matrix only')\n"
        "def test_ok(): pass\n",
        filename="test_postgres.py",
    )
    assert result.returncode == pytest.ExitCode.OK
    assert "1 passed, 1 skipped" in result.stdout


def test_passing_ci_suite_stays_successful(run_ci_pytest):
    result = run_ci_pytest("def test_ok(): pass\n")
    assert result.returncode == pytest.ExitCode.OK
