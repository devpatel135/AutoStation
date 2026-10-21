"""Trigger computation (SPEC §12): event-driven and on-demand starts without any new server.

Three sources produce entries in ``triggers.json`` (published next to the manifest; endpoints with a ``poll`` slot read it
and consume unseen ids addressed to them):

1. manifest ``triggers:`` rules matched against recently finished runs (``on_automation`` + ``on_status``)
2. ``trigger`` events emitted by payloads via ``wrapper.emit_trigger()``
3. operator requests: ``requests/*.json`` ``{automation, slot, params, requested_by}`` (written by the admin-panel pipeline)

Ids are stable hashes, so recomputing is idempotent. Chains are depth-limited to stop A->B->A loops. Entries older than
7 days are pruned.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .common import fingerprint, iso, parse_iso, read_json, write_json
from . import analytics

MAX_CHAIN_DEPTH = 3
RETENTION = timedelta(days=7)
RUN_WINDOW = timedelta(hours=24)


def _entry(automation, slot, params, now, source, depth, **extra) -> dict:
    return {"id": None, "automation": automation, "slot": slot, "params": params or {}, "created_at": iso(now),
            "source": source, "depth": depth, **extra}


def compute(existing: dict | list | None, data: analytics.Data, manifest: dict, now: datetime,
            requests_dir: str | Path | None = None) -> tuple[dict, dict]:
    """Return (triggers_doc, stats). ``existing`` is the previously published document (or list)."""
    items = (existing.get("triggers") if isinstance(existing, dict) else existing) or []
    by_id = {t["id"]: t for t in items if isinstance(t, dict) and t.get("id")}
    autos = manifest.get("automations") or {}
    stats = {"added": 0, "pruned": 0, "ignored_requests": [], "depth_limited": 0}

    def add(t: dict, key_parts: tuple) -> None:
        t["id"] = fingerprint(*key_parts)
        if t["id"] in by_id:
            return
        by_id[t["id"]] = t
        stats["added"] += 1

    def depth_of(run: dict) -> int:
        tid = run.get("trigger_id")
        return int((by_id.get(tid) or {}).get("depth", 0)) if tid else 0

    window_start = now - RUN_WINDOW
    for run in data.runs:
        if analytics._end(run) < window_start or run["status"] in ("halted_paused", "skipped_maintenance"):
            continue
        depth = depth_of(run) + 1
        # 1. manifest rules
        for target, entry in autos.items():
            for i, rule in enumerate((entry or {}).get("triggers") or []):
                if rule.get("on_automation") != run["automation"] or str(rule.get("on_status", "success")) != run["status"]:
                    continue
                if (entry or {}).get("status") == "paused":
                    continue
                if depth > MAX_CHAIN_DEPTH:
                    stats["depth_limited"] += 1
                    continue
                add(_entry(target, rule.get("slot", "poll"), dict(rule.get("params") or {}), now,
                           f"rule:{run['automation']}:{run['run_id']}", depth, source_run_id=run["run_id"]),
                    ("rule", target, run["run_id"], i))
        # 2. payload-emitted events
        for ev in run.get("events", []):
            if ev.get("event") != "trigger":
                continue
            d = ev.get("data") or {}
            target = d.get("target")
            if target not in autos or run["status"] not in ("success", "partial"):
                continue
            if depth > MAX_CHAIN_DEPTH:
                stats["depth_limited"] += 1
                continue
            add(_entry(target, d.get("slot", "poll"), dict(d.get("params") or {}), now, f"event:{run['automation']}:{run['run_id']}",
                       depth, source_run_id=run["run_id"], reason=str(d.get("reason", ""))[:200]),
                ("event", target, run["run_id"], ev.get("ts"), json.dumps(d.get("params") or {}, sort_keys=True)))

    # 3. operator requests
    if requests_dir and Path(requests_dir).exists():
        for f in sorted(Path(requests_dir).glob("*.json")):
            if datetime.fromtimestamp(f.stat().st_mtime, tz=now.tzinfo) < now - RETENTION:
                continue  # stale request file: never resurrect a pruned trigger
            try:
                req = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(req, dict) or req.get("automation") not in autos:
                    raise ValueError("unknown automation")
                if not isinstance(req.get("params", {}), dict):
                    raise ValueError("params must be an object")
            except (ValueError, OSError) as exc:
                stats["ignored_requests"].append({"file": f.name, "reason": str(exc)})
                continue
            add(_entry(req["automation"], req.get("slot", "poll"), req.get("params") or {}, now,
                       f"operator:{req.get('requested_by', 'unknown')}", 1, requested_by=req.get("requested_by")),
                ("operator", f.name, f.read_bytes().hex()))

    cutoff = now - RETENTION
    kept = {}
    for tid, t in by_id.items():
        try:
            if parse_iso(t["created_at"]) < cutoff:
                stats["pruned"] += 1
                continue
        except (KeyError, ValueError):
            stats["pruned"] += 1
            continue
        kept[tid] = t
    doc = {"schema_version": 1, "generated_at": iso(now),
           "triggers": sorted(kept.values(), key=lambda t: (t["created_at"], t["id"]))}
    return doc, stats


def run(data: analytics.Data, manifest: dict, now: datetime, out_path: str | Path, requests_dir=None) -> dict:
    existing = read_json(out_path, None)
    doc, stats = compute(existing, data, manifest, now, requests_dir)
    write_json(out_path, doc)
    stats["total"] = len(doc["triggers"])
    return stats
