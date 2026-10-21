"""Builders for realistic run/heartbeat/lifecycle artefacts and manifests.

Used by the test-suite factories and by ``python -m automation_logging sample``. Shapes follow docs/SPEC.md §6.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .common import fingerprint, iso


def _rid(automation: str, started: datetime, host: str) -> str:
    return started.strftime("%Y%m%dT%H%M%SZ") + "-" + hashlib.sha1(f"{automation}{host}{started.isoformat()}".encode()).hexdigest()[:8]


def run_record(automation: str, started_at: datetime, *, status: str = "success", ring: str = "pilot",
               host: str = "VDI-0001", version: str = "1.0.0", wrapper: str = "0.1.0", framework: str = "0.1.0",
               duration_ms: int = 120000, items: list | None = None, error: dict | None = None,
               params: dict | None = None, slot: str = "daily", synopsis: str | None = None,
               hung_detected: bool = False, trigger_id: str | None = None, run_id: str | None = None,
               self_heal_errors: list | None = None, retries: list | None = None, llm_calls: int = 1,
               extra_events: list | None = None, result: dict | None = None) -> dict:
    """items: list of (item_id, outcome, duration_ms, reason_or_error_category)."""
    started_at = started_at.astimezone(timezone.utc)
    ended = started_at + timedelta(milliseconds=duration_ms)
    events: list[dict] = [{"ts": iso(started_at), "event": "phase", "data": {"name": "execute", "status": "start"}}]
    counts = {"total": 0, "success": 0, "skipped": 0, "failed": 0}
    for (item_id, outcome, dur, why) in items or []:
        data = {"item_id": item_id, "outcome": outcome, "duration_ms": dur}
        if outcome == "skipped" and why:
            data["reason"] = why
        if outcome == "failed" and why:
            data["error_category"] = why
        events.append({"ts": iso(started_at + timedelta(milliseconds=min(duration_ms - 1, 50))), "event": "item_processed", "data": data})
        counts["total"] += 1
        counts[outcome] += 1
    for r in retries or []:
        events.append({"ts": iso(started_at), "event": "retry_event", "data": dict(r)})
    for _ in range(llm_calls):
        events.append({"ts": iso(started_at), "event": "llm_call",
                       "data": {"caller": "wrapper-core/synopsis", "prompt_chars": 900, "duration_ms": 420, "status": "success"}})
    events.extend(extra_events or [])
    rec = {
        "schema_version": 1, "kind": "run", "run_id": run_id or _rid(automation, started_at, host), "automation": automation,
        "slot": slot, "automation_version": version, "wrapper_version": wrapper, "framework_version": framework,
        "ring": ring, "host": host, "user_hash": fingerprint(host, n=12), "started_at": iso(started_at),
        "ended_at": iso(ended), "duration_ms": duration_ms, "expected_finish_at": None, "status": status,
        "exit_code": 0 if status in ("success", "partial", "halted_paused", "skipped_maintenance") else 1,
        "params": params or {}, "events": events, "items": counts, "error": error,
        "synopsis": synopsis or f"{automation} {status.replace('_', ' ')} in {duration_ms // 1000}s.",
        "synopsis_source": "deterministic",
        "self_heal": {"installed": [], "errors": list(self_heal_errors or []), "offline": False},
        "hung_detected": hung_detected, "result": result or {},
    }
    if trigger_id:
        rec["trigger_id"] = trigger_id
    return rec


def heartbeat_record(host: str, ts: datetime, *, phase: str = "idle", automations: dict | None = None, ring: str = "pilot",
                     wrapper: str = "0.1.0", framework: str = "0.1.0", automation: str | None = None) -> dict:
    return {"schema_version": 1, "kind": "heartbeat", "host": host, "user_hash": fingerprint(host, n=12), "ts": iso(ts),
            "phase": phase, "automation": automation, "run_id": None, "expected_finish_at": None,
            "versions": {"wrapper-core": wrapper, "execution-framework": framework, "automations": dict(automations or {})},
            "ring": ring}


def lifecycle_record(event: str, automation: str | None, host: str, ts: datetime, detail: dict | None = None, ring: str = "pilot") -> dict:
    return {"schema_version": 1, "kind": "lifecycle", "event": event, "automation": automation, "host": host,
            "user_hash": fingerprint(host, n=12), "ts": iso(ts), "detail": detail or {}, "ring": ring}


def artefact_name_and_id(rec: dict) -> tuple[str, str]:
    kind = rec["kind"]
    if kind == "run":
        return rec["automation"], rec["run_id"]
    if kind == "heartbeat":
        return rec["host"], f"{rec['host']}-{rec['ts'].replace(':', '').replace('-', '').replace('.', '')}-{rec['phase']}"
    return rec.get("automation") or rec["host"], f"{rec['ts'].replace(':', '').replace('-', '').replace('.', '')}-{rec['event']}"


def _ts_of(rec: dict) -> datetime:
    from .common import parse_iso
    return parse_iso(rec["started_at"] if rec["kind"] == "run" else rec["ts"])


def write_archive(archive: Path, artefacts: list[dict]) -> None:
    """Write artefacts in the archived layout: <kind>/<name>/YYYY/MM/DD/<id>.json"""
    for rec in artefacts:
        name, art_id = artefact_name_and_id(rec)
        d = _ts_of(rec)
        target = Path(archive) / rec["kind"] / name / d.strftime("%Y") / d.strftime("%m") / d.strftime("%d") / f"{art_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(rec, indent=1), encoding="utf-8")


def write_dir_transport(root: Path, artefacts: list[dict], ring: str = "pilot") -> None:
    """Write artefacts exactly like wrapper_core.transport.DirTransport: <root>/ingest/<ring>/<kind>/<name>/<id>.json"""
    for rec in artefacts:
        name, art_id = artefact_name_and_id(rec)
        target = Path(root) / "ingest" / ring / rec["kind"] / name / f"{art_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(rec, indent=2), encoding="utf-8")


def automation_entry(description: str = "test automation", owner: str = "owner@example.com", *, version: str = "1.0.0",
                     rings: dict | None = None, slots: list | None = None, tz: str = "UTC", baseline: int = 120000,
                     warn: float = 2.0, maintenance: list | None = None, classification: str = "internal",
                     llm_allowed: str = "internal", status: str = "active", triggers: list | None = None,
                     catalog: dict | None = None, versions: dict | None = None) -> dict:
    sha = hashlib.sha256(version.encode()).hexdigest()
    entry = {
        "description": description, "owner": owner, "owner_team": "Automation Platform", "systems": ["none"],
        "data_classification": classification, "llm_allowed_classification": llm_allowed, "status": status,
        "rings": rings or {"dev": version, "pilot": version, "broad": version},
        "versions": versions or {version: {"url": f"automations/x-{version}.zip", "sha256": sha}},
        "schedule": {"timezone": tz, "expected_duration_ms": {"baseline": baseline, "warn_threshold": warn},
                     "maintenance_windows": maintenance or [],
                     "slots": slots if slots is not None else [{"id": "daily", "type": "daily", "time": "09:00"}]},
    }
    if triggers:
        entry["triggers"] = triggers
    if catalog:
        entry["catalog"] = catalog
    return entry


def manifest_dict(automations: dict, *, wrapper: str = "0.1.0", framework: str = "0.1.0", fleet_paused: bool = False) -> dict:
    sha = hashlib.sha256(b"x").hexdigest()
    comp = lambda v: {"rings": {"dev": v, "pilot": v, "broad": v}, "versions": {v: {"url": f"c-{v}.zip", "sha256": sha}}}  # noqa: E731
    return {"schema_version": 1, "fleet": {"paused": fleet_paused, "paused_reason": "test" if fleet_paused else ""},
            "rings": ["dev", "pilot", "broad"],
            "compatibility": [{"wrapper_core": ">=0.1.0,<1.0.0", "execution_framework": ">=0.1.0,<1.0.0"}],
            "components": {"wrapper-core": comp(wrapper), "execution-framework": comp(framework)},
            "assets": {"default": {}}, "automations": automations}
