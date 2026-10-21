"""Analytics: KPIs, item-level metrics, retry/LLM stats, trends and compliance signals (SPEC §11).

``compute(data, manifest, now, config, state)`` is a pure function of its inputs (``now`` is injectable) and returns
``(report, new_state)``. ``report`` is JSON-serialisable and contains no raw event streams.
"""
from __future__ import annotations

import copy
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .common import (SEVERITY_ORDER, error_fingerprint, fingerprint, iso, parse_iso, percentile, read_json, utcnow,
                     wc_manifest)
from .validate import MAX_BYTES, check_name_matches_record, validate_record

DEFAULTS = {
    "window_days": 30,        # how much archive history is loaded/aggregated
    "lookback_h": 72,         # run-based signals look at runs this recent
    "silent_after_h": 36,     # SILENT: host heartbeat older than this
    "drift_grace_h": 6,       # VERSION_DRIFT: a run this long after a target change must be on the new version
    "trend_days": 14,
    "min_grace_min": 15,      # MISSED grace = max(min_grace_min, 0.25 * baseline)
}
_UNCOUNTED = ("halted_paused", "skipped_maintenance")
_FAILED = ("failed", "self_heal_failed")


class Data:
    def __init__(self, runs=None, heartbeats=None, lifecycle=None, evidence=None, archive=None):
        self.runs: list[dict] = sorted(runs or [], key=lambda r: r["started_at"])
        self.heartbeats: list[dict] = sorted(heartbeats or [], key=lambda r: r["ts"])
        self.lifecycle: list[dict] = sorted(lifecycle or [], key=lambda r: r["ts"])
        # evidence records are loaded WITHOUT their (large) payloads: {run_id, automation, ts, files:[{name,content_type,size,sha256}], _path}
        self.evidence: list[dict] = sorted(evidence or [], key=lambda r: r["ts"])
        self.archive = archive  # archive root, so the dashboard can read an evidence payload on demand
        self.runs_by_id = {r["run_id"]: r for r in self.runs}
        self.skipped_malformed = 0  # archive files that failed re-validation (set by load_archive)


def load_archive(archive, now: datetime | None = None, window_days: int = DEFAULTS["window_days"]) -> Data:
    """Read the archive. Date folders older than the window are skipped without being opened.

    The archive is re-validated on read (an archive written by an older ingest, or edited by hand, is not trusted):
    a malformed record is skipped and counted in ``Data.skipped_malformed``, never allowed to crash the build.
    """
    archive = Path(archive)
    now = now or utcnow()
    cutoff = (now - timedelta(days=window_days)).date()
    out: dict[str, list] = {"run": [], "heartbeat": [], "lifecycle": [], "evidence": []}
    skipped = 0
    for kind in out:
        for f in (archive / kind).glob("*/*/*/*/*.json") if (archive / kind).exists() else []:
            rel = f.relative_to(archive / kind).parts
            try:
                d = datetime(int(rel[1]), int(rel[2]), int(rel[3])).date()
            except ValueError:
                continue
            if d < cutoff:
                continue
            try:
                if f.stat().st_size > MAX_BYTES[kind]:
                    raise ValueError("oversized")
                rec = read_json(f)
                if not isinstance(rec, dict) or rec.get("kind") != kind:
                    raise ValueError("not a record")
                if validate_record(rec, kind) or check_name_matches_record(kind, rel[0], rec):
                    raise ValueError("invalid")
                rec["_path"] = f.relative_to(archive).as_posix()
                if kind == "evidence":  # keep the index light: payloads are read on demand
                    files = rec.get("files") if isinstance(rec.get("files"), list) else []
                    rec["files"] = [{k: v for k, v in fl.items() if k != "data_b64"} for fl in files if isinstance(fl, dict)]
            except Exception:  # noqa: BLE001 - any failure to read/validate one file only skips that file
                skipped += 1
                continue
            out[kind].append(rec)
    data = Data(out["run"], out["heartbeat"], out["lifecycle"], out["evidence"], archive)
    data.skipped_malformed = skipped
    return data


# ---------------------------------------------------------------------------------------------- helpers
def _ts(rec: dict) -> datetime:
    return parse_iso(rec["started_at"] if rec.get("kind") == "run" else rec["ts"])


