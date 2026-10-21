"""Human-in-the-loop approvals (UiPath: Action Center / Create Form Task + Wait For Form Task).

An unattended automation that reaches a decision it must not take alone (pay this invoice? delete these records?) creates an approval task
and waits; a person approves or rejects it on the same machine, from a tiny local page or from the command line.

    from wrapper_core.activities import approvals
    t = approvals.create("Pay invoice INV-1001", "Vendor Acme, amount 1,250.00", fields={"vendor": "Acme", "amount": "1250.00"},
                         expires_in_s=4 * 3600)
    d = approvals.wait(t.id, timeout_s=3600)
    if d.approved:
        pay()                      # d.status == "approved"; d.decided_by / d.note are available for the audit trail
    # d.approved is False for "rejected", "expired" and "timeout": there is no way to approve by silence (default deny)

Approver side:

    python -m wrapper_core approvals list [--status pending]
    python -m wrapper_core approvals show <id>
    python -m wrapper_core approvals approve <id> [--note "checked against PO"]      # or: reject
    python -m wrapper_core approvals ui                                              # local page (127.0.0.1, random port, session token)

Design and limits
* Tasks live under ``state/approvals/<id>.json`` (atomic writes, 128-bit unguessable ids; ids are validated against a strict pattern so they can never
  address another file). Decisions take a per-task lock, so two approvers cannot both win; a decided task is immutable apart from its audit trail.
* Every task has an expiry (default 24 h, max 30 days). After it, the task is ``expired`` and cannot be approved. Decisions are restricted to ``approve`` / ``reject``.
* The audit trail records who (the Windows user name on this machine), when, and the note. The user name stays in the local file; telemetry only carries a
  non-reversible user hash.
* Telemetry events (no titles, fields or notes - those can hold business data): ``approval_requested`` (approval_id, automation, expires_at, fields) and
  ``approval_decided`` (approval_id, decision, by_hash, waited_s) are emitted inside a run.
* The local approval page is NOT an identity system: whoever can reach 127.0.0.1 on this machine with the session token can decide. It protects against other
  web pages and other hosts (loopback only, Host/Origin/Fetch-Metadata checks, token in every request, no scripts or external resources, strict CSP), not against
  another local user who can read your process list. For stronger sign-off, require a second approver in your automation.
"""
from __future__ import annotations

import hmac
import html
import json
import os
import re
import secrets as _secrets
import sys
import threading
import time
import unicodedata
import urllib.parse
from datetime import timedelta
from dataclasses import dataclass, field
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..paths import Paths, check_name
from ..retry import DataError, LogicError
from ..util import iso, parse_iso, read_json, user_hash, utcnow, write_json
from ._common import emit

ID_RX = re.compile(r"^[A-Za-z0-9_-]{16,40}$")
FIELD_KEY_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")
DECISIONS = ("approve", "reject")
STATUSES = ("pending", "approved", "rejected", "expired")
MAX_TITLE, MAX_DESCRIPTION, MAX_FIELDS, MAX_FIELD_VALUE, MAX_NOTE, MAX_PENDING = 120, 2000, 30, 500, 500, 500
DEFAULT_TTL_S, MAX_TTL_S = 24 * 3600, 30 * 24 * 3600


class ApprovalError(LogicError):
    """The approval task does not exist, was already decided, or the request is invalid."""


@dataclass
class Decision:
    approved: bool
    status: str                      # approved | rejected | expired | timeout
    decided_by: Optional[str] = None
    note: str = ""


@dataclass
class Task:
    id: str
    automation: str
    title: str
    description: str
    fields: Dict[str, str]
    status: str
    created_at: str
    expires_at: str
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    decision_note: str = ""
    audit: List[dict] = field(default_factory=list)

    def __repr__(self) -> str:  # titles/fields may hold business data
        return f"Task(id={self.id}, automation={self.automation}, status={self.status})"


