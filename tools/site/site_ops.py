"""Operator console pages: Jobs, Machines, Queues & approvals, Schedules, Audit.

Everything here is derived from two kinds of input and nothing else (no demo numbers):

* the log archive (``automation_logging.analytics.Data``): run / heartbeat / lifecycle / evidence artefacts submitted by installed
  clients. Those artefacts are UNAUTHENTICATED, so every value is clamped and HTML-escaped before it reaches a page, unknown or
  malformed fields are tolerated (ignored), and nothing from telemetry is ever interpolated into script.
* the registry: ``manifest.yaml``, ``history/CHANGELOG.md`` and the optional ``manifest.yaml.sig``.

Pages: ``jobs/`` (history table, client-side filter/sort/paging + detail drawer fed by ``runs-detail.json``), ``machines/``,
``queues/``, ``schedules/``, ``audit/``. Without an archive the telemetry pages say "waiting for the first run".
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from site_common import SiteConfig, esc, guarded, layout, write

MAX_JOBS = 2000          # newest runs shown on the Jobs page (the page says so)
MAX_EVENTS = 40          # events kept per run in the detail file
STR_MAX = 300
SILENT_AFTER_H = 36      # same default as analytics (silent_after_h)
STALE_AFTER_H = 12
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

JOBS_JS = Path(__file__).with_name("jobs.js")


# ----------------------------------------------------------------------------------------------------- sanitising helpers
def _s(v, n: int = STR_MAX) -> str:
    """Clamp any telemetry value to a short printable string (still needs esc() before it goes into HTML)."""
    if v is None:
        return ""
    if isinstance(v, (dict, list, tuple, set)):
        return ""
    return _CTRL.sub("", str(v))[:n]


def _num(v, lo: float = 0, hi: float = 1e12):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or math.isinf(v):
        return None
    return max(lo, min(hi, v))


def _int(v) -> int:
    n = _num(v)
    return int(n) if n is not None else 0


def _ts(v):
    if not isinstance(v, str) or len(v) > 40:
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else ""


def _id(v) -> str:
    return v if isinstance(v, str) and ID_RE.match(v) else ""


def _dur(ms) -> str:
    ms = _num(ms)
    if ms is None:
        return "-"
    s = ms / 1000
    return f"{int(ms)} ms" if s < 1 else (f"{s:.1f} s" if s < 60 else f"{int(s // 60)}m {int(s % 60):02d}s")


def _age(d, now) -> str:
    if not d:
        return "never"
    secs = max(0, (now - d).total_seconds())
    return f"{int(secs // 60)} min ago" if secs < 3600 else (f"{secs / 3600:.1f} h ago" if secs < 172800 else f"{secs / 86400:.0f} d ago")


def _pill(text, kind: str) -> str:
    return f'<span class="pill {esc(kind)}">{esc(text)}</span>'


def _kind_for_status(status: str) -> str:
    return {"success": "ok", "partial": "medium", "failed": "failing", "self_heal_failed": "failing", "hung": "failing",
            "halted_paused": "paused", "skipped_maintenance": "paused"}.get(status, "unknown")


def _page(title: str, active: str, body: str, cfg: SiteConfig, out: Path, rel: str, extra_head: str = "", extra_js: str = "") -> str:
    crumbs = f'<div class="crumbs"><a href="../index.html">Home</a> / {esc(title)}</div>'
    write(out, rel, layout(title, crumbs + f"<h1>{esc(title)}</h1>" + body, cfg=cfg, depth=rel.count("/"), active=active,
                           extra_head=extra_head, extra_js=extra_js))
    return rel


def _waiting(what: str) -> str:
    return (f'<div class="panel"><p><strong>Waiting for the first run.</strong> {esc(what)} is built from the run logs, heartbeats and lifecycle '
            "events that installed automations push to the private logs repository. Nothing is shown until real data arrives (no sample numbers).</p>"
            '<p>See <a href="../docs/LIVE_LOGGING.html">Live logging setup</a>.</p></div>')


def _table(headers: list[str], rows: list[list[str]], empty: str = "Nothing to show yet.") -> str:
    """rows are lists of ALREADY-ESCAPED html cells."""
    if not rows:
        return f'<p class="sub">{esc(empty)}</p>'
    th = "".join(f"<th>{esc(h)}</th>" for h in headers)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'


# ------------------------------------------------------------------------------------------------------------------- JOBS
def _evidence_links(data, out: Path) -> dict:
    """run_id -> [relative url under jobs/] for evidence payloads the dashboard actually published."""
    links: dict[str, list[str]] = {}
    base = out / "dashboard" / "evidence"
    for rec in getattr(data, "evidence", None) or []:
        rid = _id(rec.get("run_id"))
        if not rid:
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", rid)
        d = base / safe
        if d.is_dir():
            for f in sorted(p for p in d.iterdir() if p.is_file())[:6]:
                links.setdefault(rid, []).append(f"../dashboard/evidence/{safe}/{f.name}")
    return links


def _flat(d, limit: int = 12) -> dict:
    out = {}
    if isinstance(d, dict):
        for k, v in list(d.items())[:limit]:
            if isinstance(v, (str, int, float, bool)) or v is None:
                out[_s(k, 40)] = _s(v, 120) if isinstance(v, str) else v
    return out


def build_jobs(out: Path, cfg: SiteConfig, data, now) -> list[str]:
    if data is None or not getattr(data, "runs", None):
        return [_page("Jobs", "jobs", _waiting("The job history"), cfg, out, "jobs/index.html")]
    runs = sorted(data.runs, key=lambda r: str(r.get("started_at")), reverse=True)
    shown = runs[:MAX_JOBS]
    ev = _evidence_links(data, out)
    detail: dict[str, dict] = {}
    rows: list[str] = []
    autos, hosts = set(), set()
    for r in shown:
        rid = _id(r.get("run_id"))
        if not rid or rid in detail:
            continue
        started = _ts(r.get("started_at"))
        items = r.get("items") if isinstance(r.get("items"), dict) else {}
        ok, bad, skipped = _int(items.get("success")), _int(items.get("failed")), _int(items.get("skipped"))
        status, auto, host, ring = _s(r.get("status"), 40), _s(r.get("automation"), 100), _s(r.get("host"), 100), _s(r.get("ring"), 20)
        autos.add(auto)
        hosts.add(host)
        events = r.get("events") if isinstance(r.get("events"), list) else []
        retries = [e for e in events if isinstance(e, dict) and e.get("event") == "retry_event"]
        err = r.get("error") if isinstance(r.get("error"), dict) else {}
        detail[rid] = {
            "error": {"category": _s(err.get("category"), 60), "type": _s(err.get("type"), 80), "message": _s(err.get("message"), 400),
                      "attempts": _int(err.get("attempts"))} if err else None,
            "retries": [{"attempt": _int((e.get("data") or {}).get("attempt")), "decision": _s((e.get("data") or {}).get("decision"), 40),
                         "category": _s((e.get("data") or {}).get("category"), 40), "wait_s": _num((e.get("data") or {}).get("wait_s")),
                         "outcome": _s((e.get("data") or {}).get("outcome"), 40)} for e in retries[:20] if isinstance(e.get("data"), dict)],
            "events": [{"ts": _s(e.get("ts"), 30), "event": _s(e.get("event"), 60), "data": _flat(e.get("data"))}
                       for e in events[:MAX_EVENTS] if isinstance(e, dict) and e.get("event") != "item_processed"],
            "evidence": ev.get(rid, []),
            "version": _s(r.get("automation_version"), 40), "wrapper": _s(r.get("wrapper_version"), 40), "slot": _s(r.get("slot"), 60),
            "result": _flat(r.get("result")), "exit_code": r.get("exit_code") if isinstance(r.get("exit_code"), int) else None,
        }
        syn = _s(r.get("synopsis"), 200)
        from automation_logging import reliability as _rel  # noqa: PLC0415
        esig = _rel.error_signature(err.get("category"), err.get("message")) if err else ""
        search = " ".join((rid, auto, host, status, syn, _s(err.get("category"), 60), esig)).lower()
        epoch = int(started.timestamp()) if started else 0
        rows.append(
            f'<tr data-id="{esc(rid)}" data-status="{esc(status)}" data-auto="{esc(auto)}" data-host="{esc(host)}" data-ring="{esc(ring)}" '
            f'data-ts="{epoch}" data-dur="{int(_num(r.get("duration_ms")) or 0)}" data-q="{esc(search)}">'
            f'<td>{_pill(status or "unknown", _kind_for_status(status))}</td><td>{esc(auto)}</td><td>{esc(host)}</td><td>{esc(ring)}</td>'
            f'<td>{esc(_s(r.get("automation_version"), 40))}</td><td>{esc(_iso(started))}</td><td>{esc(_dur(r.get("duration_ms")))}</td>'
            f'<td>{ok}/{bad}/{skipped}</td><td>{esc(syn)}</td><td><button type="button" class="openrun" data-run="{esc(rid)}">Details</button>'
            f'{" &middot; evidence" if ev.get(rid) else ""}</td></tr>')
    opt = lambda vals: "".join(f'<option value="{esc(v)}">{esc(v)}</option>' for v in sorted(x for x in vals if x))  # noqa: E731
    statuses = sorted({_s(r.get("status"), 40) for r in shown})
    note = (f"Showing the newest {len(rows)} of {len(runs)} runs in the archive window." if len(runs) > MAX_JOBS
            else f"{len(rows)} run(s) in the archive window.")
    body = f"""
