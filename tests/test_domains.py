"""Domain dispatch preserves the gateway boundary and simulated execution."""
from dataclasses import replace
import io
import json

import pytest
from nodi.domains import get_domain
from nodi.gateway import Gateway
from nodi.models import Action, Contract
from nodi.intent import Intent
from nodi.semantic import SemanticReviewer
from nodi.evaluation import run

NOW = 2_000_000_000


@pytest.fixture
def research(tmp_path):
    plugin = get_domain("research")
    contract = plugin.default_contract("research", NOW)
    gateway = Gateway(str(tmp_path / "research.db"), clock=lambda: NOW)
    return gateway, contract, gateway.issue(contract)["visa_id"]


def summary(visa, key="summary"):
    return Action(key, visa, "filesystem.write", "workspace:summaries/summary.txt",
                  {"path": "summaries/summary.txt", "content": "Python can use SQLite."})


def test_research_effects_are_gateway_records_only(research, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gateway, _, visa = research
    response = gateway.execute("research", get_domain("research").setup_action(visa))
    assert response["decision"] == "ALLOW"
    assert response["result"]["simulation"] is True
    request = summary(visa)
    assert gateway.execute("research", request)["decision"] == "ASK_HUMAN"
    assert len(gateway.snapshot()["effects"]) == 1
    gateway.approve("research", request.request_id, True)
    result = gateway.execute("research", request)
    assert result["executed"] and result["result"]["simulation"]
    assert gateway.execute("research", request)["effect_id"] == result["effect_id"]
    assert len(gateway.snapshot()["effects"]) == 2
    assert not (tmp_path / "summaries/summary.txt").exists()
    assert gateway.verify_audit()


@pytest.mark.parametrize("purpose", ["shell", "os", "nodi.domains.research", "procurement", "research"])
def test_only_canonical_registered_purposes_in_contracts(purpose):
    with pytest.raises(ValueError):
        Contract("a", "Any mission", purpose=purpose)


def test_no_cross_domain_tools_or_authority(research):
    gateway, contract, visa = research
    with pytest.raises(ValueError):
        replace(contract, allowed_tools=["purchase"])
    procurement = get_domain("procurement").default_contract("child", NOW)
    procurement = replace(procurement, mission=contract.mission, delegation_depth=0)
    assert gateway.delegate("research", visa, procurement)["decision"] == "DENY"
    with pytest.raises(ValueError, match="identity or mission"):
        gateway.modify(visa, replace(procurement, agent_id="research"))
    response = gateway.execute("research", Action("purchase", visa, "purchase", "vendor:approved",
                               {"product_id": "monitor-basic", "quantity": 1}))
    assert response["decision"] == "DENY"
    assert not gateway.snapshot()["effects"]


@pytest.mark.parametrize("changes", [
    {"resource": "workspace:notes/source.txt"},
    {"arguments": {"path": "../summary.txt", "content": "hello"}},
    {"arguments": {"path": "/tmp/summary.txt", "content": "hello"}},
    {"arguments": {"path": "summaries/summary.txt", "content": "hello", "domain": "procurement"}},
    {"arguments": {"path": "summaries/summary.txt", "content": "x" * 4001}},
    {"arguments": {"path": "summaries/summary.txt", "content": []}},
])
def test_research_invalid_destinations_and_arguments_deny(research, changes):
    gateway, _, visa = research
    assert gateway.execute("research", replace(summary(visa), **changes))["decision"] == "DENY"
    assert not gateway.snapshot()["effects"]


def test_research_approval_does_not_bypass_ancestor_edit(research):
    gateway, contract, visa = research
    child = gateway.delegate("research", visa, replace(contract, agent_id="child", delegation_depth=0))["visa_id"]
    request = summary(child)
    assert gateway.execute("child", request)["decision"] == "ASK_HUMAN"
    gateway.approve("child", request.request_id, True)
    gateway.modify(visa, replace(contract, forbidden_operations=["filesystem.write"]))
    assert gateway.execute("child", request)["decision"] == "DENY"
    assert not gateway.snapshot()["effects"]
    assert gateway.verify_audit()


def test_remote_allow_cannot_relax_research_baseline(research):
    gateway, _, visa = research
    reviewer = SemanticReviewer("https://reviewer.example/", "test-only")
    class Allow:
        def open(self, request, timeout):
            return io.BytesIO(json.dumps(Intent(1, 0, "allow", [], "ALLOW").dictionary()).encode())
    reviewer.opener = Allow()
    gateway.evaluator = reviewer
    assert gateway.execute("research", summary(visa))["decision"] == "ASK_HUMAN"


@pytest.mark.parametrize("domain", ["procurement", "research"])
def test_domain_datasets_match_reviewed_labels(domain):
    report = run(domain=domain)
    assert report["cases"]
    assert all(case["actual"] == case["expected"] for case in report["cases"])
    assert report["dangerous_false_allow_rate"] == 0
    assert report["domain"] == domain