# --- plumbing -------------------------------------------------------------------------------------------
def _dir(paths: Optional[Paths] = None) -> Path:
    d = (paths or Paths()).state / "approvals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _clean(text: Any, limit: int, what: str, multiline: bool = False) -> str:
    s = str(text if text is not None else "")
    # drop control AND format characters: bidi overrides (U+202E ...) could make "1250.00" display reordered to the approver
    s = "".join(ch for ch in s if ch == "\t" or (multiline and ch == "\n")
                or (ord(ch) >= 32 and ord(ch) != 127 and unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp")))
    if len(s) > limit:
        raise ValueError(f"{what} is longer than {limit} characters")
    return s


def _who(by: Optional[str]) -> str:
    who = by or os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    return _clean(who, 64, "approver name") or "unknown"


def _check_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not ID_RX.fullmatch(task_id):
        raise ApprovalError("invalid approval id")
    return task_id


def _from_json(d: Any, task_id: str) -> Task:
    if not isinstance(d, dict) or d.get("id") != task_id or d.get("status") not in STATUSES:
        raise ApprovalError(f"approval {task_id}: unreadable record")
    return Task(id=task_id, automation=str(d.get("automation", "")), title=str(d.get("title", "")), description=str(d.get("description", "")),
                fields={str(k): str(v) for k, v in (d.get("fields") or {}).items()}, status=d["status"], created_at=str(d.get("created_at", "")),
                expires_at=str(d.get("expires_at", "")), decided_at=d.get("decided_at"), decided_by=d.get("decided_by"),
                decision_note=str(d.get("decision_note", "")), audit=list(d.get("audit") or []))


def _save(task: Task, paths: Optional[Paths]) -> None:
    write_json(_dir(paths) / f"{task.id}.json", task.__dict__)


class _Lock:
    """Short per-task lock file (O_EXCL). A lock older than 30 s belongs to a crashed process and is taken over."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self) -> "_Lock":
        deadline = time.monotonic() + 10
        while True:
            try:
                os.close(os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 30:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise ApprovalError("approval is being updated by someone else; try again")
                time.sleep(0.05)

    def __exit__(self, *exc: Any) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


def _telemetry(event: str, data: dict) -> None:
    """Only inside a run (a CLI decision must not create ad-hoc run folders); never raises."""
    if not os.environ.get("AUTOMAGIX_RUN_DIR"):
        return
    try:
        from ..logging import log_event
        log_event(event, data)
    except Exception:  # noqa: BLE001
        pass


def _expired(task: Task, now: Optional[Any] = None) -> bool:
    try:
        return task.status == "pending" and (now or utcnow()) >= parse_iso(task.expires_at)
    except ValueError:
        return True  # unparseable expiry: treat as expired (deny)


# --- API --------------------------------------------------------------------------------------------------
def create(title: str, description: str = "", fields: Optional[Dict[str, Any]] = None, automation: Optional[str] = None,
           expires_in_s: float = DEFAULT_TTL_S, notify: bool = True, paths: Optional[Paths] = None) -> Task:
    """Create a pending approval task. ``fields`` (scalars only, shown to the approver) hold the facts needed to decide."""
    paths = paths or Paths()
    title = _clean(title, MAX_TITLE, "title")
    if not title.strip():
        raise ValueError("title is required")
    description = _clean(description, MAX_DESCRIPTION, "description", multiline=True)
    fields = fields or {}
    if not isinstance(fields, dict) or len(fields) > MAX_FIELDS:
        raise ValueError(f"fields must be a mapping with at most {MAX_FIELDS} entries")
    clean_fields: Dict[str, str] = {}
    for k, v in fields.items():
        if not isinstance(k, str) or not FIELD_KEY_RX.fullmatch(k):
            raise ValueError(f"field name {str(k)[:40]!r} is invalid (letters, digits, space, '_', '.', '-'; max 40)")
        if isinstance(v, (dict, list, tuple, set)):
            raise ValueError(f"field {k!r}: only scalar values can be shown to an approver")
        clean_fields[k] = _clean(v, MAX_FIELD_VALUE, f"field {k!r}")
    ttl = float(expires_in_s)
    if not (0 < ttl <= MAX_TTL_S):
        raise ValueError(f"expires_in_s must be between 1 and {MAX_TTL_S} (30 days)")
    auto = check_name(automation or os.environ.get("AUTOMAGIX_AUTOMATION") or "adhoc")
    expire_due(paths)
    _maybe_prune(paths)
    if sum(1 for t in list_tasks("pending", paths)) >= MAX_PENDING:
        raise ApprovalError(f"too many pending approvals ({MAX_PENDING})")
    now = utcnow()
    task = Task(id=_secrets.token_urlsafe(16), automation=auto, title=title, description=description, fields=clean_fields, status="pending",
                created_at=iso(now), expires_at=iso(now + timedelta(seconds=ttl)),
                audit=[{"ts": iso(now), "event": "created", "by": _who(None), "note": ""}])
    _save(task, paths)
    _telemetry("approval_requested", {"approval_id": task.id, "automation": auto, "expires_at": task.expires_at, "fields": len(clean_fields)})
    emit("approvals.create", automation=auto)
    if notify:
        try:
            from .. import notify as _notify
            _notify.toast("Approval needed", f"{auto}: {title}")
        except Exception:  # noqa: BLE001 - feedback must never break the automation
            pass
    return task


def get(task_id: str, paths: Optional[Paths] = None) -> Task:
    """Load a task (marking it ``expired`` first if its time ran out). Raises ApprovalError if unknown or the id is malformed."""
    _check_id(task_id)
    p = _dir(paths) / f"{task_id}.json"
    task = _from_json(read_json(p, None), task_id) if p.exists() else None
    if task is None:
        raise ApprovalError(f"no approval {task_id}")
    if _expired(task):
        with _Lock(_dir(paths) / f"{task_id}.lock"):
            task = _from_json(read_json(p, None), task_id)
            if _expired(task):
                task.status, task.decided_at = "expired", iso()
                task.audit.append({"ts": iso(), "event": "expired", "by": "system", "note": "no decision before the expiry"})
                _save(task, paths)
    return task


def list_tasks(status: Optional[str] = None, paths: Optional[Paths] = None, limit: int = 500) -> List[Task]:
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    out: List[Task] = []
    for p in sorted(_dir(paths).glob("*.json")):
        if len(out) >= limit or not ID_RX.fullmatch(p.stem):
            continue
        try:
            t = _from_json(read_json(p, None), p.stem)
        except ApprovalError:
            continue
        if _expired(t):
            t = get(t.id, paths)
        if status is None or t.status == status:
            out.append(t)
    return sorted(out, key=lambda t: t.created_at)


def decide(task_id: str, decision: str, by: Optional[str] = None, note: str = "", paths: Optional[Paths] = None) -> Task:
    """Approve or reject a pending task (``decision`` is ``"approve"`` or ``"reject"``). Raises ApprovalError if it is not pending."""
    if decision not in DECISIONS:
        raise ApprovalError(f"decision must be one of {DECISIONS}")
    note = _clean(note, MAX_NOTE, "note", multiline=True)
    who = _who(by)
    get(task_id, paths)  # applies expiry
    with _Lock(_dir(paths) / f"{task_id}.lock"):
        task = _from_json(read_json(_dir(paths) / f"{task_id}.json", None), task_id)
        if _expired(task):
            raise ApprovalError("approval has expired and can no longer be decided")
        if task.status != "pending":
            raise ApprovalError(f"approval was already {task.status}")
        task.status = "approved" if decision == "approve" else "rejected"
        task.decided_at, task.decided_by, task.decision_note = iso(), who, note
        task.audit.append({"ts": task.decided_at, "event": decision, "by": who, "note": note})
        _save(task, paths)
    emit("approvals.decide", decision=decision)
    return task


def wait(task_id: str, timeout_s: float = 3600.0, poll_s: float = 2.0, paths: Optional[Paths] = None,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> Decision:
    """Block until the task is decided, expires, or ``timeout_s`` passes. Anything but an explicit approval returns ``approved=False``."""
    started = clock()
    while True:
        t = get(task_id, paths)
        if t.status != "pending":
            _telemetry("approval_decided", {"approval_id": t.id, "decision": t.status, "by_hash": user_hash() if t.decided_by else "",
                                            "waited_s": round(clock() - started, 1)})
            return Decision(t.status == "approved", t.status, t.decided_by, t.decision_note)
        if clock() - started >= timeout_s:
            _telemetry("approval_decided", {"approval_id": t.id, "decision": "timeout", "by_hash": "", "waited_s": round(clock() - started, 1)})
            return Decision(False, "timeout")
        sleep(max(0.05, min(poll_s, timeout_s - (clock() - started))))


def expire_due(paths: Optional[Paths] = None) -> int:
    """Mark every pending task whose expiry passed as ``expired``. Returns how many."""
    n = 0
    for p in _dir(paths).glob("*.json"):
        if ID_RX.fullmatch(p.stem):
            try:
                t = _from_json(read_json(p, None), p.stem)
            except ApprovalError:
                continue
            if _expired(t):
                get(t.id, paths)
                n += 1
    return n


def prune(older_than_days: float = 30, paths: Optional[Paths] = None) -> int:
    """Delete decided/expired tasks older than N days (housekeeping; pending tasks are never deleted)."""
    cutoff = time.time() - older_than_days * 86400
    n = 0
    for p in _dir(paths).glob("*.json"):
        try:
            if ID_RX.fullmatch(p.stem) and p.stat().st_mtime < cutoff and _from_json(read_json(p, None), p.stem).status != "pending":
                p.unlink()
                n += 1
        except (OSError, ApprovalError):
            continue
    return n


def _maybe_prune(paths: Optional[Paths], every_s: float = 86400.0, older_than_days: float = 30) -> None:
    """Housekeeping at most once a day (marker file): decided/expired tasks older than 30 days go, so listing stays fast. Never raises."""
    try:
        marker = _dir(paths) / ".pruned"
        if marker.exists() and time.time() - marker.stat().st_mtime < every_s:
            return
        marker.write_text(iso(), encoding="ascii")
        prune(older_than_days, paths)
    except OSError:
        pass


def __getattr__(name: str) -> Any:  # ``approvals.list(...)`` without shadowing the builtin inside this module
    if name == "list":
        return list_tasks
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --- local approval page ---------------------------------------------------------------------------------
_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
_STYLE = ("body{font:15px/1.5 system-ui,Segoe UI,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#1b1b1b}"
          "h1{font-size:1.25rem}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:.4rem;text-align:left;vertical-align:top}"
          "button{font:inherit;padding:.5rem 1rem;margin-right:.5rem;border:1px solid #888;border-radius:6px;cursor:pointer}"
          "button.ok{background:#1a7f37;color:#fff;border-color:#1a7f37}button.no{background:#b42318;color:#fff;border-color:#b42318}"
          "pre{white-space:pre-wrap;background:#f6f6f6;padding:.6rem;border-radius:6px}.m{color:#555}input[type=text]{width:100%;padding:.4rem;box-sizing:border-box}")


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{_STYLE}</style><body>{body}</body></html>").encode("utf-8")


def _make_handler(state: "_UiState") -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "amx-approvals"
        timeout = 5  # per-connection socket timeout: an idle or half-open client cannot hold a handler thread
        sys_version = ""
        protocol_version = "HTTP/1.0"

        def log_message(self, *a: Any) -> None:  # silence (tokens are in URLs)
            pass

        # -- helpers
        def _send(self, code: int, body: bytes, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _deny(self, code: int = 403, why: str = "Forbidden") -> None:
            state.bad += 1
            self._send(code, _page(why, f"<h1>{html.escape(why)}</h1>"))
            if state.bad >= 50:
                state.stop.set()  # somebody is guessing: shut the page down

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in (f"127.0.0.1:{state.port}", f"localhost:{state.port}") and self.client_address[0] in ("127.0.0.1", "::1")

        def _token_ok(self, token: Optional[str]) -> bool:
            if not token:
                return False
            try:  # bytes: compare_digest raises TypeError on a non-ASCII str, which used to drop the connection without counting it
                return hmac.compare_digest(str(token).encode("utf-8"), state.token.encode("utf-8"))
            except (TypeError, ValueError):
                return False

        def _url(self, path: str, **q: str) -> str:
            return path + "?" + urllib.parse.urlencode({"t": state.token, **q})

        # -- routes
        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._deny(403, "Wrong host")
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self._token_ok((q.get("t") or [None])[0]):
                return self._deny(403, "Missing or wrong token")
            state.touch()  # only an authenticated request keeps the page alive: a probe must not
            if u.path == "/":
                return self._list(done=(q.get("done") or [""])[0])
            m = re.match(r"^/task/([A-Za-z0-9_-]{16,40})$", u.path)
            if m:
                return self._show(m.group(1))
            self._send(404, _page("Not found", "<h1>Not found</h1>"))

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            raw = self.rfile.read(min(max(length, 0), 16384))  # always consume (a bounded amount of) the body so refusals reach the client cleanly
            if not self._host_ok():
                return self._deny(403, "Wrong host")
            if self.path.split("?")[0] != "/decide":
                return self._send(404, _page("Not found", "<h1>Not found</h1>"))
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{state.port}", f"http://localhost:{state.port}"):
                return self._deny(403, "Cross-origin request refused")
            if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                return self._deny(403, "Cross-site request refused")
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
                return self._deny(415, "Unsupported content type")
            if not (0 < length <= 4096):
                return self._deny(413, "Body too large")
            form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
            one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
            if not self._token_ok(one("t")):
                return self._deny(403, "Missing or wrong token")
            state.touch()
            try:
                task = decide(one("id"), one("decision"), by=None, note=one("note"), paths=state.paths)
                state.decided += 1
            except (ApprovalError, ValueError) as exc:
                return self._send(409, _page("Not decided", f"<h1>Not decided</h1><p>{html.escape(str(exc))}</p>"
                                             f"<p><a href=\"{html.escape(self._url('/'))}\">Back</a></p>"))
            self._send(303, b"", {"Location": self._url("/", done=task.status)})

        def _list(self, done: str = "") -> None:
            tasks = list_tasks("pending", state.paths)
            rows = "".join(
                f"<tr><td><a href=\"{html.escape(self._url('/task/' + t.id))}\">{html.escape(t.title)}</a></td><td>{html.escape(t.automation)}</td>"
                f"<td class=m>{html.escape(t.expires_at)}</td></tr>" for t in tasks)
            note = f"<p>Recorded: <b>{html.escape(done)}</b></p>" if done in STATUSES else ""
            body = (f"<h1>Pending approvals</h1>{note}" + (f"<table><tr><th>Request</th><th>Automation</th><th>Expires (UTC)</th></tr>{rows}</table>"
                                                              if tasks else "<p>Nothing is waiting for you.</p>")
                    + "<p class=m>This page runs only on this computer and closes by itself when idle.</p>")
            self._send(200, _page("Pending approvals", body))

        def _show(self, task_id: str) -> None:
            try:
                t = get(task_id, state.paths)
            except ApprovalError:
                return self._send(404, _page("Not found", "<h1>No such approval</h1>"))
            facts = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in t.fields.items())
            form = ""
            if t.status == "pending":
                tok, tid = html.escape(state.token, quote=True), html.escape(t.id, quote=True)
                form = (f"<form method=post action=/decide><input type=hidden name=t value=\"{tok}\"><input type=hidden name=id value=\"{tid}\">"
                        "<p><label>Note (optional)<br><input type=text name=note maxlength=500></label></p>"
                        "<button class=ok name=decision value=approve>Approve</button><button class=no name=decision value=reject>Reject</button></form>")
            else:
                form = f"<p>Status: <b>{html.escape(t.status)}</b>" + (f" by {html.escape(t.decided_by)}" if t.decided_by else "") + "</p>"
            body = (f"<h1>{html.escape(t.title)}</h1><p class=m>{html.escape(t.automation)} - expires {html.escape(t.expires_at)}</p>"
                    + (f"<pre>{html.escape(t.description)}</pre>" if t.description else "") + (f"<table>{facts}</table>" if facts else "")
                    + form + f"<p><a href=\"{html.escape(self._url('/'))}\">All approvals</a></p>")
            self._send(200, _page(t.title, body))

    return Handler


class _LoopbackServer(ThreadingHTTPServer):
    """One idle/half-open connection must not block the page (threaded + per-connection timeout), and on Windows nobody else may bind
    the same port: SO_REUSEADDR there lets another local process or user share it and receive the one-time token."""
    allow_reuse_address = False
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: D401 - a dropped/timed-out client is routine: no traceback on the user's console
        return None

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()


class _UiState:
    def __init__(self, paths: Optional[Paths], port: int, token: str):
        self.paths, self.port, self.token = paths, port, token
        self.stop = threading.Event()
        self.bad = 0
        self.decided = 0
        self.last = time.monotonic()

    def touch(self) -> None:
        self.last = time.monotonic()


def serve(paths: Optional[Paths] = None, port: int = 0, open_browser: bool = True, idle_s: float = 600.0,
          ready: Optional[Callable[[str, str, int], None]] = None, stop: Optional[threading.Event] = None) -> int:
    """Run the local approval page until it has been idle for ``idle_s`` seconds (or ``stop`` is set). Returns how many decisions were made.

    Binds 127.0.0.1 only. ``ready(url, token, port)`` is called once listening (tests / embedding)."""
    token = _secrets.token_urlsafe(24)
    state = _UiState(paths, 0, token)
    server = _LoopbackServer(("127.0.0.1", port), _make_handler(state))
    state.port = server.server_address[1]
    if stop is not None:
        state.stop = stop
    server.timeout = 0.5
    url = f"http://127.0.0.1:{state.port}/?t={token}"
    if ready:
        ready(url, token, state.port)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        while not state.stop.is_set() and time.monotonic() - state.last < idle_s:
            server.handle_request()
    finally:
        server.server_close()
    return state.decided


# --- command line ---------------------------------------------------------------------------------------
def _show_line(t: Task) -> str:
    return f"{t.id}  {t.status:<8}  {t.automation}  {t.title}"


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m wrapper_core approvals list|show|approve|reject|ui|prune``"""
    import argparse
    ap = argparse.ArgumentParser(prog="wrapper_core approvals")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--status", choices=STATUSES, default=None)
    sh = sub.add_parser("show")
    sh.add_argument("id")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("--note", default="")
    ui = sub.add_parser("ui", help="open the local approval page")
    ui.add_argument("--port", type=int, default=0)
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument("--idle-s", type=float, default=600.0)
    pr = sub.add_parser("prune")
    pr.add_argument("--older-than-days", type=float, default=30)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            tasks = list_tasks(a.status)
            for t in tasks:
                print(_show_line(t))
            print(f"{len(tasks)} approval(s)")
        elif a.cmd == "show":
            t = get(a.id)
            print(json.dumps(t.__dict__, indent=2, ensure_ascii=True))
        elif a.cmd in ("approve", "reject"):
            t = decide(a.id, a.cmd, note=a.note)
            print(f"{t.status}: {t.id}")
        elif a.cmd == "ui":
            def announce(url: str, token: str, port: int) -> None:
                print(f"Approval page: {url}\nIt only listens on this computer and closes after {a.idle_s:.0f}s without use. Press Ctrl+C to stop.")
            n = serve(port=a.port, open_browser=not a.no_browser, idle_s=a.idle_s, ready=announce)
            print(f"{n} decision(s) made")
        elif a.cmd == "prune":
            print(f"deleted {prune(a.older_than_days)} old approval(s)")
    except (ApprovalError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0
