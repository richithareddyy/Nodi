import io
import json
from dataclasses import replace

import pytest

from nodi.gateway import Gateway
from nodi.intent import Intent
from nodi.models import Action, draft
from nodi.semantic import SemanticReviewer
from nodi.tools import prepare

VALID = Intent(0.99, 0.01, "Within mission", [], "ALLOW")


@pytest.mark.parametrize(
    "field,value",
    [
        ("reason", None),
        ("reason", {}),
        ("reason", ""),
        ("reason", "   "),
        ("reason", "x" * 4097),
        ("evidence", "claim"),
        ("evidence", {}),
        ("evidence", [None]),
        ("evidence", [""]),
        ("evidence", ["x" * 501]),
        ("evidence", ["x"] * 41),
        ("recommended_decision", ["ALLOW"]),
        ("recommended_decision", True),
        ("mission_alignment_score", True),
        ("risk_score", float("inf")),
    ],
)
def test_invalid_custom_evaluator_requires_review(tmp_path, field, value):
    class Evaluator:
        def evaluate(self, *args):
            return replace(VALID, **{field: value})

    gateway = Gateway(str(tmp_path / "state.db"), evaluator=Evaluator())
    visa = gateway.issue(draft("Buy a monitor under $300.", "a"))["visa_id"]
    action = Action("search", visa, "search_products", "catalog:monitors", {"query": "monitor"})
    response = gateway.execute("a", action)
    assert response["decision"] == "ASK_HUMAN" and not response["executed"]
    assert response["intent"]["reason"] == "Intent evaluator unavailable or invalid"
    assert not gateway.snapshot()["effects"]
    assert gateway.verify_audit()


def evaluate_raw(raw):
    reviewer = SemanticReviewer("https://reviewer.example/evaluate", "test-credential")

    class Stub:
        def open(self, *args, **kwargs):
            return io.BytesIO(raw)

    reviewer.opener = Stub()
    action = Action("search", "visa", "search_products", "catalog:monitors", {"query": "monitor"})
    return reviewer.evaluate(draft("Buy a monitor under $300.", "a"), action, prepare(action))


@pytest.mark.parametrize(
    "extra",
    [
        '"recommended_decision":"DENY",',
        '"reason":"other",',
        '"risk_score":NaN,',
    ],
)
def test_ambiguous_remote_json_rejected(extra):
    raw = ("{" + extra + json.dumps(VALID.dictionary())[1:]).encode()
    with pytest.raises(ValueError):
        evaluate_raw(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("reason", ""),
        ("reason", "x" * 2001),
        ("evidence", ["x"] * 21),
        ("evidence", [" "]),
        ("risk_score", float("nan")),
    ],
)
def test_remote_limits(field, value):
    data = {**VALID.dictionary(), field: value}
    with pytest.raises(ValueError):
        evaluate_raw(json.dumps(data).encode())


def test_remote_boundary_composes_and_validates():
    data = {**VALID.dictionary(), "reason": "x" * 2000, "evidence": ["x" * 500] * 20}
    result = evaluate_raw(json.dumps(data).encode())
    result.validate()
    assert result.recommended_decision == "ALLOW"
    assert len(result.reason) > 2000


def test_custom_result_boundary():
    replace(VALID, reason="x" * 4096, evidence=["x" * 500] * 40).validate()
