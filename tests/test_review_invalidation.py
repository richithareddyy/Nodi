import sqlite3
from dataclasses import replace

import pytest

from nodi.gateway import Gateway
from nodi.models import Action, draft
from nodi.queries import ReadModel


@pytest.fixture
def tree(tmp_path):
    gateway = Gateway(str(tmp_path / "state.db"))
    contract = replace(draft("Buy a monitor under $300.", "root"), delegation_depth=2)
    root = gateway.issue(contract)["visa_id"]
    child_contract = replace(contract, agent_id="child", delegation_depth=1)
    child = gateway.delegate("root", root, child_contract)["visa_id"]
    grandchild = gateway.delegate(
        "child", child, replace(contract, agent_id="grandchild", delegation_depth=0)
    )["visa_id"]
    sibling = gateway.delegate(
        "root", root, replace(contract, agent_id="sibling", delegation_depth=0)
    )["visa_id"]
    unrelated = gateway.issue(replace(contract, agent_id="unrelated"))["visa_id"]
    actions = {}
    for agent, visa in [
        ("root", root),
        ("child", child),
        ("grandchild", grandchild),
        ("sibling", sibling),
        ("unrelated", unrelated),
    ]:
        action = Action(
            "buy", visa, "purchase", "vendor:approved", {"product_id": "monitor-pro", "quantity": 1}
        )
        assert gateway.execute(agent, action)["decision"] == "ASK_HUMAN"
        actions[agent] = action
    gateway.approve("grandchild", "buy", True)
    return gateway, ReadModel(gateway), contract, child_contract, actions


@pytest.mark.parametrize("operation", ["revoke", "modify"])
def test_subtree_only_and_audit_count(tree, operation):
    gateway, reads, _, child_contract, actions = tree
    child = actions["child"].visa_id
    if operation == "revoke":
        gateway.revoke(child)
    else:
        gateway.modify(child, replace(child_contract, max_actions=20))
    assert {r["agent"] for r in reads.approvals()["items"]} == {"root", "sibling", "unrelated"}
    event = gateway.snapshot()["audit"][-1]["event"]
    assert event["invalidated_reviews"] == 2
    for agent in ("child", "grandchild"):
        with pytest.raises(LookupError):
            reads.inspect(agent, "buy")
        with pytest.raises(ValueError):
            gateway.approve(agent, "buy", True)
        assert gateway.execute(agent, actions[agent])["decision"] == "DENY"
    assert not gateway.snapshot()["effects"]
    assert gateway.verify_audit()


def test_root_revocation_and_repeat(tree):
    gateway, reads, _, _, actions = tree
    root = actions["root"].visa_id
    gateway.revoke(root)
    assert gateway.snapshot()["audit"][-1]["event"]["invalidated_reviews"] == 4
    assert [r["agent"] for r in reads.approvals()["items"]] == ["unrelated"]
    gateway.revoke(root)
    assert gateway.snapshot()["audit"][-1]["event"]["invalidated_reviews"] == 0


@pytest.mark.parametrize("operation", ["revoke", "modify"])
def test_audit_failure_rolls_back_policy_and_review_changes(tree, monkeypatch, operation):
    gateway, _, _, child_contract, actions = tree
    before = gateway.snapshot()

    def fail(*args):
        raise sqlite3.OperationalError("injected audit failure")

    monkeypatch.setattr(gateway, "_audit", fail)
    with pytest.raises(sqlite3.OperationalError):
        if operation == "revoke":
            gateway.revoke(actions["child"].visa_id)
        else:
            gateway.modify(actions["child"].visa_id, replace(child_contract, max_actions=20))
    assert gateway.snapshot() == before


def test_completed_results_not_invalidated(tree):
    gateway, reads, _, _, actions = tree
    action = Action(
        "search",
        actions["child"].visa_id,
        "search_products",
        "catalog:monitors",
        {"query": "monitor"},
    )
    original = gateway.execute("child", action)
    assert original["executed"]
    gateway.revoke(actions["child"].visa_id)
    replay = gateway.execute("child", action)
    assert replay["replayed"] and replay["effect_id"] == original["effect_id"]
    assert len(gateway.snapshot()["effects"]) == 1


def test_rejected_reviews_remain_rejected(tree):
    gateway, _, _, _, actions = tree
    gateway.approve("child", "buy", False)
    gateway.revoke(actions["child"].visa_id)
    assert gateway.snapshot()["audit"][-1]["event"]["invalidated_reviews"] == 1
    with gateway.read_transaction() as db:
        assert (
            db.execute("SELECT approval FROM requests WHERE principal='child'").fetchone()[0]
            == "rejected"
        )
