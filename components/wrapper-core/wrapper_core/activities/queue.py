"""Durable local work queue: the REFramework *transaction* model (UiPath: Add Queue Item, Get Transaction Item, Set Transaction Status).

One SQLite database (WAL) per queue at ``AUTOMAGIX_HOME/data/queues/<name>.db``. Safe for several threads and processes on one
machine (every operation is its own short transaction; ``next()`` claims an item atomically). It is deliberately *not* a
multi-machine queue: the platform runs unattended on a single VDI session, and a database on a network share is unsafe.

    q = queue.open("invoices")
    q.add("INV-1001", {"vendor": "Acme", "amount": 120.0}, priority=1)         # dispatcher
    item = q.next()                                                            # performer
    q.succeed(item, output={"posted": True})  |  q.fail_business(item, "missing PO")  |  q.fail_system(item, "portal down")

or let :func:`run_queue` run the whole performer loop with the platform's retry classification.

Statuses: new, in_progress, successful, failed_business, failed_system, needs_review, dead_letter (SLA expired / parked by hand; ``redrive`` re-queues).
Ordering for ``next()``: priority 1 (high) .. 3 (low), then earliest ``deadline`` (none last), then queue time, then id.
Telemetry: transitions emit ``item_processed`` events (success -> success, business failure -> skipped, exhausted system failure -> failed)
so run summaries and dashboards count queue items exactly like items processed with ``wrapper.run_item``.
"""
from __future__ import annotations

import builtins
import contextlib
import csv
import datetime as _dt
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Union

from ..logging import log_event, log_item
from ..retry import Category, DataError, Escalation, LogicError, RetryPolicy, Skipped, SystemUnavailable, run_with_retry
from ._common import data_dir, emit

STATUSES = ("new", "in_progress", "successful", "failed_business", "failed_system", "needs_review", "dead_letter")
MAX_PAYLOAD_BYTES = 1_000_000
MAX_REFERENCE_CHARS = 200
MAX_BULK = 100_000
_clock: Callable[[], float] = time.time  # tests may replace


class BusinessError(DataError):
    """Raise from a handler for a *business* exception: the item cannot be processed and retrying will not help."""


class NeedsReview(Exception):
    """Raise from a handler to hand the item to a human (UiPath Action Center). Not an error, not retried."""


def _ts(v: Union[None, float, int, str, _dt.datetime]) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, _dt.datetime):
        return (v if v.tzinfo else v.replace(tzinfo=_dt.timezone.utc)).timestamp()
    s = v.strip()
    dt = _dt.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)).timestamp()


@dataclass
class QueueItem:
    id: int
    reference: str
    payload: Dict[str, Any]
    priority: int
    status: str
    retry_no: int
    queue: str
    deadline: Optional[float] = None
    defer_until: Optional[float] = None
    started_at: Optional[float] = None
    reason: Optional[str] = None
    output: Optional[Dict[str, Any]] = None
    review_decision: Optional[str] = None
    review_note: Optional[str] = None
    max_retries: Optional[int] = None   # per-item override of the fail_system retry budget; None = use the caller value

    def __repr__(self) -> str:  # payloads may hold business data: keep repr short
        return f"QueueItem(id={self.id}, reference={self.reference!r}, status={self.status}, retry_no={self.retry_no})"


