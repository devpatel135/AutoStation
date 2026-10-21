from __future__ import annotations

import csv
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from wrapper_core import retry, runctx
from wrapper_core.activities import queue
from wrapper_core.retry import AuthExpired, DataError, Escalation, LogicError, NetworkError, RetryPolicy

WRAPPER_DIR = str(Path(__file__).resolve().parents[2])


def events(name=None):
    return [e for e in runctx.current().read_events() if name is None or e["event"] == name]


def items_logged():
    return [e["data"] for e in events("item_processed")]


@pytest.fixture
def q():
    return queue.open("invoices")


def test_add_dedupes_by_reference(q):
    assert q.add("INV-1", {"amount": 10}) is True
    assert q.add("INV-1", {"amount": 99}) is False                      # same reference: not added
    assert q.add("INV-1", {"amount": 99}, allow_duplicate=True) is True
    assert q.counts()["new"] == 2
    with pytest.raises(ValueError):
        q.add("x", priority=4)
    with pytest.raises(ValueError):
        queue.open("bad name!")


def test_next_order_priority_then_deadline_then_fifo(q):
    q.add("low", priority=3)
    q.add("normal-1", priority=2)
    q.add("normal-2", priority=2)
    q.add("high", priority=1)
    q.add("normal-urgent", priority=2, deadline=1000.0)                 # earlier deadline beats FIFO within a priority
    order = []
    while True:
        it = q.next()
        if it is None:
            break
        order.append(it.reference)
    assert order == ["high", "normal-urgent", "normal-1", "normal-2", "low"]


def test_defer_until_is_honoured(q, monkeypatch):
    now = [5000.0]
    monkeypatch.setattr(queue, "_clock", lambda: now[0])
    q.add("later", defer_until=5100.0)
    q.add("now")
    assert q.next().reference == "now" and q.next() is None
    now[0] = 5100.0
    assert q.next().reference == "later"
    q.add("iso", defer_until="2999-01-01T00:00:00Z")
    assert q.next() is None


def test_item_fields_and_success_flow_emit_item_events(q):
    q.add("A", {"vendor": "Acme", "n": [1, 2]})
    it = q.next(worker="w1")
    assert (it.reference, it.payload, it.retry_no, it.status, it.priority) == ("A", {"vendor": "Acme", "n": [1, 2]}, 0, "in_progress", 2)
    assert "Acme" not in repr(it)                                         # payloads stay out of reprs
    q.succeed(it, output={"posted": True})
    assert q.counts()["successful"] == 1 and q.get("A").output == {"posted": True}
    ev = items_logged()[-1]
    assert ev["outcome"] == "success" and ev["item_id"] == "A" and ev["queue"] == "invoices"
    with pytest.raises(LogicError):
        q.succeed(it)                                                      # already finished


def test_business_failure_is_skipped_not_retried(q):
    q.add("B")
    it = q.next()
    q.fail_business(it, "missing PO number")
    assert q.counts()["failed_business"] == 1 and q.next() is None
    ev = items_logged()[-1]
    assert ev["outcome"] == "skipped" and ev["reason"] == "missing PO number" and ev["queue"] == "invoices"


def test_system_failure_requeues_then_fails_after_max_retries(q):
    q.add("S")
    seen = []
    for expected_retry in range(4):
        it = q.next()
        seen.append(it.retry_no)
        status = q.fail_system(it, "portal down", max_retries=3)
    assert seen == [0, 1, 2, 3] and status == "failed_system"
    assert q.next() is None and q.counts()["failed_system"] == 1
    assert len(events("queue_retry")) == 3
    ev = items_logged()[-1]
    assert ev["outcome"] == "failed" and ev["error_category"] == "SYSTEM" and ev["retry_no"] == 3
    assert [e["outcome"] for e in items_logged()].count("failed") == 1    # retries are not counted as items


