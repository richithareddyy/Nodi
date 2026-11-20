import json
import subprocess
import sys

import pytest

import nodi.evaluation as evaluation

CASE = {
    "id": "valid",
    "category": "legitimate",
    "expected": "ALLOW",
    "dangerous": False,
    "tool": "search_products",
    "resource": "catalog:monitors",
    "arguments": {"query": "monitor"},
}


@pytest.mark.parametrize(
    "change",
    [
        {"dangerous": "false"},
        {"expected": "PERMIT"},
        {"kind": "unknown"},
        {"precondition": "revkoed"},
        {"category": ""},
        {"unexpected": True},
        {"id": "bad.id"},
        {"principal": None},
        {"arguments": []},
        {"dangerous": True},
        {"id": "setup", "precondition": "budget_spent"},
    ],
)
def test_invalid_case_rejected_before_any_simulation(tmp_path, monkeypatch, change):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([CASE, {**CASE, "id": "second", **change}]))

    def unexpected(*args, **kwargs):
        pytest.fail("dataset must validate before creating a gateway")

    monkeypatch.setattr(evaluation, "Gateway", unexpected)
    with pytest.raises(ValueError):
        evaluation.run(str(path))


@pytest.mark.parametrize("value", [[], {}, [CASE, CASE], [None], [CASE] * 1001])
def test_invalid_dataset_shape(tmp_path, value):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        evaluation.run(str(path))


@pytest.mark.parametrize("raw", ['[{"id":"a","id":"b"}]', "[NaN]", "[1e999]"])
def test_ambiguous_json_rejected(tmp_path, raw):
    path = tmp_path / "cases.json"
    path.write_text(raw)
    with pytest.raises(ValueError):
        evaluation.run(str(path))


def test_oversized_dataset(tmp_path):
    path = tmp_path / "cases.json"
    path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError):
        evaluation.run(str(path))


def test_delegation_only_metrics(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "child",
                    "kind": "delegation",
                    "category": "legitimate",
                    "expected": "ALLOW",
                    "dangerous": False,
                    "changes": {},
                }
            ]
        )
    )
    result = evaluation.run(str(path))
    assert result["exact_decision_accuracy"] == 1
    assert result["deterministic_policy_latency_ms"] == {"mean": None, "p95": None}
    assert result["dangerous_false_allow_rate"] is None


def test_cli_safe_only_succeeds_and_invalid_preserves_output(tmp_path):
    path = tmp_path / "cases.json"
    output = tmp_path / "report.json"
    path.write_text(json.dumps([CASE]))
    command = [
        sys.executable,
        "-m",
        "nodi",
        "evaluate",
        "--dataset",
        str(path),
        "--output",
        str(output),
    ]
    assert subprocess.run(command, capture_output=True).returncode == 0
    before = output.read_bytes()
    path.write_text("[]")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2 and "Traceback" not in result.stderr
    assert output.read_bytes() == before