def _end(rec: dict) -> datetime:
    return parse_iso(rec.get("ended_at") or rec["started_at"])


def _events(run: dict, name: str):
    return [e.get("data") or {} for e in run.get("events", []) if e.get("event") == name]


def _rate(n, d):
    return round(n / d, 4) if d else None


def run_summary(r: dict) -> dict:
    err = r.get("error") or {}
    return {"run_id": r["run_id"], "automation": r["automation"], "host": r.get("host"), "ring": r.get("ring"),
            "status": r["status"], "started_at": r["started_at"], "ended_at": r.get("ended_at"),
            "duration_ms": r.get("duration_ms"), "version": r.get("automation_version"), "items": r.get("items"),
            "synopsis": r.get("synopsis"), "synopsis_source": r.get("synopsis_source"),
            "error_category": err.get("category"), "error_message": (err.get("message") or "")[:300] or None,
            "hung_detected": bool(r.get("hung_detected")), "path": r.get("_path")}


def item_metrics(runs: list[dict]) -> dict:
    durs, outcomes, reasons, fail_cats = [], Counter(), Counter(), Counter()
    run_ms, runs_with_items = 0, 0
    for r in runs:
        its = _events(r, "item_processed")
        if not its:
            continue
        runs_with_items += 1
        run_ms += r.get("duration_ms") or 0
        for d in its:
            outcomes[d.get("outcome")] += 1
            if d.get("duration_ms") is not None:
                durs.append(d["duration_ms"])
            if d.get("outcome") == "skipped":
                reasons[d.get("reason") or "unspecified"] += 1
            if d.get("outcome") == "failed":
                fail_cats[d.get("error_category") or "unspecified"] += 1
    total = sum(outcomes.values())
    failed_runs = sum(1 for r in runs if r["status"] in _FAILED + ("hung",))
    return {
        "total": total, "success": outcomes["success"], "skipped": outcomes["skipped"], "failed": outcomes["failed"],
        "throughput_per_min": round(total / (run_ms / 60000), 2) if run_ms else None,
        "duration_ms": {"p50": percentile(durs, 50), "p95": percentile(durs, 95), "p99": percentile(durs, 99)},
        "skip_rate": _rate(outcomes["skipped"], total), "skip_reasons": dict(reasons.most_common()),
        "item_failure_rate": _rate(outcomes["failed"], total), "failure_categories": dict(fail_cats.most_common()),
        "run_failure_rate": _rate(failed_runs, len(runs)),
        # the case run-level metrics hide: runs that "succeeded" while items failed
        "runs_with_item_failures": sum(1 for r in runs if r["status"] in ("success", "partial") and (r.get("items") or {}).get("failed")),
        "runs_with_items": runs_with_items,
    }


def retry_stats(runs: list[dict]) -> dict:
    by_cat: dict[str, Counter] = defaultdict(Counter)
    by_source, total = Counter(), 0
    for r in runs:
        for d in _events(r, "retry_event"):
            total += 1
            cat = d.get("category") or "UNKNOWN"
            by_cat[cat]["events"] += 1
            by_cat[cat][d.get("outcome") or "unknown"] += 1
            by_source[d.get("source") or "unknown"] += 1
    n_runs = len(runs)
    runs_with = sum(1 for r in runs if _events(r, "retry_event"))
    cats = {}
    for cat, c in by_cat.items():
        resolved = c["recovered"] + c["skipped"]
        cats[cat] = {"events": c["events"], "recovered": c["recovered"], "escalated": c["escalated"],
                     "skipped": c["skipped"], "recovery_rate": _rate(resolved, c["events"])}
    return {"total_events": total, "runs_with_retries": runs_with, "retry_run_rate": _rate(runs_with, n_runs),
            "by_category": dict(sorted(cats.items(), key=lambda kv: -kv[1]["events"])), "by_source": dict(by_source)}