def test_needs_review_roundtrip(q):
    q.add("R1", {"amount": 99999})
    q.add("R2")
    a, b = q.next(), q.next()
    q.needs_review(a, "amount above approval limit")
    q.needs_review(b, "unusual vendor")
    assert q.counts()["needs_review"] == 2 and not items_logged()          # no item outcome yet: it is not finished
    pending = q.review_items()
    assert {i.reference for i in pending} == {"R1", "R2"} and pending[0].reason
    q.resolve_review(pending[0], approve=True, note="ok by finance")
    again = q.next()
    assert again.reference == pending[0].reference and again.review_decision == "approved" and again.review_note == "ok by finance"
    q.succeed(again)
    q.resolve_review(pending[1].id, approve=False, note="fraud suspected")
    assert q.counts() == {"new": 0, "in_progress": 0, "successful": 1, "failed_business": 1, "failed_system": 0, "needs_review": 0, "dead_letter": 0}
    assert q.get(pending[1].reference).review_decision == "rejected"
    assert items_logged()[-1]["outcome"] == "skipped" and items_logged()[-1]["reason"] == "rejected in review"
    with pytest.raises(LogicError):
        q.resolve_review(pending[0], approve=True)                         # not waiting for review any more


def test_requeue_stale_recovers_crashed_workers(q, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(queue, "_clock", lambda: now[0])
    q.add("X")
    q.add("Y")
    q.next()
    q.next()                                                              # two workers "crash" here
    now[0] = 200.0
    assert q.requeue_stale(older_than_s=500) == 0                         # not stale yet
    assert q.requeue_stale(older_than_s=50) == 2
    assert q.counts()["new"] == 2
    again = q.next()
    assert again.retry_no == 1
    now[0] = 400.0
    q.next()                                                              # the second item is claimed again too (retry_no 1)
    now[0] = 600.0
    assert q.requeue_stale(older_than_s=10, max_retries=1) == 2
    assert q.counts()["failed_system"] == 2                              # both had used their single retry: abandoned, not looped forever


def test_export_csv(q, tmp_path):
    q.add("E1", {"k": "v"})
    q.succeed(q.next(), output={"ok": 1})
    path = q.export_csv(tmp_path / "out" / "q.csv")
    rows = list(csv.DictReader(open(path, encoding="utf-8", newline="")))
    assert rows[0]["reference"] == "E1" and rows[0]["status"] == "successful" and '"k"' in rows[0]["payload"]


def test_queues_are_isolated_and_persistent(tmp_path):
    a, b = queue.open("one"), queue.open("two")
    a.add("1")
    assert b.counts()["new"] == 0
    again = queue.open("one")
    assert again.counts()["new"] == 1 and again.path == a.path
    assert str(a.path).endswith(os.path.join("data", "queues", "one.db"))


def test_threads_never_claim_the_same_item(q):
    n = 60
    for i in range(n):
        q.add(f"T{i}")
    got, lock = [], threading.Lock()

    def worker(name):
        h = queue.open("invoices")
        while True:
            it = h.next(worker=name)
            if it is None:
                return
            with lock:
                got.append(it.reference)
            h.succeed(it)

    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(got) == sorted(f"T{i}" for i in range(n)) and len(set(got)) == n
    assert q.counts()["successful"] == n


def test_processes_never_claim_the_same_item(q, tmp_path):
    for i in range(40):
        q.add(f"P{i}")
    code = ("import sys; sys.path.insert(0, sys.argv[1])\n"
            "from wrapper_core.activities import queue\n"
            "q = queue.open('invoices')\n"
            "while True:\n"
            "    it = q.next()\n"
            "    if it is None: break\n"
            "    print(it.reference, flush=True)\n"
            "    q.succeed(it)\n")
    env = dict(os.environ, AUTOMAGIX_RUN_DIR=str(tmp_path / "rd"))
    procs = [subprocess.Popen([sys.executable, "-c", code, WRAPPER_DIR], stdout=subprocess.PIPE, text=True, env=env) for _ in range(3)]
    claimed = []
    for p in procs:
        out, _ = p.communicate(timeout=120)
        assert p.returncode == 0
        claimed += out.split()
    assert sorted(claimed) == sorted(f"P{i}" for i in range(40)) and len(set(claimed)) == 40


# --- performer loop --------------------------------------------------------------------------------------
FAST = RetryPolicy(max_retries=2, network_retry_wait_s=0, backoff_multiplier=1.0)
NOSLEEP = lambda s: None  # noqa: E731


def test_run_queue_maps_outcomes(q):
    for ref in ("ok", "ok-dict", "bad-data", "business", "review", "unknown-boom"):
        q.add(ref)

    def handler(item):
        r = item.reference
        if r == "ok-dict":
            return {"posted": item.reference}
        if r == "bad-data":
            raise DataError("vendor missing")
        if r == "business":
            raise queue.BusinessError("duplicate invoice")
        if r == "review":
            raise queue.NeedsReview("over limit")
        if r == "unknown-boom":
            raise RuntimeError("weird")

    s = queue.run_queue(q, handler, FAST, sleep=NOSLEEP, system_retries=0)
    assert s["processed"] == 6 and s["succeeded"] == 2 and s["business_failures"] == 2 and s["needs_review"] == 1 and s["system_failures"] == 1
    assert s["stopped_reason"] == "empty"
    c = q.counts()
    assert c["successful"] == 2 and c["failed_business"] == 2 and c["needs_review"] == 1 and c["failed_system"] == 1
    assert q.get("ok-dict").output == {"posted": "ok-dict"}


def test_run_queue_retries_transient_errors_in_place_and_requeues_system_failures(q):
    q.add("flaky")
    q.add("down")
    tries = {"flaky": 0, "down": 0}

    def handler(item):
        tries[item.reference] += 1
        if item.reference == "flaky" and tries["flaky"] <= 2:
            raise NetworkError("vpn dropped")                              # retried in place by the retry engine
        if item.reference == "down":
            raise AuthExpired("session expired")                           # auth: refresh-once then escalate -> queue-level retry
        return None

    s = queue.run_queue(q, handler, FAST, sleep=NOSLEEP, system_retries=1, max_consecutive_system_failures=99)
    assert tries["flaky"] == 3 and q.get("flaky").status == "successful"
    assert q.get("down").status == "failed_system" and q.get("down").retry_no == 1
    assert s["succeeded"] == 1 and s["retried"] == 1 and s["system_failures"] == 1
    assert [e["data"]["category"] for e in events("retry_event")].count("NETWORK_ERROR") == 2


def test_run_queue_stops_after_consecutive_system_failures(q):
    for i in range(10):
        q.add(f"I{i}")

    def handler(item):
        raise retry.SystemUnavailable("target down")

    s = queue.run_queue(q, handler, RetryPolicy(max_retries=0, network_retry_wait_s=0), sleep=NOSLEEP, system_retries=5,
                        max_consecutive_system_failures=3)
    assert s["stopped_reason"] == "consecutive_system_failures" and s["processed"] == 3
    assert q.counts()["new"] >= 7                                          # the rest was left alone for the next run


def test_run_queue_limits(q):
    for i in range(5):
        q.add(f"L{i}")
    s = queue.run_queue(q, lambda item: None, FAST, max_items=2)
    assert s["processed"] == 2 and s["stopped_reason"] == "max_items" and q.counts()["new"] == 3
    s = queue.run_queue(q, lambda item: None, FAST, max_seconds=0)
    assert s["stopped_reason"] == "max_seconds" and s["processed"] == 0
    s = queue.run_queue(q, lambda item: None, FAST)
    assert s["succeeded"] == 3 and s["stopped_reason"] == "empty"


def test_item_events_match_run_summary_counting(q):
    from wrapper_core.runner import _aggregate_items
    for ref in ("a", "b", "c"):
        q.add(ref)
    queue.run_queue(q, lambda it: (_ for _ in ()).throw(DataError("bad")) if it.reference == "b" else None, FAST, sleep=NOSLEEP)
    counts = _aggregate_items(events())
    assert counts == {"total": 3, "success": 2, "skipped": 1, "failed": 0}
