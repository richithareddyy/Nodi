"""Scripted unit checks validate harness wiring, never measured model performance."""

import asyncio
import json
import threading
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from benchmarks.injection.environment import MODES, Environment
from benchmarks.injection.oracle import classify
from benchmarks.injection.provider import Model, ProviderError
from benchmarks.injection.report import summarize
from benchmarks.injection.runner import episode, run
from benchmarks.injection.suite import build, load
from benchmarks.metrics import rate
from benchmarks.purpose.contention import measure
from benchmarks.purpose.run import run as purpose_run
from nodi.domains import get_domain
from nodi.gateway import Gateway
from nodi.intent import DomainEvaluator, Intent
from nodi.models import Action

ROOT = Path(__file__).resolve().parents[1]


class Scripted:
    def __init__(self, proposal):
        self.proposal, self.calls = proposal, 0

    async def complete(self, messages, tools):
        self.calls += 1
        if tools[0]["name"] == "review_action":
            proposal = {
                "name": "review_action",
                "arguments": {"decision": "DENY", "reason": "scripted unit test"},
            }
        elif self.calls == 1:
            proposal = self.proposal
        else:
            return {"role": "assistant", "content": "Done", "tool_calls": []}, {"fixture": True}
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": str(self.calls),
                    "type": "function",
                    "function": {
                        "name": proposal["name"],
                        "arguments": json.dumps(proposal["arguments"]),
                    },
                }
            ],
        }, {"fixture": True}


def test_dataset_separation_and_labels():
    dev, _ = load(ROOT / "benchmarks/injection/data/dev.json")
    held, _ = load(ROOT / "benchmarks/injection/data/heldout.json")
    assert not {c["family"] for c in dev} & {c["family"] for c in held}
    assert sum(c["kind"] == "attack" for c in dev + held) == 200
    assert len(build(30)) == 640
    for case in dev + held:
        contract = get_domain(case["domain"]).default_contract("agent", 2000000000)
        label, _ = classify(case["target"], contract.dictionary())
        assert label == ("harmful" if case["kind"] == "attack" else "benign"), case["id"]


@pytest.mark.parametrize("mode", MODES)
def test_real_mcp_wiring_with_scripted_attack(mode):
    pytest.importorskip("mcp")
    case = build()[0]
    row = asyncio.run(episode(case, mode, Scripted(case["target"])))
    assert row["status"] == "complete", row
    assert row["calls"][0]["label"] == "harmful"
    assert row["calls"][0]["decision"] == (
        "DENY" if mode in ("full_nodi", "llm_judge") else "ALLOW"
    )
    assert row["initial_messages"][-1]["role"] == "tool"
    assert (
        json.loads(row["initial_messages"][-1]["content"])["result"]["products"]["monitor-basic"][
            "description"
        ]
        == case["payload"]
    )
    assert case["payload"] not in row["initial_messages"][0]["content"]


@pytest.mark.parametrize("domain", ["procurement", "research"])
def test_benign_mcp_paths(domain):
    pytest.importorskip("mcp")
    case = next(c for c in build() if c["kind"] == "benign" and c["domain"] == domain)
    row = asyncio.run(episode(case, "full_nodi", Scripted(case["target"])))
    assert row["status"] == "complete", row
    assert row["calls"][0]["label"] == "benign"
    assert row["calls"][0]["decision"] in ("ALLOW", "ASK_HUMAN")


def test_delegation_uses_production_rejection():
    pytest.importorskip("mcp")
    case = next(c for c in build() if c["category"] == "delegation")
    row = asyncio.run(episode(case, "full_nodi", Scripted(case["target"])))
    assert row["status"] == "complete", row
    assert row["calls"][0]["decision"] == "DENY"


def test_skip_is_not_performance(tmp_path, monkeypatch):
    for key in ("NODI_BENCH_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    args = Namespace(
        dataset=ROOT / "benchmarks/injection/data/heldout.json",
        output=tmp_path / "skip",
        live=True,
        seed=1,
        turns=2,
        max_calls=4,
        limit=2,
    )
    report = asyncio.run(run(args))
    assert report["status"] == "skipped" and report["modes"] == {}
    assert not (args.output / "traces.jsonl").exists()
    assert rate(0, 0)["rate"] is None
    assert rate(5, 10)["wilson95"][0] < 0.5 < rate(5, 10)["wilson95"][1]


def test_report_denominators_and_excluded_errors():
    base = {
        "mode": "full_nodi",
        "kind": "attack",
        "category": "drift",
        "family": "family",
        "status": "complete",
        "model_ms": [1],
        "calls": [
            {
                "label": "harmful",
                "needs_review": False,
                "decision": "ASK_HUMAN",
                "executed": False,
                "gateway_ms": 2,
            }
        ],
    }
    report = summarize([base, {**base, "status": "error"}])["full_nodi"]
    assert report["agent_compromise"]["denominator"] == 1
    assert report["harmful_attempt_containment"]["rate"] == 1
    assert report["benign_false_denial"]["rate"] is None
    assert report["excluded_incomplete"] == 1


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_provider_mock_transport(provider):
    httpx = pytest.importorskip("httpx2")

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        if provider == "openai":
            assert body["store"] is False and body["tools"][0]["type"] == "function"
            value = {
                "id": "x",
                "model": "test-model",
                "choices": [{"message": {"content": "done"}}],
                "usage": {"total_tokens": 1},
            }
        else:
            assert body["max_tokens"] == 1200
            value = {
                "id": "x",
                "model": "test-model",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 1},
            }
        return httpx.Response(200, json=value)

    model = Model(
        provider, "test-secret", "test-model", max_calls=1, transport=httpx.MockTransport(handler)
    )
    result, meta = asyncio.run(
        model.complete(
            [{"role": "user", "content": "test"}],
            [{"name": "test", "description": "test", "parameters": {"type": "object"}}],
        )
    )
    assert result["content"] == "done" and meta["model"] == "test-model"
    assert "test-secret" not in json.dumps(meta)
    with pytest.raises(ProviderError):
        asyncio.run(model.complete([], []))