_COLUMNS = ("id", "reference", "payload", "priority", "status", "retry_no", "queued_at", "defer_until", "deadline", "created_at",
            "started_at", "ended_at", "worker", "reason", "output", "review_decision", "review_note", "max_retries")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reference TEXT NOT NULL,
  payload TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 2,
  status TEXT NOT NULL DEFAULT 'new',
  retry_no INTEGER NOT NULL DEFAULT 0,
  queued_at REAL NOT NULL,
  defer_until REAL,
  deadline REAL,
  created_at REAL NOT NULL,
  started_at REAL,
  ended_at REAL,
  worker TEXT,
  reason TEXT,
  output TEXT,
  review_decision TEXT,
  review_note TEXT,
  max_retries INTEGER
);
CREATE INDEX IF NOT EXISTS idx_items_pick ON items(status, priority, queued_at);
CREATE INDEX IF NOT EXISTS idx_items_ref ON items(reference);
"""


class Queue:
    def __init__(self, name: str, path: Path):
        self.name, self.path = name, path
        with self._tx() as c:
            for stmt in _SCHEMA.strip().split(";"):
                if stmt.strip():
                    c.execute(stmt)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(items)").fetchall()}
            if "max_retries" not in cols:  # databases written before per-item retry limits existed
                c.execute("ALTER TABLE items ADD COLUMN max_retries INTEGER")

    # -- plumbing ----------------------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One short IMMEDIATE transaction on a fresh connection (cross-thread / cross-process safe)."""
        conn = self._connect()
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise SystemUnavailable(f"queue {self.name!r} is busy: {exc}") from exc
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    def _row_to_item(self, r: sqlite3.Row) -> QueueItem:
        return QueueItem(id=r["id"], reference=r["reference"], payload=json.loads(r["payload"]), priority=r["priority"],
                         status=r["status"], retry_no=r["retry_no"], queue=self.name, deadline=r["deadline"],
                         defer_until=r["defer_until"], started_at=r["started_at"], reason=r["reason"],
                         output=json.loads(r["output"]) if r["output"] else None, review_decision=r["review_decision"],
                         review_note=r["review_note"], max_retries=r["max_retries"])

    # -- dispatcher side ---------------------------------------------------------------------------------
    @staticmethod
    def _check_new(reference: Any, payload: Any, priority: Any, max_retries: Any) -> "tuple[str, str]":
        ref = str(reference)
        if not ref or len(ref) > MAX_REFERENCE_CHARS or any(ord(ch) < 32 for ch in ref):
            raise ValueError(f"reference must be 1-{MAX_REFERENCE_CHARS} printable characters")
        if priority not in (1, 2, 3):
            raise ValueError("priority must be 1 (high), 2 (normal) or 3 (low)")
        if max_retries is not None and not (isinstance(max_retries, int) and not isinstance(max_retries, bool) and 0 <= max_retries <= 20):
            raise ValueError("max_retries must be an integer 0-20")
        body = json.dumps(payload or {}, default=str)
        if len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
        return ref, body

    def add(self, reference: str, payload: Optional[Dict[str, Any]] = None, priority: int = 2, defer_until=None, deadline=None,
            allow_duplicate: bool = False, max_retries: Optional[int] = None) -> bool:
        """Add an item. Returns False (nothing added) if `reference` already exists and not `allow_duplicate`.

        ``defer_until`` hides the item until then; ``deadline`` is its SLA (earlier deadlines are served first, ``stats()`` counts breaches);
        ``max_retries`` overrides the system-failure retry budget for this item."""
        ref, body = self._check_new(reference, payload, priority, max_retries)
        now = _clock()
        with self._tx() as c:
            if not allow_duplicate and c.execute("SELECT 1 FROM items WHERE reference=? LIMIT 1", (ref,)).fetchone():
                added = False
            else:
                c.execute("INSERT INTO items(reference,payload,priority,status,queued_at,defer_until,deadline,created_at,max_retries) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                          (ref, body, priority, "new", now, _ts(defer_until), _ts(deadline), now, max_retries))
                added = True
        emit("queue.add", queue=self.name, added=added)
        return added

    def add_many(self, items: Any, priority: int = 2, defer_until=None, deadline=None, dedupe: bool = True,
                 max_retries: Optional[int] = None) -> Dict[str, Any]:
        """Bulk add in ONE transaction (all or nothing). Each element is ``(reference, payload)`` or a dict with ``reference``, ``payload`` and
        optional ``priority`` / ``defer_until`` / ``deadline`` / ``max_retries``. Duplicate references (already queued, or repeated in the batch)
        are skipped when ``dedupe``. At most 100,000 items per call. Returns ``{"added": n, "duplicates": n, "duplicate_references": [first 20]}``."""
        rows = []
        for it in items:
            if len(rows) >= MAX_BULK:
                raise ValueError(f"add_many accepts at most {MAX_BULK} items per call")
            d = it if isinstance(it, dict) else {"reference": it[0], "payload": it[1] if len(it) > 1 else {}}
            pr = d.get("priority", priority)
            mr = d.get("max_retries", max_retries)
            ref, body = self._check_new(d["reference"], d.get("payload"), pr, mr)
            rows.append((ref, body, pr, _ts(d.get("defer_until", defer_until)), _ts(d.get("deadline", deadline)), mr))
        now = _clock()
        added, dups = 0, []
        with self._tx() as c:
            seen = set()
            for ref, body, pr, du, dl, mr in rows:
                if dedupe and (ref in seen or c.execute("SELECT 1 FROM items WHERE reference=? LIMIT 1", (ref,)).fetchone()):
                    dups.append(ref)
                    continue
                seen.add(ref)
                c.execute("INSERT INTO items(reference,payload,priority,status,queued_at,defer_until,deadline,created_at,max_retries) "
                          "VALUES(?,?,?,?,?,?,?,?,?)", (ref, body, pr, "new", now, du, dl, now, mr))
                added += 1
        emit("queue.add_many", queue=self.name, added=added, duplicates=len(dups))
        return {"added": added, "duplicates": len(dups), "duplicate_references": dups[:20]}

    # -- performer side ----------------------------------------------------------------------------------
    def next(self, worker: Optional[str] = None) -> Optional[QueueItem]:
        """Atomically claim the next available item (status -> in_progress), or None."""
        import os
        now = _clock()
        with self._tx() as c:
            r = c.execute(
                "SELECT * FROM items WHERE status='new' AND (defer_until IS NULL OR defer_until<=?) "
                "ORDER BY priority ASC, (deadline IS NULL) ASC, deadline ASC, queued_at ASC, id ASC LIMIT 1", (now,)).fetchone()
            if r is None:
                return None
            c.execute("UPDATE items SET status='in_progress', started_at=?, worker=? WHERE id=?",
                      (now, worker or f"pid{os.getpid()}", r["id"]))
            r = c.execute("SELECT * FROM items WHERE id=?", (r["id"],)).fetchone()
        return self._row_to_item(r)

    def _finish(self, item: QueueItem, new_status: str, reason: Optional[str] = None, output: Optional[dict] = None) -> sqlite3.Row:
        with self._tx() as c:
            cur = c.execute("UPDATE items SET status=?, ended_at=?, reason=?, output=? WHERE id=? AND status='in_progress'",
                            (new_status, _clock(), reason, json.dumps(output, default=str) if output is not None else None, item.id))
            if cur.rowcount != 1:
                raise LogicError(f"queue item {item.reference!r} is not in progress (already finished or re-queued)")
            return c.execute("SELECT * FROM items WHERE id=?", (item.id,)).fetchone()

    def _duration_ms(self, row: sqlite3.Row) -> Optional[int]:
        return int((row["ended_at"] - row["started_at"]) * 1000) if row["started_at"] and row["ended_at"] else None

    def succeed(self, item: QueueItem, output: Optional[Dict[str, Any]] = None) -> None:
        row = self._finish(item, "successful", None, output)
        item.status = "successful"
        log_item(item.reference, "success", duration_ms=self._duration_ms(row), queue=self.name, retry_no=item.retry_no)

    def fail_business(self, item: QueueItem, reason: str) -> None:
        """Business exception: the item is not processable (bad/missing data). Never retried."""
        row = self._finish(item, "failed_business", reason)
        item.status, item.reason = "failed_business", reason
        log_item(item.reference, "skipped", duration_ms=self._duration_ms(row), queue=self.name, reason=reason[:200],
                 error_category="BUSINESS")

    def fail_system(self, item: QueueItem, reason: str, max_retries: int = 3) -> str:
        """System exception: re-queue (retry_no + 1) until `max_retries` is exhausted, then fail. Returns the new status."""
        if item.max_retries is not None:
            max_retries = item.max_retries
        if item.retry_no < max_retries:
            with self._tx() as c:
                cur = c.execute("UPDATE items SET status='new', retry_no=retry_no+1, queued_at=?, started_at=NULL, worker=NULL, reason=? "
                                "WHERE id=? AND status='in_progress'", (_clock(), reason, item.id))
                if cur.rowcount != 1:
                    raise LogicError(f"queue item {item.reference!r} is not in progress")
            item.retry_no += 1
            item.status, item.reason = "new", reason
            log_event("queue_retry", {"queue": self.name, "reference": item.reference, "retry_no": item.retry_no, "reason": reason[:200]})
            return "new"
        row = self._finish(item, "failed_system", reason)
        item.status, item.reason = "failed_system", reason
        log_item(item.reference, "failed", duration_ms=self._duration_ms(row), queue=self.name, reason=reason[:200],
                 error_category="SYSTEM", retry_no=item.retry_no)
        return "failed_system"

    # -- human in the loop (Action Center) -----------------------------------------------------------------
    def needs_review(self, item: QueueItem, reason: str) -> None:
        self._finish(item, "needs_review", reason)
        item.status, item.reason = "needs_review", reason
        log_event("queue_needs_review", {"queue": self.name, "reference": item.reference, "reason": reason[:200]})

    def review_items(self) -> List[QueueItem]:
        return self.items("needs_review")

    def resolve_review(self, item: "QueueItem | int", approve: bool, note: str = "") -> None:
        """Approve -> the item goes back to `new` for a performer; reject -> `failed_business`."""
        item_id = item if isinstance(item, int) else item.id
        with self._tx() as c:
            r = c.execute("SELECT * FROM items WHERE id=? AND status='needs_review'", (item_id,)).fetchone()
            if r is None:
                raise LogicError(f"queue item {item_id} is not waiting for review")
            if approve:
                c.execute("UPDATE items SET status='new', queued_at=?, review_decision='approved', review_note=?, started_at=NULL, worker=NULL WHERE id=?",
                          (_clock(), note, item_id))
            else:
                c.execute("UPDATE items SET status='failed_business', ended_at=?, review_decision='rejected', review_note=?, "
                          "reason='rejected in review' WHERE id=?", (_clock(), note, item_id))
        log_event("queue_review_resolved", {"queue": self.name, "reference": r["reference"], "approved": bool(approve)})
        if not approve:
            log_item(r["reference"], "skipped", queue=self.name, reason="rejected in review", error_category="BUSINESS")

    # -- dead letters, SLA, redrive ------------------------------------------------------------------------------
    def dead_letter(self, item: QueueItem, reason: str) -> None:
        """Park an in-progress item you cannot process and do not want retried (it stays visible; ``redrive`` re-queues it)."""
        self._finish(item, "dead_letter", reason)
        item.status, item.reason = "dead_letter", reason
        log_event("queue_dead_letter", {"queue": self.name, "reference": item.reference, "reason": reason[:200]})

    def expire_overdue(self, now: Optional[float] = None) -> int:
        """Items still ``new`` whose SLA ``deadline`` has passed move to ``dead_letter`` (reason "SLA deadline passed"). Returns how many."""
        t = _clock() if now is None else float(now)
        with self._tx() as c:
            cur = c.execute("UPDATE items SET status='dead_letter', ended_at=?, reason='SLA deadline passed before processing' "
                            "WHERE status='new' AND deadline IS NOT NULL AND deadline < ?", (t, t))
            n = cur.rowcount
        if n:
            log_event("queue_sla_expired", {"queue": self.name, "items": n})
        return n

    def redrive(self, statuses: Any = ("dead_letter", "failed_system"), reference: Optional[str] = None, reset_retries: bool = True,
                limit: int = 1000) -> int:
        """Put dead-lettered / system-failed items back to ``new`` (optionally only one ``reference``). Returns how many were re-queued."""
        sts = list(statuses) if isinstance(statuses, (list, tuple, set)) else [statuses]
        if not sts or any(x not in ("dead_letter", "failed_system", "failed_business", "needs_review") for x in sts):
            raise ValueError("redrive: statuses must be among dead_letter, failed_system, failed_business, needs_review")
        marks = ",".join("?" for _ in sts)
        sql = f"SELECT id FROM items WHERE status IN ({marks})" + (" AND reference=?" if reference is not None else "") + " ORDER BY id LIMIT ?"
        args = list(sts) + ([str(reference)] if reference is not None else []) + [max(1, min(int(limit), 100_000))]
        with self._tx() as c:
            ids = [r["id"] for r in c.execute(sql, args).fetchall()]
            for i in ids:
                c.execute("UPDATE items SET status='new', queued_at=?, started_at=NULL, ended_at=NULL, worker=NULL, reason=NULL"
                          + (", retry_no=0" if reset_retries else "") + " WHERE id=?", (_clock(), i))
        if ids:
            log_event("queue_redrive", {"queue": self.name, "items": len(ids)})
        return len(ids)

    def stats(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Operational numbers: ``counts``, ``oldest_waiting_age_s`` (oldest visible ``new`` item), ``oldest_in_progress_age_s``,
        ``deferred`` (hidden until later), ``overdue`` (past deadline, unfinished), ``sla_breached`` (finished after its deadline),
        ``completed_last_hour`` and ``dead_letter``."""
        t = _clock() if now is None else float(now)
        conn = self._connect()
        try:
            def one(sql: str, *a: Any) -> Any:
                return conn.execute(sql, a).fetchone()[0]
            waiting = one("SELECT MIN(queued_at) FROM items WHERE status='new' AND (defer_until IS NULL OR defer_until<=?)", t)
            running = one("SELECT MIN(started_at) FROM items WHERE status='in_progress'")
            out = {
                "oldest_waiting_age_s": round(t - waiting, 3) if waiting is not None else None,
                "oldest_in_progress_age_s": round(t - running, 3) if running is not None else None,
                "deferred": one("SELECT COUNT(*) FROM items WHERE status='new' AND defer_until>?", t),
                "overdue": one("SELECT COUNT(*) FROM items WHERE status IN ('new','in_progress') AND deadline IS NOT NULL AND deadline<?", t),
                "sla_breached": one("SELECT COUNT(*) FROM items WHERE ended_at IS NOT NULL AND deadline IS NOT NULL AND ended_at>deadline"),
                "completed_last_hour": one("SELECT COUNT(*) FROM items WHERE status='successful' AND ended_at>=?", t - 3600),
            }
        finally:
            conn.close()
        out["counts"] = self.counts()
        out["dead_letter"] = out["counts"]["dead_letter"]
        return out

    # -- inspection / housekeeping --------------------------------------------------------------------------
    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in STATUSES}
        conn = self._connect()
        try:
            for r in conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status"):
                out[r["status"]] = r["n"]
        finally:
            conn.close()
        return out

    def items(self, status: Optional[str] = None, limit: int = 1000) -> List[QueueItem]:
        if status is not None and status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        conn = self._connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id LIMIT ?", (status, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
        return [self._row_to_item(r) for r in rows]

    def get(self, reference: str) -> Optional[QueueItem]:
        conn = self._connect()
        try:
            r = conn.execute("SELECT * FROM items WHERE reference=? ORDER BY id DESC LIMIT 1", (str(reference),)).fetchone()
        finally:
            conn.close()
        return self._row_to_item(r) if r else None

    def requeue_stale(self, older_than_s: float, max_retries: int = 3) -> int:
        """Items stuck in_progress longer than `older_than_s` (a crashed worker) go back to `new` (retry_no + 1), or fail
        as abandoned once `max_retries` is used up. Returns how many items were touched."""
        cutoff = _clock() - older_than_s
        touched = 0
        with self._tx() as c:
            for r in c.execute("SELECT * FROM items WHERE status='in_progress' AND started_at < ?", (cutoff,)).fetchall():
                if r["retry_no"] < max_retries:
                    c.execute("UPDATE items SET status='new', retry_no=retry_no+1, queued_at=?, started_at=NULL, worker=NULL, "
                              "reason='re-queued: worker did not finish' WHERE id=?", (_clock(), r["id"]))
                else:
                    c.execute("UPDATE items SET status='failed_system', ended_at=?, reason='abandoned: worker did not finish' WHERE id=?",
                              (_clock(), r["id"]))
                touched += 1
        if touched:
            log_event("queue_requeue_stale", {"queue": self.name, "items": touched})
        return touched

    def export_csv(self, path: "str | Path", escape_formulas: bool = True) -> str:
        """Dump all items. Text that Excel would evaluate as a formula (``= + - @ tab CR``) is prefixed with ``'`` unless
        ``escape_formulas=False`` (queue payloads/errors can contain attacker-supplied text)."""
        from .excel import csv_safe
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
        finally:
            conn.close()
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with builtins.open(dest, "w", encoding="utf-8", newline="") as f:  # module-level open() is the queue opener
            w = csv.writer(f)
            w.writerow(_COLUMNS)
            for r in rows:
                w.writerow([csv_safe(r[c], escape_formulas) for c in _COLUMNS])
        return str(dest)


def _jsonl_export(self: "Queue", path: "str | Path") -> str:
    """One JSON object per line (payload/output parsed). For machines; use export_csv for spreadsheets."""
    conn = self._connect()
    try:
        rows = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    finally:
        conn.close()
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with builtins.open(dest, "w", encoding="utf-8", newline="") as f:
        for r in rows:
            rec = {c: r[c] for c in _COLUMNS}
            for k in ("payload", "output"):
                rec[k] = json.loads(rec[k]) if rec[k] else None
            f.write(json.dumps(rec, default=str) + chr(10))
    return str(dest)


Queue.export_jsonl = _jsonl_export  # type: ignore[attr-defined]


def open(name: str, path: "str | Path | None" = None) -> Queue:  # noqa: A001 - mirrors the documented API (queue.open)
    """Open (creating if needed) the named queue. `path` overrides the default location (tests, shared scratch)."""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", name):
        raise ValueError("queue name: letters, digits, '.', '_', '-' (max 64)")
    p = Path(path) if path else data_dir("queues") / f"{name}.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return Queue(name, p)


# --- performer loop ---------------------------------------------------------------------------------------
def run_queue(q: Queue, handler: Callable[[QueueItem], Any], policy: Optional[RetryPolicy] = None, worker: Optional[str] = None,
              max_items: Optional[int] = None, max_seconds: Optional[float] = None, system_retries: int = 3,
              max_consecutive_system_failures: int = 3, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Process items until the queue is empty or a limit is hit. Never loops forever.

    For each item `handler(item)` runs under the platform retry engine (transient network/auth/availability errors are
    retried in place per `policy`). Outcome mapping:
      returns                          -> succeed (a returned dict becomes the item's output)
      raises DataError / BusinessError -> fail_business (no retry)
      raises NeedsReview               -> needs_review
      anything else (after in-place retries) -> fail_system(item, system_retries): re-queued, then failed
    A run of `max_consecutive_system_failures` system failures stops the loop (`stopped_reason`): the target is down or the code is
    broken and hammering it helps nobody - let the run fail visibly.
    """
    summary: Dict[str, Any] = {"processed": 0, "succeeded": 0, "business_failures": 0, "system_failures": 0, "retried": 0,
                               "needs_review": 0, "stopped_reason": "empty"}
    started = time.monotonic()
    consecutive = 0
    while True:
        if max_items is not None and summary["processed"] >= max_items:
            summary["stopped_reason"] = "max_items"
            break
        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            summary["stopped_reason"] = "max_seconds"
            break
        item = q.next(worker)
        if item is None:
            break
        summary["processed"] += 1

        def attempt(item=item):
            try:
                return ("ok", handler(item))
            except NeedsReview as nr:
                return ("review", str(nr) or "needs human review")

        try:
            result = run_with_retry(attempt, policy, item_id=item.reference, sleep=sleep)
        except Escalation as esc:
            if esc.category is Category.DATA_ERROR:
                q.fail_business(item, str(esc))
                summary["business_failures"] += 1
            else:
                status = q.fail_system(item, str(esc), system_retries)
                summary["system_failures" if status == "failed_system" else "retried"] += 1
                consecutive += 1
        else:
            if isinstance(result, Skipped):
                q.fail_business(item, result.reason or "business exception")
                summary["business_failures"] += 1
            elif result[0] == "review":
                q.needs_review(item, result[1])
                summary["needs_review"] += 1
                consecutive = 0
            else:
                q.succeed(item, output=result[1] if isinstance(result[1], dict) else None)
                summary["succeeded"] += 1
                consecutive = 0
        if consecutive >= max_consecutive_system_failures:
            summary["stopped_reason"] = "consecutive_system_failures"
            break
    emit("queue.run_queue", queue=q.name, **{k: v for k, v in summary.items() if k != "stopped_reason"}, stopped=summary["stopped_reason"])
    return summary
