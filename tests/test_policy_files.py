"""Strict compilation, deny precedence, exact money and CLI error behavior."""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import pytest
from hypothesis import given, strategies as st
from nodi.gateway import Gateway
from nodi.models import Action, Contract
from nodi.policy_files import compile_policy, load_policy

NOW = 2_000_000_000
ROOT = Path(__file__).resolve().parents[1]


def policy():
    return json.loads((ROOT / "examples/policies/research.json").read_text())


def test_deny_glob_wins_and_cannot_be_delegated_away(tmp_path):
    value = policy()
    value["deny"] = ["filesystem.*"]
    report = compile_policy(value, now=NOW)
    contract = Contract(**report["contract"])
    assert contract.allowed_tools == ["web.fetch"]
    assert set(contract.forbidden_operations) == {"filesystem.read", "filesystem.write"}
    gateway = Gateway(str(tmp_path / "ledger.db"), clock=lambda: NOW)
    visa = gateway.issue(contract)["visa_id"]
    request = Action("blocked", visa, "filesystem.read", "workspace:notes/source.txt", {"path": "notes/source.txt"})
    assert gateway.execute("research", request)["decision"] == "DENY"
    assert gateway.delegate("research", visa, replace(contract, agent_id="child", delegation_depth=0,
                            allowed_tools=["filesystem.read"], forbidden_operations=[]))["decision"] == "DENY"
    assert not gateway.snapshot()["effects"]


def test_aliases_and_unmatched_denies_are_explained():
    path = ROOT / "examples/policies/procurement.json"
    report = load_policy(path, now=NOW)
    assert report["aliases_used"] == {"web.search": "search_products", "product.lookup": "compare_products"}
    assert report["unmatched_deny_patterns"] == ["filesystem.*", "shell.*"]
    assert report["contract"]["expires_at"] == NOW + 7200
    assert report["resources_defaulted"]
    value = json.loads(path.read_text())
    value["deny"] = ["web.*"]
    assert "search_products" not in compile_policy(value, now=NOW)["effective_tools"]
    value["allow"] = ["web.search"]
    value["deny"] = ["search_*"]
    assert compile_policy(value, now=NOW)["effective_tools"] == []


@pytest.mark.parametrize("section", [None, "resources", "budget", "delegation", "approval"])
def test_unknown_keys_are_rejected_at_every_level(section):
    value = policy()
    target = value if section is None else value[section]
    target["typo"] = True
    with pytest.raises(ValueError):
        compile_policy(value, now=NOW)


@pytest.mark.parametrize("field,value", [
    ("domain", "missing"), ("domain", "research_assistant"), ("domain", []),
    ("mission", ""), ("agent_id", False), ("allow", ["shell.exec"]),
    ("allow", ["filesystem.*"]), ("allow", ["web.fetch", "web.fetch"]),
    ("deny", [True]), ("deny", "filesystem.*"), ("deny", ["filesystem./*"]),
    ("max_actions", True), ("max_actions", 0), ("expires_in", "0h"),
    ("expires_in", "-1h"), ("expires_in", "1.5h"), ("expires_in", "8d"),
    ("expires_in", 3600), ("delegation", {"max_depth": 9}),
    ("resources", {"allow": ["workspace:../secret"], "deny": []}), ("resources", None),
])
def test_invalid_policy_values(field, value):
    document = policy()
    document[field] = value
    with pytest.raises((ValueError, TypeError)):
        compile_policy(document, now=NOW)


@pytest.mark.parametrize("amount", [True, "1.00", -1, Decimal("0.001"), Decimal("1.000000000000000000000000000001"),
                                    Decimal("NaN"), Decimal("Infinity"), 1_000_000_001])
def test_invalid_money_is_not_rounded_or_coerced(amount):
    value = policy()
    value["budget"]["max_usd"] = amount
    with pytest.raises(ValueError):
        compile_policy(value, now=NOW)


@given(amount=st.integers(0, 100_000_000), depth=st.integers(0, 4))
def test_money_and_depth_compile_exactly(amount, depth):
    value = policy()
    value["budget"] = {"max_usd": Decimal(amount) / 100, "max_transaction_usd": Decimal(amount) / 100}
    value["approval"] = {"purchase_above_usd": Decimal(amount) / 100}
    value["delegation"] = {"max_depth": depth}
    contract = compile_policy(value, now=NOW)["contract"]
    assert contract["spending_limit"] == contract["maximum_transaction"] == contract["approval_threshold"] == amount
    assert contract["delegation_depth"] == depth


@pytest.mark.parametrize("raw", [b'{"domain":"research","domain":"procurement"}', b'{"money":NaN}',
                                  b'[]', b'\xff', b' ' * 65537, b'domain: research'])
def test_file_rejects_ambiguous_oversized_non_json(tmp_path, raw):
    path = tmp_path / "invalid.json"
    path.write_bytes(raw)
    with pytest.raises((ValueError, UnicodeError)):
        load_policy(path)


def test_file_decimal_precision_and_resource_denial(tmp_path):
    value = policy()
    value["resources"]["deny"] = ["workspace:notes/source.txt"]
    value["budget"] = {"max_usd": 0.29, "max_transaction_usd": 0.29}
    value["approval"] = {"purchase_above_usd": 0.29}
    path = tmp_path / "decimal.json"
    path.write_text(json.dumps(value))
    report = load_policy(path, now=NOW)
    assert report["contract"]["spending_limit"] == 29
    assert "workspace:notes/source.txt" not in report["contract"]["allowed_resources"]
    assert "workspace:notes/source.txt" in report["contract"]["forbidden_resources"]


def test_cli_validate_explain_and_failure(tmp_path):
    path = ROOT / "examples/policies/research.json"
    command = [sys.executable, "-m", "nodi", "policy"]
    validation = subprocess.run([*command, "validate", str(path)], capture_output=True, text=True)
    assert validation.returncode == 0
    assert json.loads(validation.stdout) == {"valid": True, "domain": "research"}
    explain = subprocess.run([*command, "explain", str(path), "--agent", "reader"], capture_output=True, text=True)
    assert explain.returncode == 0
    assert json.loads(explain.stdout)["contract"]["agent_id"] == "reader"
    bad = tmp_path / "bad.json"
    bad.write_text('{"unknown":true}')
    failure = subprocess.run([*command, "validate", str(bad)], capture_output=True, text=True)
    assert failure.returncode == 2 and "Traceback" not in failure.stderr
    assert not json.loads(failure.stderr)["valid"]