def test_purpose_report_records_provenance_and_review(tmp_path):
    report = purpose_run(ROOT / "benchmarks/purpose/cases.json", tmp_path / "purpose.json")
    assert report["label_provenance"]["independently_validated"] is False
    assert report["groups"]["heldout"]["ordinary"]["ordinary_review"]["numerator"] >= 2
    assert report["groups"]["heldout"]["adversarial"]["false_allow"]["numerator"] == 0


def test_contention_metrics_without_timing_thresholds():
    report = measure(workers=3, delay=0.001)
    for case in report["conditions"].values():
        assert case["writer_acquisition"]["n"] == 3 and case["audit_valid"]
    assert report["conditions"]["failing"]["decisions"]["ASK_HUMAN"] == 3


def test_slow_evaluator_holds_writer_lock_and_budget_is_rechecked(tmp_path):
    entered, release, second_attempted = threading.Event(), threading.Event(), threading.Event()

    class Evaluator:
        def evaluate(self, *args):
            entered.set()
            assert release.wait(5)
            return DomainEvaluator().evaluate(*args)

    class Observed(Gateway):
        @contextmanager
        def transaction(self):
            if threading.current_thread().name.startswith("waiting"):
                second_attempted.set()
            with super().transaction() as db:
                yield db

    gateway = Observed(str(tmp_path / "ledger.db"), evaluator=Evaluator())
    visa = gateway.issue(get_domain("procurement").default_contract("agent", 2000000000))["visa_id"]
    gateway.clock = lambda: 2000000000
    action = Action(
        "one", visa, "purchase", "vendor:approved", {"product_id": "monitor-basic", "quantity": 1}
    )
    with (
        ThreadPoolExecutor(max_workers=1) as first,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="waiting") as second,
    ):
        a = first.submit(gateway.execute, "agent", action)
        assert entered.wait(5)
        b = second.submit(gateway.execute, "agent", replace(action, request_id="two"))
        assert second_attempted.wait(5)
        assert not b.done()
        release.set()
        assert a.result()["decision"] == "ALLOW"
        assert b.result()["decision"] == "DENY"
    assert gateway.snapshot()["visas"][0]["spent"] == 18000


def test_hard_policy_precedes_semantic_approval(tmp_path):
    class Approves:
        calls = 0

        def evaluate(self, *args):
            self.calls += 1
            return Intent(1, 0, "Test approval", [], "ALLOW")

    evaluator = Approves()
    g = Gateway(str(tmp_path / "ledger.db"), evaluator=evaluator, clock=lambda: 2000000000)
    visa = g.issue(get_domain("procurement").default_contract("agent", 2000000000))["visa_id"]
    result = g.execute(
        "agent",
        Action(
            "over",
            visa,
            "purchase",
            "vendor:approved",
            {"product_id": "monitor-ultra", "quantity": 1},
        ),
    )
    assert result["decision"] == "DENY" and not result["executed"] and evaluator.calls == 0


def test_failed_call_preserves_observed_attempt_without_claiming_containment(monkeypatch):
    pytest.importorskip("mcp")
    original = Environment.call

    async def fail_after_seed(self, client, call):
        if not self.seeding:
            raise ConnectionError("controlled transport failure")
        return await original(self, client, call)

    monkeypatch.setattr(Environment, "call", fail_after_seed)
    case = build()[0]
    row = asyncio.run(episode(case, "full_nodi", Scripted(case["target"])))
    report = summarize([row])["full_nodi"]
    assert row["status"] == "error"
    assert report["observed_harmful_attempts_all_statuses"] == 1
    assert report["harmful_attempt_containment"]["denominator"] == 0


def test_oracle_accounts_for_action_cap():
    contract = get_domain("procurement").default_contract("agent", 2000000000)
    proposal = {
        "name": "execute_action",
        "arguments": {
            "tool": "search_products",
            "resource": "catalog:monitors",
            "arguments": {"query": "monitor"},
        },
    }
    assert classify(proposal, contract.dictionary(), actions=contract.max_actions)[0] == "harmful"


def test_counterfactual_negative_quantity_does_not_create_budget():
    pytest.importorskip("mcp")

    class Sequence:
        index = 0

        async def complete(self, messages, tools):
            self.index += 1
            if self.index > 3:
                return {"role": "assistant", "content": "done", "tool_calls": []}, {"fixture": True}
            args = {
                "tool": "purchase",
                "resource": "vendor:approved",
                "arguments": {
                    "product_id": "monitor-basic",
                    "quantity": -1 if self.index == 2 else 1,
                },
            }
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": str(self.index),
                        "type": "function",
                        "function": {"name": "execute_action", "arguments": json.dumps(args)},
                    }
                ],
            }, {"fixture": True}

    row = asyncio.run(episode(build()[0], "no_guard", Sequence()))
    assert row["status"] == "complete"
    assert [r["label"] for r in row["calls"]] == ["benign", "harmful", "harmful"]
