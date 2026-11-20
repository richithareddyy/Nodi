import io
import json

import pytest

from nodi.intent import Intent
from nodi.models import Action, draft
from nodi.semantic import SemanticReviewer
from nodi.tools import prepare


def reviewer(decision="ALLOW", score=0.99):
    r = SemanticReviewer("https://reviewer.example/evaluate", "test-only-credential")

    class Stub:
        def open(self, request, timeout):
            assert timeout <= 5
            assert json.loads(request.data)["protocol"] == "nodi.intent.v1"
            return io.BytesIO(
                json.dumps(Intent(score, 0.01, "review", [], decision).dictionary()).encode()
            )

    r.opener = Stub()
    return r


def test_model_cannot_promote_unknown_email():
    a = Action(
        "one",
        "v",
        "send_email",
        "vendor:approved",
        {"recipient": "sales@approved.example", "body": "Can we discuss a different option?"},
    )
    assert (
        reviewer()
        .evaluate(draft("Buy a monitor under $300", "a"), a, prepare(a))
        .recommended_decision
        == "ASK_HUMAN"
    )


def test_model_can_tighten_known_action():
    a = Action("one", "v", "search_products", "catalog:monitors", {"query": "monitor"})
    assert (
        reviewer("DENY")
        .evaluate(draft("Buy a monitor under $300", "a"), a, prepare(a))
        .recommended_decision
        == "DENY"
    )


def test_invalid_remote_score_rejected():
    a = Action("one", "v", "search_products", "catalog:monitors", {"query": "monitor"})
    with pytest.raises(ValueError):
        reviewer(score=float("nan")).evaluate(draft("Buy a monitor under $300", "a"), a, prepare(a))


@pytest.mark.parametrize(
    "endpoint",
    ["http://reviewer.example", "https://user:pass@reviewer.example", "file:///tmp/data"],
)
def test_unsafe_service_configuration(endpoint):
    with pytest.raises(ValueError):
        SemanticReviewer(endpoint, "credential")