def llm_stats(runs: list[dict]) -> dict:
    status, callers, durs = Counter(), Counter(), []
    decisions = 0
    for r in runs:
        for d in _events(r, "llm_call"):
            status[d.get("status") or "unknown"] += 1
            callers[d.get("caller") or "unknown"] += 1
            if d.get("duration_ms") is not None:
                durs.append(d["duration_ms"])
        decisions += len(_events(r, "llm_decision"))
    total = sum(status.values())
    return {"calls": total, "by_status": dict(status), "by_caller": dict(callers.most_common()),
            "error_rate": _rate(total - status["success"], total), "decisions": decisions,
            "schema_failures": sum(len(_events(r, "llm_schema_failure")) for r in runs),
            "duration_ms": {"avg": round(sum(durs) / len(durs)) if durs else None, "p95": percentile(durs, 95)},
            "synopsis_from_llm": sum(1 for r in runs if r.get("synopsis_source") == "llm"),
            "synopsis_deterministic": sum(1 for r in runs if r.get("synopsis_source") == "deterministic")}


def status_counts(runs: list[dict]) -> dict:
    c = Counter(r["status"] for r in runs)
    counted = [r for r in runs if r["status"] not in _UNCOUNTED]
    ok = sum(1 for r in counted if r["status"] in ("success", "partial"))
    return {"total": len(runs), "success": c["success"], "partial": c["partial"], "failed": c["failed"] + c["self_heal_failed"],
            "hung": c["hung"], "halted_paused": c["halted_paused"], "skipped_maintenance": c["skipped_maintenance"],
            "success_rate": _rate(ok, len(counted))}


# ---------------------------------------------------------------------------------------------- schedule
def _slot_times(slot: dict, tz, start: datetime, end: datetime) -> list[datetime]:
    """Expected UTC fire times of one slot within [start, end]."""
    out = []
    first = (start.astimezone(tz) - timedelta(days=1)).date()
    last = (end.astimezone(tz) + timedelta(days=1)).date()
    day = first
    weekdays = {str(d).capitalize() for d in slot.get("days") or []}
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    while day <= last:
        stype = slot.get("type")
        if stype in ("daily", "weekly"):
            if stype == "weekly" and names[day.weekday()] not in weekdays:
                day += timedelta(days=1)
                continue
            try:
                h, m = (int(x) for x in str(slot["time"]).split(":"))
            except (KeyError, ValueError):
                break
            out.append(datetime(day.year, day.month, day.day, h, m, tzinfo=tz).astimezone(timezone.utc))
        elif stype == "hourly":
            n = max(1, int(slot.get("every", 1)))
            for hr in range(0, 24, n):
                out.append(datetime(day.year, day.month, day.day, hr, 0, tzinfo=tz).astimezone(timezone.utc))
        day += timedelta(days=1)
    return sorted(t for t in out if start <= t <= end)


def expected_runs(entry: dict, start: datetime, end: datetime) -> list[tuple[str, datetime]]:
    """(slot_id, utc_time) for every non-poll slot, sorted. Poll slots are event-driven and exempt."""
    sched = entry.get("schedule") or {}
    tz = wc_manifest._tz(sched.get("timezone")) or timezone.utc
    out = []
    for slot in sched.get("slots") or []:
        if slot.get("type") == "poll":
            continue
        for t in _slot_times(slot, tz, start, end):
            out.append((str(slot.get("id")), t))
    return sorted(out, key=lambda x: x[1])


def _grace(entry: dict, min_grace_min: int) -> timedelta:
    base = ((entry.get("schedule") or {}).get("expected_duration_ms") or {}).get("baseline") or 0
    return timedelta(milliseconds=max(min_grace_min * 60000, 0.25 * base))


# ---------------------------------------------------------------------------------------------- signals
def _sig(type_, severity, automation, host, ring, window, detail, first_seen, last_seen, evidence, owner, **extra) -> dict:
    s = {"id": fingerprint(type_, automation, host, window), "type": type_, "severity": severity, "automation": automation,
         "host": host, "ring": ring, "detail": detail, "first_seen": iso(first_seen), "last_seen": iso(last_seen),
         "evidence": list(evidence), "diagnosis": None, "owner": owner}
    s.update(extra)
    return s


