import hashlib
import io
import json
from pathlib import Path

from nodi.dataset import load_dataset
from nodi.evaluation import run

SOURCE = Path(__file__).parents[1] / "nodi" / "data" / "cases.json"


def test_bundled_report_hash_and_all_denominators():
    result = run()
    cases = json.loads(SOURCE.read_bytes())
    assert result["report_schema_version"] == 1
    assert result["dataset_sha256"] == hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    counts = result["metric_sample_counts"]
    assert counts["dangerous_false_allow_rate"] == 33
    assert counts["exact_decision_accuracy"] == counts["gateway_latency_ms"] == 42
    assert counts["false_deny_rate"] == counts["unnecessary_review_rate"] == 9
    for metric, category in [
        ("mission_drift_detection_rate", "mission_drift"),
        ("prompt_injection_containment_rate", "prompt_injection"),
        ("delegation_escalation_prevention_rate", "delegation_escalation"),
    ]:
        assert counts[metric] == sum(c["category"] == category for c in cases)
    assert counts["deterministic_policy_latency_ms"] == sum(
        c.get("kind") != "delegation" for c in cases
    )
    assert sum(result["category_counts"].values()) == 42


def test_hash_uses_original_bytes_not_filename_or_normalized_json(tmp_path):
    raw = json.dumps(json.loads(SOURCE.read_bytes())[:1]).encode()
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    first.write_bytes(raw)
    second.write_bytes(raw)
    a, b = run(str(first)), run(str(second))
    assert a["dataset_sha256"] == b["dataset_sha256"]
    second.write_bytes(raw + b"\n")
    c = run(str(second))
    assert c["dataset_sha256"] != b["dataset_sha256"]
    assert c["confusion_matrix"] == b["confusion_matrix"]


def test_zero_sample_metrics_explicit(tmp_path):
    path = tmp_path / "safe.json"
    path.write_text(json.dumps(json.loads(SOURCE.read_bytes())[:1]))
    result = run(str(path))
    for key in (
        "dangerous_false_allow_rate",
        "mission_drift_detection_rate",
        "prompt_injection_containment_rate",
        "delegation_escalation_prevention_rate",
    ):
        assert result["metric_sample_counts"][key] == 0 and result[key] is None


def test_dataset_is_read_once_for_validation_and_hash():
    raw = SOURCE.read_bytes()

    class Source:
        calls = 0

        def open(self, mode):
            self.calls += 1
            assert self.calls == 1 and mode == "rb"
            return io.BytesIO(raw)

    source = Source()
    loaded = load_dataset(source)
    assert source.calls == 1 and len(loaded.cases) == 42
    assert loaded.sha256 == hashlib.sha256(raw).hexdigest()
