"""Strict validation of log artefacts.

Threat model: the credential installed clients use to write the logs repository ships inside a PUBLIC installer, so every
artefact (run / heartbeat / lifecycle / evidence) is attacker-authored. Nothing here trusts a field: types, enums, sizes
and names are checked before an artefact is archived and again when the archive is read, so one forged record can never
crash ingest, analytics or the dashboards.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from .common import iso, parse_iso

RINGS = ("dev", "pilot", "broad")
KINDS = ("run", "heartbeat", "lifecycle", "evidence")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REMEDIATION_OUTCOMES = ("fixed", "not_fixed", "skipped", "escalated", "recommended")
MAX_PLAYBOOKS = 20
RUN_STATUSES = {"success", "partial", "failed", "halted_paused", "skipped_maintenance", "self_heal_failed", "hung"}
MAX_BYTES = {"run": 1_000_000, "heartbeat": 100_000, "lifecycle": 100_000, "evidence": 3_000_000}
MAX_EVENTS = 5000


def ref_digest(text: str) -> str:
    """Safe file stem for anything derived from an untrusted ref / path (never use the raw text as a file name)."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e15


MAX_FUTURE = timedelta(minutes=5)       # a client clock may be a little fast; anything beyond this is forged or broken
MIN_YEAR = 2020
TS_FIELDS = ("started_at", "ended_at", "ts", "expected_finish_at")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts_ok(v) -> bool:
    """A timestamp the rest of the platform can safely do arithmetic on: parseable, representable in UTC, not absurdly old
    and not in the future (a forged 2099 heartbeat would otherwise mask SILENT / FAILED signals)."""
    if not isinstance(v, str) or len(v) > 40:
        return False
    try:
        utc = parse_iso(v).astimezone(timezone.utc)
        return utc.year >= MIN_YEAR and utc <= _now() + MAX_FUTURE
    except (ValueError, TypeError, OverflowError, OSError):
        return False


def normalize_timestamps(rec: dict) -> dict:
    """Rewrite the known timestamp fields (record and events) as canonical UTC strings so that string ordering == time ordering.
    Call only on records that passed validate_record."""
    for k in TS_FIELDS:
        if isinstance(rec.get(k), str):
            rec[k] = iso(parse_iso(rec[k]))
    for e in rec.get("events") or []:
        if isinstance(e, dict) and isinstance(e.get("ts"), str):
            e["ts"] = iso(parse_iso(e["ts"]))
    return rec


def check_path_parts(ring: str, kind: str, name: str, art_id: str) -> str | None:
    if ring not in RINGS:
        return f"unknown ring {ring[:30]!r}"
    if kind not in KINDS:
        return f"unknown kind {kind[:30]!r}"
    if not NAME_RE.match(name) or name.startswith(".") or ".." in name:
        return "unsafe artefact name"
    if not ID_RE.match(art_id) or ".." in art_id:
        return "unsafe artefact id"
    return None


def _str(v, limit=500) -> bool:
    return isinstance(v, str) and len(v) <= limit


def _check_remediation_attempt(d: dict) -> str | None:
    """Event ``remediation_attempt``: {playbook, category, action, outcome, detail<=300, duration_ms, attempt}. Strict: it feeds fix-rate maths."""
    if d.get("outcome") not in REMEDIATION_OUTCOMES:
        return "run: remediation_attempt outcome is not a known value"
    for k, lim in (("playbook", 100), ("category", 60), ("action", 200), ("detail", 300)):
        if d.get(k) is not None and not _str(d[k], lim):
            return f"run: remediation_attempt {k} is not a bounded string"
    for k in ("duration_ms", "attempt"):
        if d.get(k) is not None and (not _is_num(d[k]) or d[k] < 0):
            return f"run: remediation_attempt {k} is not a non-negative number"
    return None


def _check_remediation_summary(v) -> str | None:
    """Run key ``remediation``: {category, fixed: bool, playbooks: [names]}."""
    if not isinstance(v, dict):
        return "run: remediation is not an object"
    if v.get("category") is not None and not _str(v["category"], 60):
        return "run: remediation.category is not a string"
    if v.get("fixed") is not None and not isinstance(v["fixed"], bool):
        return "run: remediation.fixed is not a boolean"
    pbs = v.get("playbooks")
    if pbs is not None and (not isinstance(pbs, list) or len(pbs) > MAX_PLAYBOOKS or not all(_str(x, 100) for x in pbs)):
        return "run: remediation.playbooks is not a bounded list of names"
    return None