def compute_signals(data: Data, manifest: dict, now: datetime, cfg: dict, state: dict) -> tuple[list[dict], dict]:
    autos = manifest.get("automations") or {}
    fleet_paused = bool((manifest.get("fleet") or {}).get("paused"))
    lookback = now - timedelta(hours=cfg["lookback_h"])
    signals: list[dict] = []
    targets = state.setdefault("targets", {})

    # track target version changes so VERSION_DRIFT has a notion of "should have healed by now"
    def track(key: str, version):
        if version is None:
            return None
        cur = targets.get(key)
        if not cur or cur.get("version") != version:
            targets[key] = {"version": version, "since": iso(now)}
        return parse_iso(targets[key]["since"])

    for name, entry in autos.items():
        for ring, v in ((entry or {}).get("rings") or {}).items():
            track(f"{name}|{ring}", v)
    for cname, centry in (manifest.get("components") or {}).items():
        for ring, v in ((centry or {}).get("rings") or {}).items():
            track(f"component:{cname}|{ring}", v)

    # --- per (automation, host) latest-run signals
    latest: dict[tuple, list] = defaultdict(list)
    for r in data.runs:
        if r["status"] in _UNCOUNTED or r.get("slot") in ("adhoc", "dev"):
            continue
        latest[(r["automation"], r.get("host"))].append(r)

    for (name, host), runs in latest.items():
        entry = autos.get(name) or {}
        owner = entry.get("owner")
        last = runs[-1]
        if _ts(last) < lookback:
            continue
        ring = last.get("ring")
        base = ((entry.get("schedule") or {}).get("expected_duration_ms") or {})
        if last["status"] in _FAILED:
            trailing = []
            for r in reversed(runs):
                if r["status"] in _FAILED:
                    trailing.append(r)
                else:
                    break
            err = last.get("error") or {}
            cat = err.get("category") or "UNKNOWN"
            fp = error_fingerprint(cat, err.get("message"))
            n = len(trailing)
            signals.append(_sig("FAILED", "critical" if n >= 3 else "high", name, host, ring, fp,
                                f"{n} consecutive failed run(s). {cat}: {(err.get('message') or '')[:200]}",
                                _ts(trailing[-1]), _ts(last), [r["run_id"] for r in reversed(trailing)], owner,
                                error_category=cat, error_message=(err.get("message") or "")[:300], error_fp=fp,
                                synopsis=last.get("synopsis"), consecutive=n))
        elif last["status"] == "hung" or last.get("hung_detected"):
            killed = last["status"] == "hung"
            err = last.get("error") or {}
            signals.append(_sig("HUNG", "critical", name, host, ring, "hung",
                                ("Run exceeded its window and was killed at timeout." if killed
                                 else "Watchdog fired: run exceeded its expected window (it later finished)."),
                                _ts(last), _ts(last), [last["run_id"]], owner,
                                error_category=err.get("category") or "SYSTEM_UNAVAILABLE",
                                error_message=(err.get("message") or "")[:300],
                                error_fp=error_fingerprint(err.get("category"), err.get("message")), synopsis=last.get("synopsis")))
        else:
            if base.get("baseline") and last.get("duration_ms") and last["duration_ms"] > base["baseline"] * float(base.get("warn_threshold", 2.0)):
                signals.append(_sig("SLOW", "medium", name, host, ring, "slow",
                                    f"Run took {last['duration_ms'] // 1000}s; baseline {base['baseline'] // 1000}s x {base.get('warn_threshold', 2.0)} = "
                                    f"{int(base['baseline'] * float(base.get('warn_threshold', 2.0))) // 1000}s.",
                                    _ts(last), _ts(last), [last["run_id"]], owner, synopsis=last.get("synopsis"),
                                    error_category=None))
        items = last.get("items") or {}
        if items.get("failed") and last["status"] not in _FAILED + ("hung",):
            cats = sorted({(d.get("error_category") or "unspecified") for d in _events(last, "item_processed") if d.get("outcome") == "failed"})
            rate = items["failed"] / items["total"] if items.get("total") else 0
            signals.append(_sig("ITEM_FAILURES", "high" if rate >= 0.3 else "medium", name, host, ring, ",".join(cats),
                                f"{items['failed']} of {items['total']} item(s) failed ({', '.join(cats)}) although the run completed.",
                                _ts(last), _ts(last), [last["run_id"]], owner, error_category=cats[0].upper() if cats else None,
                                error_fp=fingerprint(",".join(cats), n=12), synopsis=last.get("synopsis"),
                                failure_rate=round(rate, 3)))

        # VERSION_DRIFT (latest run on this host)
        tv = ((entry.get("rings") or {}).get(ring)) if entry else None
        drift_bits = []
        since = parse_iso(targets[f"{name}|{ring}"]["since"]) if f"{name}|{ring}" in targets else None
        grace = timedelta(hours=cfg["drift_grace_h"])
        healed_errors = bool((last.get("self_heal") or {}).get("errors"))
        if tv and last.get("automation_version") and str(last["automation_version"]) != str(tv):
            if healed_errors or (since and _ts(last) >= since + grace):
                drift_bits.append(f"{name} ran {last['automation_version']}, ring '{ring}' targets {tv}")
        for cname, field in (("wrapper-core", "wrapper_version"), ("execution-framework", "framework_version")):
            ctv = (((manifest.get("components") or {}).get(cname) or {}).get("rings") or {}).get(ring)
            csince = parse_iso(targets[f"component:{cname}|{ring}"]["since"]) if f"component:{cname}|{ring}" in targets else None
            if ctv and last.get(field) and str(last[field]) != str(ctv) and (healed_errors or (csince and _ts(last) >= csince + grace)):
                drift_bits.append(f"{cname} {last[field]} != target {ctv}")
        if drift_bits:
            signals.append(_sig("VERSION_DRIFT", "medium", name, host, ring, "|".join(drift_bits),
                                "; ".join(drift_bits) + (" (self-heal reported errors)" if healed_errors else ""),
                                _ts(last), _ts(last), [last["run_id"]], owner,
                                error_message="; ".join((last.get("self_heal") or {}).get("errors") or [])[:300] or None))

    # --- UNEXPECTED: ran inside a declared maintenance window
    seen_unexp: dict[tuple, dict] = {}
    for r in data.runs:
        if r["status"] in _UNCOUNTED or r.get("slot") in ("adhoc", "dev") or _ts(r) < lookback:
            continue
        entry = autos.get(r["automation"]) or {}
        w = wc_manifest.in_maintenance(entry.get("schedule"), _ts(r))
        if w:
            seen_unexp[(r["automation"], r.get("host"))] = {"run": r, "window": w}
    for (name, host), v in seen_unexp.items():
        r = v["run"]
        signals.append(_sig("UNEXPECTED", "medium", name, host, r.get("ring"), str(v["window"]),
                            f"Run started {r['started_at']} inside maintenance window {v['window']}; the window is not propagating to the endpoint.",
                            _ts(r), _ts(r), [r["run_id"]], (autos.get(name) or {}).get("owner")))

    # --- MISSED
    first_seen: dict[str, datetime] = {}
    for r in data.runs:
        first_seen[r["automation"]] = min(first_seen.get(r["automation"], _ts(r)), _ts(r))
    for ev in data.lifecycle:
        if ev.get("event") == "installed" and ev.get("automation"):
            first_seen[ev["automation"]] = min(first_seen.get(ev["automation"], _ts(ev)), _ts(ev))
    missed_by_auto: dict[str, list] = defaultdict(list)
    for name, entry in autos.items():
        entry = entry or {}
        if fleet_paused or entry.get("status", "active") == "paused" or name not in first_seen:
            continue
        start = max(lookback, first_seen[name])
        grace = _grace(entry, cfg["min_grace_min"])
        exp = expected_runs(entry, start, now + timedelta(days=2))
        runs = [r for r in data.runs if r["automation"] == name]
        slot_ids = {s for s, _ in exp}
        for i, (slot_id, e) in enumerate(exp):
            if e < start or e > now or now < e + grace:
                continue
            if wc_manifest.in_maintenance(entry.get("schedule"), e):
                continue
            nxt = next((t for s, t in exp[i + 1:] if s == slot_id), e + timedelta(days=366))
            end = min(nxt, e + max(grace * 4, timedelta(hours=6)))  # a very late run is not 'this' expected run
            ran = any((e - timedelta(minutes=10)) <= _ts(r) < end and (r.get("slot") == slot_id or r.get("slot") not in slot_ids)
                      for r in runs)
            if not ran:
                missed_by_auto[name].append((slot_id, e))
    for name, items in missed_by_auto.items():
        entry = autos[name]
        sev = "critical" if len(items) >= 2 else "high"
        for slot_id, e in items:
            signals.append(_sig("MISSED", sev, name, None, None, iso(e) + "|" + slot_id,
                                f"Expected run of slot '{slot_id}' at {iso(e)} was not seen (grace elapsed).", e, now, [],
                                entry.get("owner"), slot=slot_id, expected_at=iso(e)))

    # --- SILENT: hosts that stopped checking in
    hb_latest: dict[str, dict] = {}
    for hb in data.heartbeats:
        hb_latest[hb["host"]] = hb
    silent_after = timedelta(hours=cfg["silent_after_h"])
    for host, hb in hb_latest.items():
        installed = dict(((hb.get("versions") or {}).get("automations")) or {})
        for ev in data.lifecycle:
            if ev.get("host") == host and ev.get("event") in ("decommissioned", "uninstalled") and _ts(ev) >= _ts(hb):
                installed.pop(ev.get("automation"), None)
        # latest run activity counts as life too (heartbeat may lag behind a spooled run)
        last_seen = max([_ts(hb)] + [_ts(r) for r in data.runs if r.get("host") == host][-1:])
        if installed and now - last_seen > silent_after:
            names = sorted(installed)
            owners = sorted({(autos.get(n) or {}).get("owner") for n in names if (autos.get(n) or {}).get("owner")})
            signals.append(_sig("SILENT", "high", names[0] if len(names) == 1 else None, host, hb.get("ring"), "silent",
                                f"No heartbeat from {host} for {int((now - last_seen).total_seconds() // 3600)}h "
                                f"(threshold {cfg['silent_after_h']}h); hosts {', '.join(names)}.",
                                last_seen, now, [], owners[0] if owners else None, automations=names))

    signals.sort(key=lambda s: (SEVERITY_ORDER[s["severity"]], s["first_seen"]))
    return signals, state


