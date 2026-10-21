from __future__ import annotations

import json
import sqlite3

import pytest

from wrapper_core.activities import queue as qmod
from wrapper_core.retry import LogicError


@pytest.fixture
def q(tmp_path):
    t = [1000.0]
    qmod._clock = lambda: t[0]
    q = qmod.open("more", tmp_path / "more.db")
    q.t = t
    yield q
    qmod._clock = __import__("time").time


# --- bulk add -----------------------------------------------------------------------------------------------
def test_add_many_is_one_atomic_deduplicating_batch(q):
    q.add("EXISTING", {})
    res = q.add_many([("A", {"n": 1}), {"reference": "B", "payload": {"n": 2}, "priority": 1}, ("A", {"n": 3}), ("EXISTING", {})])
    assert res == {"added": 2, "duplicates": 2, "duplicate_references": ["A", "EXISTING"]}
    assert [q.next().reference for _ in range(3)] == ["B", "EXISTING", "A"]       # priority 1 first, then queue order
    assert q.next() is None


def test_add_many_priority_order_and_no_dedupe(q):
    q.add_many([("L", {}, ), {"reference": "H", "payload": {}, "priority": 1}])
    assert q.next().reference == "H"
    assert q.add_many([("L", {})], dedupe=False)["added"] == 1


def test_add_many_rolls_back_on_one_bad_item(q):
    with pytest.raises(ValueError):
        q.add_many([("ok", {}), {"reference": "bad", "payload": {}, "priority": 9}])
    assert q.counts()["new"] == 0
    with pytest.raises(ValueError):
        q.add_many([("ok", {}), ("x" * 201, {})])
    assert q.counts()["new"] == 0


def test_bulk_cap(q, monkeypatch):
    monkeypatch.setattr(qmod, "MAX_BULK", 3)
    with pytest.raises(ValueError, match="at most 3"):
        q.add_many((f"r{i}", {}) for i in range(10))
    assert q.counts()["new"] == 0


@pytest.mark.parametrize("ref", ["", "x" * 201, "bad" + chr(10) + "ref", "bad" + chr(0)])
def test_reference_validation(q, ref):
    with pytest.raises(ValueError):
        q.add(ref, {})


def test_payload_and_retry_limits(q):
    with pytest.raises(ValueError):
        q.add("big", {"blob": "x" * (qmod.MAX_PAYLOAD_BYTES + 1)})
    for bad in (-1, 21, True, "3"):
        with pytest.raises(ValueError):
            q.add("r", {}, max_retries=bad)


# --- per-item retries, dead letters ---------------------------------------------------------------------------------
def test_per_item_max_retries_overrides_the_caller_budget(q):
    q.add("A", {}, max_retries=1)
    item = q.next()
    assert q.fail_system(item, "boom", max_retries=5) == "new"          # retry 1 of 1
    item = q.next()
    assert q.fail_system(item, "boom", max_retries=5) == "failed_system"
    q.add("B", {}, max_retries=0)
    assert q.fail_system(q.next(), "boom", max_retries=5) == "failed_system"


def test_dead_letter_and_redrive(q):
    q.add("A", {"x": 1})
    item = q.next()
    q.dead_letter(item, "cannot parse")
    assert q.counts()["dead_letter"] == 1 and q.get("A").reason == "cannot parse" and q.next() is None
    assert q.redrive(reference="NOPE") == 0
    assert q.redrive() == 1
    again = q.next()
    assert again.reference == "A" and again.retry_no == 0 and again.reason is None


def test_redrive_includes_exhausted_system_failures_and_validates(q):
    q.add("S", {}, max_retries=0)
    q.fail_system(q.next(), "down")
    assert q.counts()["failed_system"] == 1
    assert q.redrive(statuses=["failed_system"], reset_retries=False) == 1
    with pytest.raises(ValueError):
        q.redrive(statuses=["successful"])
    with pytest.raises(ValueError):
        q.redrive(statuses=[])


def test_dead_letter_requires_in_progress(q):
    q.add("A", {})
    item = q.items()[0]
    with pytest.raises(LogicError):
        q.dead_letter(item, "nope")


