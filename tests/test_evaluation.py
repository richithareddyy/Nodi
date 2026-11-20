from pathlib import Path

from nodi.demo import run as demo
from nodi.evaluation import run


def test_labeled_regression_dataset():
    result = run(str(Path(__file__).parents[1] / "nodi" / "data" / "cases.json"))
    assert result["dangerous_false_allow_rate"] == 0
    assert result["exact_decision_accuracy"] == 1
    assert result["prompt_injection_containment_rate"] == 1
    assert result["delegation_escalation_prevention_rate"] == 1


def test_six_demo_scenarios():
    assert [r["decision"] for r in demo()] == [
        "ALLOW",
        "ALLOW",
        "DENY",
        "ASK_HUMAN",
        "ALLOW",
        "DENY",
        "DENY",
        "DENY",
    ]


def test_bundled_evaluation_ignores_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "evaluation").mkdir()
    (tmp_path / "evaluation" / "cases.json").write_text("invalid unrelated data")
    result = run()
    assert result["case_count"] == 42 and result["exact_decision_accuracy"] == 1


def test_custom_dataset_remains_supported(tmp_path):
    import json

    source = Path(__file__).parents[1] / "nodi" / "data" / "cases.json"
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps(json.loads(source.read_text())[:2]))
    result = run(str(custom))
    assert result["dataset"] == "custom.json" and result["case_count"] == 2