# ---------------------------------------------------------------------------------------------- report
def compute(data: Data, manifest: dict | None, now: datetime | None = None, config: dict | None = None,
            state: dict | None = None) -> tuple[dict, dict]:
    now = now or utcnow()
    cfg = dict(DEFAULTS, **(config or {}))
    manifest = manifest or {}
    state = copy.deepcopy(state or {})
    autos = manifest.get("automations") or {}
    window_start = now - timedelta(days=cfg["window_days"])
    runs = [r for r in data.runs if _ts(r) >= window_start]

    signals, state = compute_signals(data, manifest, now, cfg, state)
    sig_by_auto: dict[str, list] = defaultdict(list)
    for s in signals:
        for a in (s.get("automations") or [s["automation"]]):
            if a:
                sig_by_auto[a].append(s["id"])

    names = sorted(set(autos) | {r["automation"] for r in runs})
    automations = {}
    for name in names:
        entry = autos.get(name) or {}
        mine = [r for r in runs if r["automation"] == name]
        counted = [r for r in mine if r["status"] not in _UNCOUNTED]
        last = counted[-1] if counted else (mine[-1] if mine else None)
        durs = [r["duration_ms"] for r in counted if r.get("duration_ms") and r["status"] in ("success", "partial")]
        sev = sorted((s for s in signals if s["id"] in sig_by_auto.get(name, [])), key=lambda s: SEVERITY_ORDER[s["severity"]])
        if entry.get("status") == "paused" or (manifest.get("fleet") or {}).get("paused"):
            health = "paused"
        elif sev and sev[0]["severity"] in ("critical", "high"):
            health = "failing"
        elif sev:
            health = "degraded"
        elif last is None:
            health = "unknown"
        else:
            health = "healthy"
        automations[name] = {
            "description": entry.get("description"), "owner": entry.get("owner"), "owner_team": entry.get("owner_team"),
            "data_classification": entry.get("data_classification"), "systems": entry.get("systems"),
            "status": entry.get("status", "active" if entry else "unregistered"), "paused_reason": entry.get("paused_reason"),
            "ring_versions": entry.get("rings"), "health": health, "signals": sig_by_auto.get(name, []),
            "runs": status_counts(mine), "duration_ms": {"p50": percentile(durs, 50), "p95": percentile(durs, 95)},
            "items": item_metrics(mine), "retries": retry_stats(mine), "llm": llm_stats(mine),
            "last_run": run_summary(last) if last else None,
            "recent_runs": [run_summary(r) for r in mine[-10:]][::-1],
            "daily": _daily(mine, now, cfg["trend_days"]), "hosts": sorted({r.get("host") for r in mine if r.get("host")}),
            "slots": [(s.get("id"), s.get("type")) for s in ((entry.get("schedule") or {}).get("slots") or [])],
        }

    by_ring, by_host = {}, {}
    for key, fld, out in (("ring", "ring", by_ring), ("host", "host", by_host)):
        groups: dict = defaultdict(list)
        for r in runs:
            groups[r.get(fld)].append(r)
        for g, rs in groups.items():
            out[str(g)] = {"runs": status_counts(rs), "last_run_at": max(r["started_at"] for r in rs)}

    versions = _versions(data, manifest, autos)
    items_7d = _items_since(runs, now - timedelta(days=7))
    items_30d = _items_since(runs, now - timedelta(days=30))
    saved = 0.0
    saved_known = False
    for name, entry in autos.items():
        mpi = ((entry or {}).get("catalog") or {}).get("minutes_per_item")
        if mpi:
            saved_known = True
            saved += _items_since([r for r in runs if r["automation"] == name], now - timedelta(days=30)) * float(mpi) / 60.0
    sev_counts = Counter(s["severity"] for s in signals)
    fleet = status_counts(runs)
    fleet.update({"automations": len(names), "hosts": len({r.get("host") for r in runs if r.get("host")} | {h["host"] for h in data.heartbeats}),
                  "signals": dict(sev_counts), "signals_total": len(signals),
                  "status": ("paused" if (manifest.get("fleet") or {}).get("paused")
                             else "critical" if sev_counts["critical"] else "attention" if sev_counts["high"] else
                             "degraded" if sev_counts["medium"] else "healthy")})
    report = {
        "schema_version": 1, "generated_at": iso(now), "window_days": cfg["window_days"], "config": cfg, "fleet": fleet,
        "fleet_paused": bool((manifest.get("fleet") or {}).get("paused")),
        "data_quality": {"skipped_malformed": int(getattr(data, "skipped_malformed", 0))},
        "automations": automations, "signals": signals, "by_ring": by_ring, "by_host": by_host,
        "items": item_metrics(runs), "retries": retry_stats(runs), "llm": llm_stats(runs),
        "trend": _daily(runs, now, cfg["trend_days"]), "versions": versions,
        "impact": {"items_7d": items_7d, "items_30d": items_30d,
                   "hours_saved_30d": round(saved, 1) if saved_known else None},
    }
    return report, state