# --- SLA ----------------------------------------------------------------------------------------------------------------
def test_expire_overdue_moves_unprocessed_items_to_dead_letter(q):
    q.add("late", {}, deadline=1100)
    q.add("fine", {}, deadline=2000)
    q.add("none", {})
    q.t[0] = 1200
    assert q.expire_overdue() == 1
    assert q.get("late").status == "dead_letter" and q.get("late").reason.startswith("SLA deadline passed")
    assert q.get("fine").status == "new" and q.get("none").status == "new"
    assert q.expire_overdue() == 0


def test_earliest_deadline_first_within_a_priority(q):
    q.add_many([{"reference": "later", "payload": {}, "deadline": 5000}, {"reference": "sooner", "payload": {}, "deadline": 3000}, ("nodl", {})])
    assert [q.next().reference for _ in range(3)] == ["sooner", "later", "nodl"]


def test_defer_until_hides_items(q):
    q.add("A", {}, defer_until=1500)
    assert q.next() is None
    q.t[0] = 1500
    assert q.next().reference == "A"


# --- stats --------------------------------------------------------------------------------------------------------------
def test_stats(q):
    q.add("old", {}, deadline=1010)
    q.add("deferred", {}, defer_until=9999)
    q.add("done", {}, deadline=1500)
    q.t[0] = 1100
    first = q.next()                                       # earliest deadline: "old"
    assert first.reference == "old"
    q.t[0] = 1200
    s = q.stats()
    assert s["counts"]["in_progress"] == 1 and s["counts"]["new"] == 2
    assert s["deferred"] == 1 and s["overdue"] == 1                         # "old" is in progress past its deadline
    assert s["oldest_waiting_age_s"] == 200.0 and s["oldest_in_progress_age_s"] == 100.0
    q.succeed(first, {"ok": True})
    s = q.stats()
    assert s["sla_breached"] == 1 and s["completed_last_hour"] == 1 and s["overdue"] == 0
    assert s["dead_letter"] == 0


def test_stats_on_an_empty_queue(q):
    s = q.stats()
    assert s["oldest_waiting_age_s"] is None and s["oldest_in_progress_age_s"] is None and s["overdue"] == 0


# --- export + migration -----------------------------------------------------------------------------------------------------
def test_export_jsonl_parses_payloads(q, tmp_path):
    q.add("A", {"vendor": "=cmd|evil"})
    item = q.next()
    q.succeed(item, {"posted": True})
    path = q.export_jsonl(tmp_path / "out" / "q.jsonl")
    rows = [json.loads(line) for line in open(path, encoding="utf-8")]
    assert rows[0]["reference"] == "A" and rows[0]["payload"] == {"vendor": "=cmd|evil"} and rows[0]["output"] == {"posted": True} and "max_retries" in rows[0]


def test_csv_export_still_escapes_formulas_and_has_new_column(q, tmp_path):
    q.add("=HYPERLINK(1)", {})
    text = open(q.export_csv(tmp_path / "q.csv"), encoding="utf-8").read()
    assert "max_retries" in text.splitlines()[0] and "'=HYPERLINK(1)" in text


def test_old_databases_are_migrated_in_place(tmp_path):
    p = tmp_path / "old.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""CREATE TABLE items(id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT NOT NULL, payload TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 2,
        status TEXT NOT NULL DEFAULT 'new', retry_no INTEGER NOT NULL DEFAULT 0, queued_at REAL NOT NULL, defer_until REAL, deadline REAL, created_at REAL NOT NULL,
        started_at REAL, ended_at REAL, worker TEXT, reason TEXT, output TEXT, review_decision TEXT, review_note TEXT);
        INSERT INTO items(reference,payload,queued_at,created_at) VALUES('OLD','{"k":1}',1.0,1.0);""")
    conn.commit()
    conn.close()
    q = qmod.open("old", p)
    item = q.next()
    assert item.reference == "OLD" and item.payload == {"k": 1} and item.max_retries is None
    q.add("NEW", {}, max_retries=2)
    assert q.get("NEW").max_retries == 2
    assert q.stats()["counts"]["in_progress"] == 1


def test_items_rejects_unknown_status(q):
    with pytest.raises(ValueError):
        q.items("hacked'; DROP TABLE items;--")
