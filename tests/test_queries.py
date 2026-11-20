import pytest

from nodi.gateway import Gateway
from nodi.models import Action, draft
from nodi.queries import ReadModel


@pytest.fixture
def data(tmp_path):
    now = [2000000000]
    g = Gateway(str(tmp_path / "pages.db"), clock=lambda: now[0])
    reads = ReadModel(g)
    visa = g.issue(draft("Buy a monitor under $300.", "a", now[0]))["visa_id"]
    for i in range(8):
        g.execute("a", Action(str(i), visa, "purchase", "vendor:approved", {"product_id": "monitor-pro", "quantity": 1}))
    return g, reads, visa, now


def test_audit_pages_have_no_gaps_and_exclude_later_inserts(data):
    g, reads, visa, _ = data
    page = reads.audit(limit=3)
    high, rows = page["through"], page["items"]
    g.revoke(visa)
    while page["has_more"]:
        page = reads.audit(after=page["next_after"], through=high, limit=3)
        rows.extend(page["items"])
    assert [r["sequence"] for r in rows] == list(range(1, high + 1))
    assert reads.audit(after=high)["items"][0]["event"]["event"] == "visa_revoked"


def test_audit_filters_are_parameterized_and_exact(data):
    g, reads, visa, _ = data
    g.execute("a", Action("bad", visa, "purchase", "vendor:approved", {"product_id": "monitor-ultra", "quantity": 1}))
    page = reads.audit(agent="a", visa=visa, decision="DENY", blocked=True)
    assert len(page["items"]) == 1
    assert page["items"][0]["event"]["action"]["request_id"] == "bad"
    assert not reads.audit(agent="a' OR 1=1 --")["items"]
    assert not reads.audit(visa="unknown")["items"]


def test_sparse_filtered_pages_and_empty_high_water(data):
    g, reads, visa, _ = data
    for i in range(5):
        g.execute("a", Action("deny" + str(i), visa, "shell", "vendor:approved", {}))
    first = reads.audit(blocked=True, limit=2)
    second = reads.audit(blocked=True, limit=2, after=first["next_after"], through=first["through"])
    assert len(first["items"]) == len(second["items"]) == 2
    assert set(r["cursor"] for r in first["items"]).isdisjoint(r["cursor"] for r in second["items"])
    empty = reads.audit(after=first["through"])
    assert empty["items"] == [] and empty["next_after"] is None and not empty["has_more"]


def test_approval_pages_and_server_clock_expiry(data):
    _, reads, _, now = data
    first = reads.approvals(limit=3)
    assert len(first["items"]) == 3 and first["has_more"]
    second = reads.approvals(limit=3, after=first["next_after"], through=first["through"])
    assert {r["request_id"] for r in first["items"]}.isdisjoint(r["request_id"] for r in second["items"])
    now[0] += 300
    assert not reads.approvals()["items"]
    assert all(r["expired"] for r in reads.approvals(include_expired=True)["items"])
    review = reads.inspect("a", "0")
    assert review["expired"] and review["server_time"] == now[0]


def test_approval_mutation_between_pages(data):
    g, reads, _, _ = data
    first = reads.approvals(limit=2)
    g.approve("a", "2", False)
    second = reads.approvals(after=first["next_after"], through=first["through"])
    assert "2" not in {r["request_id"] for r in second["items"]}
    with pytest.raises(LookupError):
        reads.inspect("a", "2")


def test_visas_page_uses_stable_insert_order(data):
    g, reads, _, now = data
    for i in range(4):
        g.issue(draft("Buy a monitor under $300.", "other", now[0]))
    first = reads.visas(agent="other", limit=2)
    second = reads.visas(agent="other", limit=2, after=first["next_after"], through=first["through"])
    assert len(first["items"]) == len(second["items"]) == 2
    assert not second["has_more"]
    assert all(v["contract"]["agent_id"] == "other" for v in first["items"])


@pytest.mark.parametrize("kwargs", [{"limit":0}, {"limit":101}, {"after":-1}, {"after":2**63}, {"through":-1}, {"through":True}])
def test_invalid_bounds_fail(data, kwargs):
    _, reads, _, _ = data
    with pytest.raises(ValueError):
        reads.audit(**kwargs)


def test_read_connection_forbids_writes_and_indexes_are_repeatable(data):
    import sqlite3
    g, _, _, _ = data
    ReadModel(g)
    with g.read_transaction() as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM audit")


def test_read_pages_do_not_call_legacy_snapshot(data, monkeypatch):
    g, reads, _, _ = data
    monkeypatch.setattr(g, "snapshot", lambda: pytest.fail("unbounded snapshot used"))
    assert reads.audit(limit=1)["items"]
    assert reads.visas(limit=1)["items"]
    assert reads.approvals(limit=1)["items"]
    assert reads.inspect("a", "0")["request_id"] == "0"
