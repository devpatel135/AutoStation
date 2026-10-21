"""Program / ROI dashboard: what the automation programme delivers, what it costs, and whether the fleet is healthy.

``compute(report, data, manifest, config, now)`` is a pure function; ``build(...)`` renders ``program/index.html`` and
``program.json``. Inputs: the analytics ``report`` (see analytics.compute), the loaded archive ``data``, the registry
``manifest`` (rings, classification, ``catalog.minutes_per_item``) and an optional ``program.yaml``::

    currency: USD
    hourly_cost: 55                     # fully-loaded cost of one hour of manual work
    uipath:
      license_cost_per_robot_per_year: 12000
      robots_retired: 4                 # robots/licences switched off because their automations moved to AutoStation
      orchestrator_cost_per_year: 30000 # 0 until the Orchestrator subscription is actually cancelled
    llm_cost_per_1k_calls: 0            # internal endpoint: usually 0; set it to track a chargeback rate
    migration: {total_in_scope: 0}      # optional; the migration dashboard has the full picture

Every number is derived from archived run logs and the manifest; log-derived strings are HTML-escaped on output.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import analytics
from .common import iso, miniyaml, read_json, utcnow, write_json
from .dashboard import CSS as DASH_CSS, esc, fmt_ms, pct, pill, spark

DEFAULTS = {
    "currency": "USD", "hourly_cost": 0.0,
    "uipath": {"license_cost_per_robot_per_year": 0.0, "robots_retired": 0, "orchestrator_cost_per_year": 0.0},
    "llm_cost_per_1k_calls": 0.0, "migration": {},
}


def load_config(path) -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if path and Path(path).is_file():
        doc = miniyaml.loads(Path(path).read_text(encoding="utf-8")) or {}
        for k, v in doc.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def _num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _ts(r: dict):
    return analytics._ts(r)


def compute(report: dict, data: analytics.Data, manifest: dict | None, config: dict | None = None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    cfg = load_config(None)
    for k, v in (config or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    manifest = manifest or {}
    autos = manifest.get("automations") or {}
    window_days = report.get("window_days", 30)
    runs = [r for r in data.runs if _ts(r) >= now - timedelta(days=window_days)]
    since30 = now - timedelta(days=30)

    # --- per automation: runs, success, items and hours saved ---
    per: dict[str, dict] = {}
    for name in sorted(set(autos) | {r["automation"] for r in runs}):
        mine = [r for r in runs if r["automation"] == name]
        counted = [r for r in mine if r["status"] not in ("halted_paused", "skipped_maintenance")]
        ok = sum(1 for r in counted if r["status"] in ("success", "partial"))
        entry = autos.get(name) or {}
        mpi = ((entry.get("catalog") or {}).get("minutes_per_item"))
        items_ok_30 = sum((r.get("items") or {}).get("success", 0) for r in mine if _ts(r) >= since30)
        items_all = sum((r.get("items") or {}).get("total", 0) for r in mine)
        per[name] = {
            "runs": len(counted), "success_rate": round(ok / len(counted), 4) if counted else None,
            "items": items_all, "items_success_30d": items_ok_30, "minutes_per_item": mpi,
            "hours_saved_30d": round(items_ok_30 * _num(mpi) / 60.0, 1) if mpi else None,
            "ring_versions": entry.get("rings"), "classification": entry.get("data_classification"),
            "status": entry.get("status", "unregistered" if not entry else "active"), "owner": entry.get("owner"),
        }
    hours_30 = round(sum(p["hours_saved_30d"] or 0 for p in per.values()), 1)
    estimated = [n for n, p in per.items() if p["hours_saved_30d"] is None and p["items_success_30d"]]

    # --- money ---
    ui = cfg.get("uipath") or {}
    licence_year = _num(ui.get("license_cost_per_robot_per_year")) * _num(ui.get("robots_retired")) + _num(ui.get("orchestrator_cost_per_year"))
    labour_year = hours_30 * 12 * _num(cfg.get("hourly_cost"))
    llm_calls = 0
    prompt_chars = 0
    for r in runs:
        for e in r.get("events", []):
            if e.get("event") == "llm_call":
                llm_calls += 1
                prompt_chars += int((e.get("data") or {}).get("prompt_chars") or 0)
    money = {"currency": cfg.get("currency", "USD"), "licence_avoided_per_year": round(licence_year, 2),
             "labour_value_per_year": round(labour_year, 2), "llm_cost_30d": round(llm_calls / 1000.0 * _num(cfg.get("llm_cost_per_1k_calls")), 2),
             "net_value_per_year": round(licence_year + labour_year, 2), "configured": bool(licence_year or labour_year)}

    # --- registry shape ---
    by_ring: Counter = Counter()
    by_class: Counter = Counter()
    for name, e in autos.items():
        e = e or {}
        for ring, v in (e.get("rings") or {}).items():
            if v:
                by_ring[ring] += 1
        by_class[e.get("data_classification") or "unclassified"] += 1
    paused = sorted(n for n, e in autos.items() if (e or {}).get("status") == "paused")

    # --- fleet versions / drift ---
    hosts = (report.get("versions") or {}).get("hosts") or []
    drift = []
    for h in hosts:
        bad = [f"{k} {v['version']} (target {v['target']})" for k, v in h["components"].items() if not v["ok"]]
        bad += [f"{k} {v['version']} (target {v['target']})" for k, v in h["automations"].items() if not v["ok"]]
        if bad:
            drift.append({"host": h["host"], "ring": h["ring"], "issues": bad})

    # --- failure causes (run level + item level) ---
    causes: dict[tuple, dict] = {}
    for r in runs:
        if r["status"] in ("failed", "self_heal_failed", "hung"):
            err = r.get("error") or {}
            cat = err.get("category") or "UNKNOWN"
            key = (cat, analytics.error_fingerprint(cat, err.get("message")))
            c = causes.setdefault(key, {"category": cat, "message": (err.get("message") or "")[:160], "runs": 0, "automations": set(), "hosts": set()})
            c["runs"] += 1
            c["automations"].add(r["automation"])
            c["hosts"].add(r.get("host"))
    top = sorted(causes.values(), key=lambda c: -c["runs"])[:8]
    failure_causes = [{"category": c["category"], "message": c["message"], "runs": c["runs"], "automations": sorted(c["automations"]),
                       "hosts": len(c["hosts"])} for c in top]
    failed_runs = [r for r in runs if r["status"] in ("failed", "self_heal_failed", "hung")]
    with_evidence = {e.get("run_id") for e in (getattr(data, "evidence", None) or [])}
    evidence = {"failed_runs": len(failed_runs), "with_evidence": sum(1 for r in failed_runs if r["run_id"] in with_evidence),
                "files": sum(len(e.get("files") or []) for e in (getattr(data, "evidence", None) or []))}

    items = report.get("items") or {}
    retries = report.get("retries") or {}
    fleet = report.get("fleet") or {}
    program = {
        "schema_version": 1, "generated_at": iso(now), "window_days": window_days,
        "headline": {"automations": len(autos), "active": len(autos) - len(paused), "runs": fleet.get("total", len(runs)),
                     "success_rate": fleet.get("success_rate"), "items_processed": items.get("total", 0),
                     "items_30d": (report.get("impact") or {}).get("items_30d"), "hours_saved_30d": hours_30,
                     "hosts": fleet.get("hosts"), "open_signals": fleet.get("signals_total")},
        "per_automation": per, "hours_saved_estimated_for_all": not estimated, "automations_without_minutes_per_item": estimated,
        "money": money, "by_ring": dict(by_ring), "by_classification": dict(by_class), "paused": paused,
        "versions": {"hosts": len(hosts), "drifted": drift},
        "failure_causes": failure_causes, "item_failure_categories": items.get("failure_categories") or {},
        "retries": {"events": retries.get("total_events", 0), "run_rate": retries.get("retry_run_rate"),
                    "by_category": {k: {"events": v["events"], "recovery_rate": v["recovery_rate"]} for k, v in (retries.get("by_category") or {}).items()}},
        "llm": {"calls": llm_calls, "approx_tokens_in": prompt_chars // 4, "error_rate": (report.get("llm") or {}).get("error_rate"),
                "synopsis_from_llm": (report.get("llm") or {}).get("synopsis_from_llm"), "cost_30d": money["llm_cost_30d"]},
        "evidence": evidence, "trend": report.get("trend") or [], "migration": cfg.get("migration") or {},
    }
    return program


def _money(amount: float, cur: str) -> str:
    return f"{cur} {amount:,.0f}"


def render(p: dict) -> str:
    h = p["headline"]
    m = p["money"]
    A: list[str] = []
    ap = A.append
    ap('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
       "<title>AUTOSTATION Program dashboard</title>"
       '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=IBM+Plex+Mono:wght@400;700&display=swap">'
       f"<style>{DASH_CSS}</style></head><body><div class=\"page\">")
    ap(f'<header><div><div class="eyebrow">AUTOSTATION &middot; Program</div><h1>Automation programme: value &amp; health</h1>'
       f'<div class="sub">Generated {esc(p["generated_at"])} &middot; last {esc(p["window_days"])} days of run logs</div></div>'
       '<button class="theme" type="button" onclick="var r=document.documentElement;r.dataset.theme=(r.dataset.theme===\'light\'?\'dark\':\'light\')">toggle theme</button></header>')

    cards = [("Automations", h["automations"], f"{h['active']} active, {len(p['paused'])} paused"),
             ("Runs", h["runs"], f"success rate {pct(h['success_rate'])}"),
             ("Items processed", h["items_processed"], f"{h['items_30d'] if h['items_30d'] is not None else '-'} in last 30 days"),
             ("Hours saved (30d)", h["hours_saved_30d"], "items x minutes_per_item" + ("" if p["hours_saved_estimated_for_all"] else " (some automations have no estimate)")),
             ("Hosts", h["hosts"] if h["hosts"] is not None else "-", f"{p['versions']['hosts']} reporting, {len(p['versions']['drifted'])} drifted"),
             ("Open signals", h["open_signals"] if h["open_signals"] is not None else "-", "see the fleet dashboard")]
    ap('<h2>Headline</h2><div class="cards">' + "".join(
        f'<div class="card"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div><div class="s">{esc(s)}</div></div>' for k, v, s in cards) + "</div>")

    ap("<h2>Value</h2>")
    if m["configured"]:
        cur = m["currency"]
        ap('<div class="cards">'
           f'<div class="card"><div class="k">UiPath licences avoided / yr</div><div class="v">{esc(_money(m["licence_avoided_per_year"], cur))}</div><div class="s">from program.yaml (robots retired + Orchestrator)</div></div>'
           f'<div class="card"><div class="k">Labour value / yr</div><div class="v">{esc(_money(m["labour_value_per_year"], cur))}</div><div class="s">hours saved x 12 x hourly cost</div></div>'
           f'<div class="card"><div class="k">Net programme value / yr</div><div class="v">{esc(_money(m["net_value_per_year"], cur))}</div><div class="s">licences + labour (LLM cost 30d: {esc(_money(m["llm_cost_30d"], cur))})</div></div></div>')
    else:
        ap('<div class="ok-note">Cost inputs are not configured. Add <code>program.yaml</code> (hourly_cost, uipath.license_cost_per_robot_per_year, '
           'uipath.robots_retired, uipath.orchestrator_cost_per_year) next to the analytics config to turn hours saved and retired licences into money.</div>')

    ap('<h2>Automations</h2><div class="tw"><table><tr><th>Automation</th><th>Status</th><th>Owner</th><th>Rings</th><th>Class.</th><th>Runs</th><th>Success</th>'
       "<th>Items</th><th>Min / item</th><th>Hours saved (30d)</th></tr>")
    for name, a in sorted(p["per_automation"].items()):
        rings = ", ".join(f"{k} {v}" for k, v in (a.get("ring_versions") or {}).items() if v) or "-"
        ap(f"<tr><td><code>{esc(name)}</code></td><td>{pill(a['status'], 'paused' if a['status'] == 'paused' else 'ok')}</td><td>{esc(a.get('owner') or '-')}</td>"
           f"<td>{esc(rings)}</td><td>{esc(a.get('classification') or '-')}</td><td>{a['runs']}</td><td>{pct(a['success_rate'])}</td><td>{a['items']}</td>"
           f"<td>{esc(a['minutes_per_item'] if a['minutes_per_item'] is not None else '-')}</td><td>{esc(a['hours_saved_30d'] if a['hours_saved_30d'] is not None else '-')}</td></tr>")
    ap("</table></div>")

    trend = p["trend"]
    if trend:
        ap('<h2>Trend</h2><div class="cards">'
           f'<div class="card"><div class="k">Runs / day</div>{spark([d["runs"] for d in trend], 220, 40)}</div>'
           f'<div class="card"><div class="k">Failed / day</div>{spark([d["failed"] for d in trend], 220, 40, "bad")}</div>'
           f'<div class="card"><div class="k">Items / day</div>{spark([d["items"] for d in trend], 220, 40)}</div></div>')

    ap('<h2>Portfolio</h2><div class="cards">'
       f'<div class="card"><div class="k">By ring</div><div class="s">{esc(", ".join(f"{k}: {v}" for k, v in sorted(p["by_ring"].items())) or "-")}</div></div>'
       f'<div class="card"><div class="k">By classification</div><div class="s">{esc(", ".join(f"{k}: {v}" for k, v in sorted(p["by_classification"].items())) or "-")}</div></div></div>')

    ap('<h2>Fleet versions &amp; drift</h2>')
    if p["versions"]["drifted"]:
        ap('<div class="tw"><table><tr><th>Host</th><th>Ring</th><th>Out of date</th></tr>'
           + "".join(f"<tr><td>{esc(d['host'])}</td><td>{esc(d['ring'])}</td><td>{esc('; '.join(d['issues']))}</td></tr>" for d in p["versions"]["drifted"]) + "</table></div>")
    else:
        ap(f'<div class="ok-note">{p["versions"]["hosts"]} host(s) reporting; none are behind their ring target.</div>')

    ap("<h2>Top failure causes</h2>")
    if p["failure_causes"]:
        ap('<div class="tw"><table><tr><th>Category</th><th>Message</th><th>Runs</th><th>Automations</th><th>Hosts</th></tr>'
           + "".join(f"<tr><td><code>{esc(c['category'])}</code></td><td>{esc(c['message'])}</td><td>{c['runs']}</td><td>{esc(', '.join(c['automations']))}</td><td>{c['hosts']}</td></tr>"
                     for c in p["failure_causes"]) + "</table></div>")
    else:
        ap('<div class="ok-note">No failed runs in the window.</div>')
    ev = p["evidence"]
    ap(f'<p class="sub">Failed runs with captured evidence (screenshots / DOM snapshots): {ev["with_evidence"]} of {ev["failed_runs"]} ({ev["files"]} file(s)). '
       "Item-level failure categories: " + esc(", ".join(f"{k}: {v}" for k, v in p["item_failure_categories"].items()) or "none") + ".</p>")

    rt = p["retries"]
    ap(f'<h2>Retries &amp; LLM</h2><div class="cards"><div class="card"><div class="k">Retry events</div><div class="v">{rt["events"]}</div><div class="s">in {pct(rt["run_rate"])} of runs</div></div>'
       f'<div class="card"><div class="k">LLM calls</div><div class="v">{p["llm"]["calls"]}</div><div class="s">~{p["llm"]["approx_tokens_in"]:,} input tokens, error rate {pct(p["llm"]["error_rate"])}</div></div>'
       f'<div class="card"><div class="k">LLM synopses</div><div class="v">{esc(p["llm"]["synopsis_from_llm"] if p["llm"]["synopsis_from_llm"] is not None else "-")}</div><div class="s">rest use the deterministic fallback</div></div></div>')
    if rt["by_category"]:
        ap('<div class="tw"><table><tr><th>Retry category</th><th>Events</th><th>Recovery rate</th></tr>'
           + "".join(f"<tr><td><code>{esc(k)}</code></td><td>{v['events']}</td><td>{pct(v['recovery_rate'])}</td></tr>" for k, v in rt["by_category"].items()) + "</table></div>")
    ap("</div></body></html>")
    return "".join(A)


def build(report: dict, data: analytics.Data, manifest: dict | None, out_dir, config: dict | None = None, now: datetime | None = None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    p = compute(report, data, manifest, config, now)
    (out / "index.html").write_text(render(p), encoding="utf-8")
    write_json(out / "program.json", p)
    return {"out": str(out), "files": ["index.html", "program.json"], "program": p["headline"]}