<p class="sub">{esc(note)} Items are success / failed / skipped. Filters and the open run live in the URL, so a link to this page reproduces your view.</p>
<form id="jobfilter" class="panel" style="display:flex;gap:.6rem;flex-wrap:wrap;align-items:end">
<label>Search<br><input id="f-q" type="search" maxlength="100" placeholder="run id, automation, host, synopsis"></label>
<label>Status<br><select id="f-status"><option value="">any</option>{opt(statuses)}</select></label>
<label>Automation<br><select id="f-auto"><option value="">any</option>{opt(autos)}</select></label>
<label>Machine<br><select id="f-host"><option value="">any</option>{opt(hosts)}</select></label>
<label>Ring<br><select id="f-ring"><option value="">any</option><option>dev</option><option>pilot</option><option>broad</option></select></label>
<span id="job-count" class="sub" aria-live="polite"></span></form>
<div class="tw"><table id="jobs"><thead><tr>
<th data-sort="status">Status</th><th data-sort="auto">Automation</th><th data-sort="host">Machine</th><th data-sort="ring">Ring</th><th>Version</th>
<th data-sort="ts">Started (UTC)</th><th data-sort="dur">Duration</th><th>Items ok/failed/skipped</th><th>Synopsis</th><th></th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<div id="pager" class="sub" style="margin:.6rem 0"><button type="button" id="prev">Previous</button> <span id="pageinfo"></span> <button type="button" id="next">Next</button></div>
<aside id="drawer" class="panel" role="dialog" aria-label="Run details" hidden>
<button type="button" id="drawer-close" style="float:right">Close</button><h2 id="drawer-title">Run</h2><div id="drawer-body"></div></aside>
<noscript><p class="warnbox">Filtering, sorting and the details drawer need JavaScript; the table above is complete without it.</p></noscript>
"""
    write(out, "jobs/runs-detail.json", json.dumps(detail, sort_keys=True, separators=(",", ":")))
    write(out, "assets/jobs.js", JOBS_JS.read_text(encoding="utf-8"))
    return [_page("Jobs", "jobs", body, cfg, out, "jobs/index.html", extra_js='<script src="../assets/jobs.js"></script>'), "jobs/runs-detail.json"]


# --------------------------------------------------------------------------------------------------------------- MACHINES
def _ring_target(manifest: dict, kind: str, name: str, ring: str):
    table = (manifest.get(kind) or {}).get(name) or {}
    return ((table.get("rings") if isinstance(table, dict) else None) or {}).get(ring)


def machine_rows(data, manifest: dict, now) -> list[dict]:
    hosts: dict[str, dict] = {}
    for hb in sorted(getattr(data, "heartbeats", None) or [], key=lambda h: str(h.get("ts"))):
        h = _s(hb.get("host"), 100)
        if h:
            hosts[h] = hb
    last_run: dict[str, dict] = {}
    for r in getattr(data, "runs", None) or []:
        h = _s(r.get("host"), 100)
        if h and (h not in last_run or str(r.get("started_at")) > str(last_run[h].get("started_at"))):
            last_run[h] = r
    rows = []
    for host in sorted(set(hosts) | set(last_run)):
        hb, run = hosts.get(host), last_run.get(host)
        seen = _ts((hb or {}).get("ts")) or _ts((run or {}).get("started_at"))
        ring = _s((hb or {}).get("ring") or (run or {}).get("ring"), 20)
        v = (hb or {}).get("versions") if isinstance((hb or {}).get("versions"), dict) else {}
        drift = []
        for comp in ("wrapper-core", "execution-framework"):
            have, target = _s(v.get(comp), 40), _ring_target(manifest, "components", comp, ring)
            if have and target and have != str(target):
                drift.append(f"{comp} {have} (ring {ring}: {target})")
        for a, ver in (v.get("automations") or {}).items() if isinstance(v.get("automations"), dict) else []:
            target = _ring_target(manifest, "automations", a, ring)
            if ver and target and str(ver) != str(target):
                drift.append(f"{_s(a, 80)} {_s(ver, 40)} (ring {ring}: {target})")
        age_h = (now - seen).total_seconds() / 3600 if seen else None
        fresh = "unknown" if age_h is None else ("ok" if age_h <= STALE_AFTER_H else ("medium" if age_h <= SILENT_AFTER_H else "failing"))
        signals = []
        if age_h is None or age_h > SILENT_AFTER_H:
            signals.append("SILENT")
        if drift:
            signals.append("VERSION_DRIFT")
        if run and run.get("status") in ("failed", "self_heal_failed", "hung"):
            signals.append("LAST_RUN_FAILED")
        rows.append({"host": host, "ring": ring, "seen": seen, "fresh": fresh, "phase": _s((hb or {}).get("phase"), 40), "versions": v,
                     "drift": drift, "run": run, "signals": signals})
    return rows


def build_machines(out: Path, cfg: SiteConfig, data, manifest: dict, now) -> list[str]:
    if data is None or not (getattr(data, "heartbeats", None) or getattr(data, "runs", None)):
        return [_page("Machines", "machines", _waiting("The machine list"), cfg, out, "machines/index.html")]
    rows_data = machine_rows(data, manifest, now)
    rows = []
    for m in rows_data:
        v = m["versions"] if isinstance(m["versions"], dict) else {}
        autos = v.get("automations") if isinstance(v.get("automations"), dict) else {}
        run = m["run"]
        rows.append([esc(m["host"]), esc(m["ring"] or "-"), _pill(_age(m["seen"], now), m["fresh"]), esc(m["phase"] or "-"),
                     esc(f'wrapper-core {_s(v.get("wrapper-core"), 30) or "?"}; framework {_s(v.get("execution-framework"), 30) or "?"}; ' +
                         ", ".join(f"{_s(a, 60)} {_s(x, 30)}" for a, x in list(autos.items())[:8])),
                     (esc("; ".join(m["drift"])) if m["drift"] else _pill("on target", "ok")),
                     (f'{_pill(_s(run.get("status"), 30), _kind_for_status(_s(run.get("status"), 30)))} {esc(_age(_ts(run.get("started_at")), now))}'
                      if run else "-"),
                     "".join(_pill(s, "failing" if s != "VERSION_DRIFT" else "medium") + " " for s in m["signals"]) or _pill("healthy", "ok")])
    body = (f'<p class="sub">Heartbeat freshness: fresh &le; {STALE_AFTER_H} h, aging &le; {SILENT_AFTER_H} h, silent beyond that. Versions are compared with the '
            "manifest target for the machine's ring. Machines appear only after they have reported at least once.</p>" +
            _table(["Machine", "Ring", "Last seen", "Phase", "Installed", "Versions vs ring target", "Last run", "Signals"], rows))
    return [_page("Machines", "machines", body, cfg, out, "machines/index.html")]


# ------------------------------------------------------------------------------------------------------ QUEUES & APPROVALS
def _event_stream(data):
    """Yield (ts_dt, automation, event_name, data_dict) from run events and lifecycle events (defensively)."""
    for r in getattr(data, "runs", None) or []:
        auto = _s(r.get("automation"), 100)
        for e in (r.get("events") if isinstance(r.get("events"), list) else [])[:5000]:
            if isinstance(e, dict) and isinstance(e.get("event"), str):
                yield (_ts(e.get("ts")) or _ts(r.get("started_at")), auto, e["event"], e.get("data") if isinstance(e.get("data"), dict) else {})
    for ev in getattr(data, "lifecycle", None) or []:
        if isinstance(ev.get("event"), str):
            yield (_ts(ev.get("ts")), _s(ev.get("automation"), 100), ev["event"], ev.get("detail") if isinstance(ev.get("detail"), dict) else {})


def queue_and_approval_state(data, now) -> dict:
    queues: dict[tuple, dict] = {}
    approvals: dict[tuple, dict] = {}
    for ts, auto, name, d in _event_stream(data):
        if name == "queue_stats":
            q = _s(d.get("queue"), 80) or "default"
            cur = queues.get((auto, q))
            if cur is None or (ts and (cur["ts"] is None or ts >= cur["ts"])):
                queues[(auto, q)] = {"ts": ts, "automation": auto, "queue": q, **{k: _int(d.get(k)) for k in
                                     ("pending", "in_progress", "done", "failed", "dead_letter", "sla_breached")},
                                     "oldest_age_s": _num(d.get("oldest_age_s"))}
        elif name in ("approval_requested", "approval_decided"):
            tid = _id(d.get("task_id"))
            if not tid:
                continue
            a = approvals.setdefault((auto, tid), {"automation": auto, "task": tid, "requested": None, "decided": None, "decision": "",
                                                    "title": "", "sla_s": None, "approver": ""})
            if name == "approval_requested":
                a["requested"] = ts if a["requested"] is None or (ts and ts < a["requested"]) else a["requested"]
                a["title"] = a["title"] or _s(d.get("title") or d.get("reason"), 120)
                a["sla_s"] = _num(d.get("sla_s")) if a["sla_s"] is None else a["sla_s"]
            else:
                dec = _s(d.get("decision"), 20).lower()
                a["decided"] = ts
                a["decision"] = dec if dec in ("approved", "rejected", "expired", "cancelled") else "unknown"
                a["approver"] = _s(d.get("decided_by"), 80)
    open_, breached = [], 0
    for a in approvals.values():
        if a["decided"] is None:
            wait = (now - a["requested"]).total_seconds() if a["requested"] else None
            a["wait_s"] = wait
            a["breach"] = bool(a["sla_s"] and wait is not None and wait > a["sla_s"])
            breached += a["breach"]
            open_.append(a)
        else:
            a["wait_s"] = (a["decided"] - a["requested"]).total_seconds() if a["requested"] else None
            a["breach"] = bool(a["sla_s"] and a["wait_s"] is not None and a["wait_s"] > a["sla_s"])
    return {"queues": sorted(queues.values(), key=lambda q: (q["automation"], q["queue"])), "approvals": approvals, "open": open_,
            "approvals_breached": breached}


def build_queues(out: Path, cfg: SiteConfig, data, now) -> list[str]:
    if data is None or not (getattr(data, "runs", None) or getattr(data, "lifecycle", None)):
        return [_page("Queues & approvals", "queues", _waiting("Queue and approval activity"), cfg, out, "queues/index.html")]
    st = queue_and_approval_state(data, now)
    qrows = [[esc(q["automation"]), esc(q["queue"]), str(q["pending"]), str(q["in_progress"]), str(q["done"]), str(q["failed"]),
              (_pill(str(q["dead_letter"]), "failing") if q["dead_letter"] else "0"),
              (_pill(str(q["sla_breached"]), "high") if q["sla_breached"] else "0"),
              esc(_dur(q["oldest_age_s"] * 1000) if q["oldest_age_s"] is not None else "-"), esc(_iso(q["ts"]))] for q in st["queues"]]
    decided = sorted((a for a in st["approvals"].values() if a["decided"]), key=lambda a: a["decided"], reverse=True)[:100]
    orows = [[esc(a["automation"]), esc(a["task"]), esc(a["title"] or "-"), esc(_iso(a["requested"])), esc(_dur((a["wait_s"] or 0) * 1000) if a["wait_s"] is not None else "-"),
              (_pill("SLA breached", "high") if a["breach"] else _pill("waiting", "medium"))] for a in sorted(st["open"], key=lambda a: str(a["requested"]))[:200]]
    drows = [[esc(a["automation"]), esc(a["task"]), esc(a["title"] or "-"), _pill(a["decision"], {"approved": "ok", "rejected": "failing"}.get(a["decision"], "unknown")),
              esc(a["approver"] or "-"), esc(_iso(a["decided"])), esc(_dur(a["wait_s"] * 1000) if a["wait_s"] is not None else "-")] for a in decided]
    dead = sum(q["dead_letter"] for q in st["queues"])
    sla = sum(q["sla_breached"] for q in st["queues"]) + st["approvals_breached"]
    tiles = (f'<div class="tiles"><div class="tile"><div class="n">{len(st["queues"])}</div><div class="l">Queues reporting</div></div>'
             f'<div class="tile"><div class="n" style="color:var(--red)">{dead}</div><div class="l">Dead-lettered items</div></div>'
             f'<div class="tile"><div class="n" style="color:var(--yellow)">{len(st["open"])}</div><div class="l">Approvals waiting</div></div>'
             f'<div class="tile"><div class="n" style="color:var(--orange)">{sla}</div><div class="l">SLA breaches</div></div></div>')
    body = (tiles + '<p class="sub">Built from <code>queue_stats</code>, <code>approval_requested</code> and <code>approval_decided</code> events that automations emit. '
            "Queues are per machine (the durable queue is local); the latest report per automation and queue is shown.</p>"
            "<h2>Queues</h2>" + _table(["Automation", "Queue", "Pending", "In progress", "Done", "Failed", "Dead letter", "SLA breached", "Oldest item", "Reported"], qrows,
                                      "No queue statistics reported yet.") +
            "<h2>Approvals waiting</h2>" + _table(["Automation", "Task", "Title", "Requested", "Waiting", "State"], orows, "No approvals are waiting.") +
            "<h2>Recent decisions</h2>" + _table(["Automation", "Task", "Title", "Decision", "By", "Decided", "Time to decide"], drows, "No decisions recorded yet."))
    return [_page("Queues & approvals", "queues", body, cfg, out, "queues/index.html")]


# ------------------------------------------------------------------------------------------------------------- SCHEDULES
_CRON_FIELD = re.compile(r"^(\*|\d+(-\d+)?)(/\d+)?(,(\*|\d+(-\d+)?)(/\d+)?)*$")
_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _tz(name):
    if not name or str(name).upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(name))
    except Exception:  # noqa: BLE001 - missing tz database or bad name: fall back to UTC and say so on the page
        return None


def _hhmm(v):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(v or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))


def _cron_match(field: str, value: int, lo: int, hi: int) -> bool:
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                return False
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            if not (x.isdigit() and y.isdigit()):
                return False
            a, b = int(x), int(y)
        elif part.isdigit():
            a = b = int(part)
        else:
            return False
        if a <= value <= b and (value - a) % step == 0:
            return True
    return False


def next_runs(slot: dict, tz_name, after: datetime, count: int = 5, horizon_days: int = 62):
    """Next ``count`` fire times (UTC datetimes) for a manifest slot, or (None, reason) when the slot type is not understood.

    Supported: daily, weekdays, weekly(days), monthly(day), hourly(every), interval/poll(every / every_minutes / every_hours), cron (5 fields,
    ``*`` ``,`` ``-`` ``/``). Unknown types fall back gracefully - the page lists the slot without a calendar rather than guessing.
    """
    if not isinstance(slot, dict):
        return None, "malformed slot"
    stype = str(slot.get("type") or "").lower()
    tz = _tz(tz_name)
    note = "" if tz is not None else f"unknown timezone {_s(tz_name, 40)!r}: shown in UTC"
    tz = tz or timezone.utc
    start = after.astimezone(tz)
    end = start + timedelta(days=horizon_days)
    out: list[datetime] = []
    if stype in ("hourly", "interval", "poll"):
        if stype == "hourly":
            step = timedelta(hours=_num(slot.get("every"), 1, 168) or 1)
        elif "every_minutes" in slot or stype == "poll":
            step = timedelta(minutes=_num(slot.get("every_minutes"), 1, 10080) or 5)
        elif "every_hours" in slot:
            step = timedelta(hours=_num(slot.get("every_hours"), 1, 168) or 1)
        else:
            step = timedelta(minutes=_num(slot.get("every"), 1, 10080) or 60)
        epoch = start.replace(hour=0, minute=0, second=0, microsecond=0)
        n = int((start - epoch) / step) + 1          # first tick strictly after ``after``
        t = epoch + n * step
        while t < end and len(out) < count:
            out.append(t)
            t += step
        return [d.astimezone(timezone.utc) for d in out], note
    if stype == "cron":
        parts = str(slot.get("cron") or slot.get("expression") or "").split()
        if len(parts) != 5 or not all(_CRON_FIELD.match(p) for p in parts):
            return None, "cron needs 5 fields of numbers, *, ranges (a-b), lists (a,b) and steps (/n)"
        t = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
        guard = 0
        while t < end and len(out) < count and guard < 62 * 1440:
            guard += 1
            if (_cron_match(parts[0], t.minute, 0, 59) and _cron_match(parts[1], t.hour, 0, 23) and _cron_match(parts[2], t.day, 1, 31)
                    and _cron_match(parts[3], t.month, 1, 12) and _cron_match(parts[4], t.isoweekday() % 7, 0, 6)):
                out.append(t)
            t += timedelta(minutes=1)
        return [d.astimezone(timezone.utc) for d in out], note
    hm = _hhmm(slot.get("time"))
    if stype in ("daily", "weekdays", "weekly", "monthly"):
        if hm is None:
            return None, "missing or invalid time (HH:MM)"
        days = {str(d).lower() for d in (slot.get("days") or [])} if stype == "weekly" else None
        if stype == "weekly" and not days:
            days = {"monday"}
        dom = int(_num(slot.get("day"), 1, 31) or 1) if stype == "monthly" else None
        d0 = start.date()
        for i in range(horizon_days + 1):
            day = d0 + timedelta(days=i)
            if stype == "weekdays" and day.weekday() > 4:
                continue
            if stype == "weekly" and _DAYS[day.weekday()] not in days:
                continue
            if stype == "monthly" and day.day != dom:
                continue
            t = datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=tz)
            if t > start:
                out.append(t)
                if len(out) >= count:
                    break
        return [d.astimezone(timezone.utc) for d in out], note
    return None, f"slot type {_s(stype, 30) or '?'!r} is not shown on the calendar"


def _in_maintenance(t: datetime, windows, tz_name) -> bool:
    tz = _tz(tz_name) or timezone.utc
    local = t.astimezone(tz)
    for w in windows or []:
        if isinstance(w, dict) and any(str(d).lower() == _DAYS[local.weekday()] for d in (w.get("days") or [])):
            return True
    return False


def build_schedules(out: Path, cfg: SiteConfig, manifest: dict, now) -> list[str]:
    rows = []
    for name, entry in sorted((manifest.get("automations") or {}).items()):
        entry = entry if isinstance(entry, dict) else {}
        sched = entry.get("schedule") if isinstance(entry.get("schedule"), dict) else {}
        tz_name = sched.get("timezone")
        paused = entry.get("status") == "paused" or bool((manifest.get("fleet") or {}).get("paused"))
        for slot in sched.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            times, note = next_runs(slot, tz_name, now, 5)
            cells = []
            for t in times or []:
                held = _in_maintenance(t, sched.get("maintenance_windows"), tz_name)
                cells.append(f'{esc(_iso(t))}{" (maintenance window: will not run)" if held else ""}')
            rows.append([esc(_s(name, 100)), esc(_s(slot.get("id"), 60)), esc(_s(slot.get("type"), 30)),
                         esc(_s(slot.get("time") or slot.get("cron") or slot.get("every_minutes") or slot.get("every") or "", 40)),
                         esc(_s(tz_name, 40) or "UTC"),
                         (_pill("paused", "paused") + " " if paused else "") + ("<br>".join(cells) if cells else esc(note or "no upcoming runs in the next 62 days")),
                         esc(note if cells and note else "")])
    body = ('<p class="sub">Next fire times computed from the manifest (UTC). Machines run a slot via Windows Task Scheduler at the slot time in the schedule timezone; '
            "a machine that was off runs it at the next start (StartWhenAvailable). Paused automations and maintenance windows do not run.</p>" +
            _table(["Automation", "Slot", "Type", "When", "Timezone", "Next runs (UTC)", "Note"], rows, "No schedules are defined in the manifest."))
    return [_page("Schedules", "schedules", body, cfg, out, "schedules/index.html")]


# ------------------------------------------------------------------------------------------------------------------ AUDIT
def signing_status(manifest_bytes: bytes | None, sig_text: str | None) -> dict:
    if sig_text is None:
        return {"state": "unsigned", "detail": "No manifest.yaml.sig is published: endpoints cannot verify who published this registry. "
                                                "Set REGISTRY_SIGNING_SEED (tools/setup_live.py) to sign it."}
    try:
        doc = json.loads(sig_text)
    except ValueError:
        return {"state": "invalid", "detail": "manifest.yaml.sig is not valid JSON."}
    if not isinstance(doc, dict):
        return {"state": "invalid", "detail": "manifest.yaml.sig is not an object."}
    info = {"key_id": _s(doc.get("key_id"), 16), "signed_at": _s(doc.get("signed_at"), 40)}
    try:
        from wrapper_core import trust
        keys = trust.configured_keys(None)
        if manifest_bytes is None:
            return {"state": "present", "detail": "Signature published; manifest not available to verify against.", **info}
        if keys:
            trust.verify_manifest(manifest_bytes, doc, keys)
            return {"state": "valid", "detail": "Signature verifies against a key pinned in this build.", **info}
        sha_ok = doc.get("manifest_sha256") == hashlib.sha256(manifest_bytes).hexdigest()
        return {"state": "present", "detail": ("Signature published and it matches this manifest's SHA-256, but no public key is pinned in this build, so the "
                                                 "signer is not verified here (pin it via AUTOSTATION_REGISTRY_PUBKEY)." if sha_ok else
                                                 "Signature published but it does NOT match this manifest's SHA-256."), **info}
    except Exception as exc:  # noqa: BLE001 - TrustError or a missing wrapper_core: report, never crash the build
        return {"state": "invalid", "detail": _s(exc, 200), **info}


def build_audit(out: Path, cfg: SiteConfig, manifest: dict, data, repo: Path, registry_dir: Path | None, now) -> list[str]:
    parts = []
    # signing
    mbytes = sig_text = None
    if registry_dir:
        mp, sp = Path(registry_dir) / "manifest.yaml", Path(registry_dir) / "manifest.yaml.sig"
        mbytes = mp.read_bytes() if mp.is_file() else None
        sig_text = sp.read_text(encoding="utf-8", errors="replace") if sp.is_file() else None
    sig = signing_status(mbytes, sig_text)
    kind = {"valid": "ok", "present": "medium", "unsigned": "medium", "invalid": "failing"}[sig["state"]]
    parts.append("<h2>Registry signing</h2><div class=\"panel\">" + _pill(sig["state"], kind) +
                 f' {esc(sig["detail"])}' + (f'<div class="kv"><b>Key id</b><span>{esc(sig["key_id"])}</span><b>Signed at</b><span>{esc(sig["signed_at"])}</span></div>'
                                              if sig.get("key_id") or sig.get("signed_at") else "") + "</div>")
    # operator actions
    hist = []
    hp = Path(repo) / "components" / "automation-registry" / "history" / "CHANGELOG.md"
    if hp.is_file():
        rx = re.compile(r"^- (\d{4}-\d{2}-\d{2}) `([a-z-]+)` \*\*([^*]+)\*\* - (.*)$")
        for line in hp.read_text(encoding="utf-8", errors="replace").splitlines()[:5000]:
            m = rx.match(line)
            if m:
                hist.append([esc(m.group(1)), esc(m.group(2)), esc(_s(m.group(3), 100)), esc(_s(m.group(4), 400))])
    parts.append("<h2>Operator actions</h2><p class=\"sub\">From <code>history/CHANGELOG.md</code>; the pull request is the approval record. Newest first.</p>" +
                 _table(["Date", "Action", "Target", "Detail"], list(reversed(hist))[:200], "No operator actions recorded yet."))
    # pause / kill-switch state and events
    pauses = []
    fleet = manifest.get("fleet") or {}
    if fleet.get("paused"):
        pauses.append([_pill("FLEET PAUSED", "paused"), esc("fleet"), esc(_s(fleet.get("paused_reason"), 300)), "manifest"])
    for name, e in sorted((manifest.get("automations") or {}).items()):
        if isinstance(e, dict) and e.get("status") == "paused":
            pauses.append([_pill("paused", "paused"), esc(_s(name, 100)), esc(_s(e.get("paused_reason"), 300)), "manifest"])
    seen = 0
    for ev in reversed(getattr(data, "lifecycle", None) or []) if data is not None else []:
        if ev.get("event") == "paused_seen" and seen < 50:
            seen += 1
            pauses.append([_pill("kill-switch seen", "medium"), esc(_s(ev.get("automation") or ev.get("host"), 100)),
                           esc(_s((ev.get("detail") or {}).get("reason") if isinstance(ev.get("detail"), dict) else "", 300)),
                           esc(f'{_s(ev.get("host"), 60)} {_s(ev.get("ts"), 30)}')])
    parts.append("<h2>Pause / kill-switch</h2>" + _table(["State", "Scope", "Reason", "Source"], pauses, "Nothing is paused and no machine has reported a pause."))
    # releases
    rel = []
    for kind_name, key in (("component", "components"), ("automation", "automations")):
        for name, e in sorted((manifest.get(key) or {}).items()):
            e = e if isinstance(e, dict) else {}
            rings = e.get("rings") if isinstance(e.get("rings"), dict) else {}
            for ver, info in (e.get("versions") or {}).items() if isinstance(e.get("versions"), dict) else []:
                info = info if isinstance(info, dict) else {}
                on = [r for r in ("dev", "pilot", "broad") if str(rings.get(r)) == str(ver)]
                rel.append([esc(kind_name), esc(_s(name, 100)), esc(_s(ver, 40)), (esc(", ".join(on)) if on else "superseded"),
                            f'<span class="sha">{esc(_s(info.get("sha256"), 64))}</span>'])
    parts.append("<h2>Releases</h2><p class=\"sub\">Every published version with its SHA-256 and the rings that currently run it.</p>" +
                 _table(["Kind", "Name", "Version", "Rings running it", "SHA-256"], rel, "No releases in the manifest."))
    body = '<p class="sub">Who changed what, and what is running where. Operator actions and releases come from the git-governed registry; pause sightings come from (unauthenticated) client telemetry.</p>' + "".join(parts)
    return [_page("Audit", "audit", body, cfg, out, "audit/index.html")]


# -------------------------------------------------------------------------------------------------------------- entry point
def build_ops(out: Path, cfg: SiteConfig, manifest: dict, data, repo: Path, registry_dir: Path | None = None, now=None) -> list[str]:
    """Write jobs/, machines/, queues/, schedules/ and audit/. ``data`` is an ``analytics.Data`` or None (no archive)."""
    now = now or datetime.now(timezone.utc)
    out = Path(out)
    pages: list[str] = []
    pages += guarded(out, cfg, "jobs", "Jobs", build_jobs, data, now, active="jobs")
    pages += guarded(out, cfg, "machines", "Machines", build_machines, data, manifest, now, active="machines")
    pages += guarded(out, cfg, "queues", "Queues and approvals", build_queues, data, now, active="queues")
    pages += guarded(out, cfg, "schedules", "Schedules", build_schedules, manifest, now, active="schedules")
    pages += guarded(out, cfg, "audit", "Audit", build_audit, manifest, data, repo, registry_dir, now, active="audit")
    return pages