def _validate_run(rec: dict) -> str | None:
    if not isinstance(rec.get("automation"), str) or not NAME_RE.match(rec["automation"]):
        return "run: bad automation"
    if not isinstance(rec.get("run_id"), str) or not ID_RE.match(rec["run_id"]):
        return "run: bad run_id"
    if not _ts_ok(rec.get("started_at")):
        return "run: bad started_at"
    if rec.get("status") not in RUN_STATUSES:
        return "run: status is not one of the known values"
    if rec.get("ended_at") is not None and not _ts_ok(rec.get("ended_at")):
        return "run: bad ended_at"
    for key in ("duration_ms",):
        if rec.get(key) is not None and not _is_num(rec[key]):
            return f"run: {key} is not a number"
    if rec.get("exit_code") is not None and (not isinstance(rec["exit_code"], int) or isinstance(rec["exit_code"], bool)):
        return "run: exit_code is not an integer"
    if rec.get("host") is not None and not (isinstance(rec["host"], str) and NAME_RE.match(rec["host"])):
        return "run: host is not a plain host name"
    for key in ("ring", "automation_version", "wrapper_version", "framework_version", "slot", "synopsis",
                "synopsis_source", "trigger_id", "halt_reason", "expected_finish_at"):
        if rec.get(key) is not None and not _str(rec[key], 2000):
            return f"run: {key} is not a string"
    if rec.get("ring") is not None and rec["ring"] not in RINGS:
        return "run: unknown ring"
    events = rec.get("events", [])
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        return "run: events is not a bounded list"
    for e in events:
        if not isinstance(e, dict) or not _str(e.get("event"), 100):
            return "run: malformed event"
        if e.get("data") is not None and not isinstance(e["data"], dict):
            return "run: event data is not an object"
        if e.get("ts") is not None and not _ts_ok(e["ts"]):
            return "run: event ts is not a timestamp"
        d = e.get("data") or {}
        if e["event"] == "remediation_attempt":
            bad = _check_remediation_attempt(d)
            if bad:
                return bad
        if e["event"] == "item_processed":
            if d.get("outcome") is not None and d["outcome"] not in ("success", "skipped", "failed"):
                return "run: item outcome is not a known value"
            if d.get("duration_ms") is not None and not _is_num(d["duration_ms"]):
                return "run: item duration_ms is not a number"
    if rec.get("remediation") is not None:
        bad = _check_remediation_summary(rec["remediation"])
        if bad:
            return bad
    items = rec.get("items")
    if items is not None:
        if not isinstance(items, dict) or any(not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > 10**9 for v in items.values()):
            return "run: items must be an object of non-negative integers"
    for key in ("params", "result", "self_heal"):
        if rec.get(key) is not None and not isinstance(rec[key], dict):
            return f"run: {key} is not an object"
    err = rec.get("error")
    if err is not None:
        if not isinstance(err, dict):
            return "run: error is not an object"
        for k in ("category", "type", "message"):
            if err.get(k) is not None and not _str(err[k], 5000):
                return f"run: error.{k} is not a string"
        if err.get("attempts") is not None and not _is_num(err["attempts"]):
            return "run: error.attempts is not a number"
        tb = err.get("traceback_tail")
        if tb is not None and (not isinstance(tb, list) or not all(isinstance(x, str) for x in tb)):
            return "run: error.traceback_tail is not a list of strings"
    if rec.get("hung_detected") is not None and not isinstance(rec["hung_detected"], bool):
        return "run: hung_detected is not a boolean"
    return None


def _validate_heartbeat(rec: dict) -> str | None:
    if not _ts_ok(rec.get("ts")):
        return "heartbeat: bad ts"
    if not isinstance(rec.get("host"), str) or not NAME_RE.match(rec["host"]):
        return "heartbeat: bad host"
    for key in ("phase", "ring", "automation", "run_id", "user_hash", "expected_finish_at"):
        if rec.get(key) is not None and not _str(rec[key], 200):
            return f"heartbeat: {key} is not a string"
    if rec.get("ring") is not None and rec["ring"] not in RINGS:
        return "heartbeat: unknown ring"
    v = rec.get("versions")
    if v is not None:
        if not isinstance(v, dict):
            return "heartbeat: versions is not an object"
        for k, x in v.items():
            if k == "automations":
                if not isinstance(x, dict) or not all(_str(a, 100) and (b is None or _str(b, 100)) for a, b in x.items()):
                    return "heartbeat: versions.automations malformed"
            elif x is not None and not _str(x, 100):
                return f"heartbeat: versions.{str(k)[:30]} is not a string"
    return None


def _validate_lifecycle(rec: dict) -> str | None:
    if not _ts_ok(rec.get("ts")):
        return "lifecycle: bad ts"
    if not _str(rec.get("event"), 100) or not rec.get("event"):
        return "lifecycle: bad event"
    for key in ("automation", "host", "user_hash"):
        if rec.get(key) is not None and not _str(rec[key], 200):
            return f"lifecycle: {key} is not a string"
    if rec.get("detail") is not None and not isinstance(rec["detail"], dict):
        return "lifecycle: detail is not an object"
    return None


def validate_record(rec, kind: str) -> str | None:
    """Return a rejection reason, or None when the record is well-formed. Evidence payloads are checked by ingest."""
    if not isinstance(rec, dict):
        return "not a JSON object"
    if rec.get("schema_version") != 1:
        return f"unsupported schema_version {str(rec.get('schema_version'))[:20]!r}"
    if rec.get("kind") != kind:
        return "kind in the content does not match the path"
    if kind == "run":
        return _validate_run(rec)
    if kind == "heartbeat":
        return _validate_heartbeat(rec)
    if kind == "lifecycle":
        return _validate_lifecycle(rec)
    if kind == "evidence":
        if not _ts_ok(rec.get("ts")):
            return "evidence: bad ts"
        if not isinstance(rec.get("automation"), str) or not NAME_RE.match(rec["automation"]):
            return "evidence: bad automation"
        if not isinstance(rec.get("run_id"), str) or not ID_RE.match(rec["run_id"]):
            return "evidence: bad run_id"
        return None
    return f"unknown kind {kind[:30]!r}"


def check_name_matches_record(kind: str, name: str, rec: dict) -> str | None:
    if kind in ("run", "lifecycle", "evidence"):
        auto = rec.get("automation")
        if kind == "lifecycle" and auto is None:
            return None  # host-level lifecycle events carry no automation; name is then the host
        if name != auto:
            return "artefact name does not match the automation inside it"
    return None