def _daily(runs: list[dict], now: datetime, days: int) -> list[dict]:
    today = now.date()
    rows = {}
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        rows[d.isoformat()] = {"date": d.isoformat(), "runs": 0, "success": 0, "partial": 0, "failed": 0, "items": 0, "duration_ms": 0}
    for r in runs:
        row = rows.get(_ts(r).date().isoformat())
        if not row or r["status"] in _UNCOUNTED:
            continue
        row["runs"] += 1
        key = "failed" if r["status"] in _FAILED + ("hung",) else r["status"]
        if key in row:
            row[key] += 1
        row["items"] += (r.get("items") or {}).get("total", 0)
        row["duration_ms"] += r.get("duration_ms") or 0
    return list(rows.values())


def _items_since(runs, since) -> int:
    return sum((r.get("items") or {}).get("total", 0) for r in runs if _ts(r) >= since)


def _versions(data: Data, manifest: dict, autos: dict) -> dict:
    comps = manifest.get("components") or {}
    hosts: dict[str, dict] = {}
    for hb in data.heartbeats:
        hosts[hb["host"]] = hb
    rows = []
    for host, hb in sorted(hosts.items()):
        ring = hb.get("ring")
        v = hb.get("versions") or {}
        row = {"host": host, "ring": ring, "last_seen": hb["ts"], "components": {}, "automations": {}}
        for cname, key in (("wrapper-core", "wrapper-core"), ("execution-framework", "execution-framework")):
            target = (((comps.get(cname) or {}).get("rings")) or {}).get(ring)
            have = v.get(key)
            row["components"][cname] = {"version": have, "target": target, "ok": (target is None or have == target)}
        for a, ver in (v.get("automations") or {}).items():
            target = (((autos.get(a) or {}).get("rings")) or {}).get(ring)
            row["automations"][a] = {"version": ver, "target": target, "ok": (target is None or str(ver) == str(target))}
        rows.append(row)
    return {"hosts": rows,
            "targets": {n: (e or {}).get("rings") for n, e in autos.items()},
            "component_targets": {n: (c or {}).get("rings") for n, c in comps.items()}}
