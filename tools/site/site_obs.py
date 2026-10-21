"""Observability pages and exports: Reliability (SLO), Errors & remediation, metrics (json / Prometheus), fleet status, badges, search index.

    build_obs(out, cfg, manifest, data, now) -> [relative paths written]

``data`` is an ``analytics.Data`` or None (no archive yet -> honest waiting pages, an "unknown" fleet status and empty metrics, never sample
numbers). Every number comes from ``automation_logging.reliability`` (pure functions, unit-tested with hand-computed cases); this module
only lays it out. All telemetry-derived text is clamped (``reliability.clean``) and HTML-escaped; the search page script renders with
textContent only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import site_ops as so
from site_common import SiteConfig, esc, guarded, write

MAX_SEARCH = 600           # entries in search-ops.json
MAX_AUTOMATIONS = 200      # cardinality bound for metrics and badges
MAX_TELEMETRY_ONLY = 50       # automations that appear only in telemetry (not in the manifest) shown after the registered ones
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
GSEARCH_JS = Path(__file__).with_name("gsearch.js")
BADGE_COLORS = {"passing": "#2ea043", "degraded": "#bf8700", "failing": "#d1242f", "paused": "#8250df", "no data": "#6e7781"}


def _libs():
    from automation_logging import analytics, reliability  # noqa: PLC0415 (path is set up by build_site)
    return analytics, reliability


def _cfg() -> dict:
    analytics, reliability = _libs()
    return dict(analytics.DEFAULTS, **reliability.DEFAULTS)


def _spark(values, w: int = 120, h: int = 28, kind: str = "ok") -> str:
    """Inline SVG polyline; None / non-numbers are gaps; a flat or empty series draws a baseline."""
    vals = [v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None for v in values]
    pts = [(i, v) for i, v in enumerate(vals) if v is not None]
    color = {"ok": "var(--accent)", "bad": "var(--red)"}.get(kind, "var(--accent)")
    if len(pts) < 2:
        return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="not enough data"><line x1="0" y1="{h - 2}" x2="{w}" y2="{h - 2}" style="stroke:var(--border)"/></svg>'
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1.0
    n = max(len(vals) - 1, 1)
    xy = " ".join(f"{i / n * (w - 4) + 2:.1f},{h - 3 - (v - lo) / span * (h - 6):.1f}" for i, v in pts)
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="trend, min {lo:g}, max {hi:g}">'
            f'<polyline points="{xy}" style="fill:none;stroke:{color};stroke-width:1.6"/></svg>')


def _pct(x) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def _secs(s) -> str:
    if s is None:
        return "-"
    s = float(s)
    return f"{s / 3600:.1f} h" if s >= 3600 else (f"{s / 60:.0f} min" if s >= 60 else f"{s:.0f} s")


def _jobs_link(prefix: str, run_id) -> str:
    rid = so._id(run_id)
    return f'<a href="../jobs/index.html#run={esc(rid)}">{esc(rid[:14])}{"..." if len(rid) > 14 else ""}</a>' if rid else "-"


def _tile(n, label, color: str = "") -> str:
    style = f' style="color:var(--{color})"' if color else ""
    return f'<div class="tile"><div class="n"{style}>{esc(n)}</div><div class="l">{esc(label)}</div></div>'


# ---------------------------------------------------------------------------------------------------------- shared compute
def compute_all(manifest: dict, data, now: datetime) -> dict:
    """Everything the pages / exports need, computed once."""
    analytics, rel = _libs()
    cfg = _cfg()
    autos = (manifest or {}).get("automations") or {}
    runs = list(getattr(data, "runs", None) or [])
    window_start = now - timedelta(days=cfg["window_days"])
    runs = [r for r in runs if analytics._ts(r) >= window_start]
    # registered automations first: junk names invented by forged telemetry can neither evict a real automation nor crowd the pages
    registered = sorted(n for n in autos if NAME_RE.match(str(n)))
    telemetry_only = sorted({r["automation"] for r in runs if NAME_RE.match(str(r.get("automation"))) and r["automation"] not in autos})
    names = (registered + telemetry_only[:MAX_TELEMETRY_ONLY])[:MAX_AUTOMATIONS]
    per = {}
    for name in names:
        entry = autos.get(name) or {}
        start = now - timedelta(days=min(cfg["slo_window_days"], 30))
        expected = None
        grace = None
        if entry:
            try:
                expected = [(s, t) for s, t in analytics.expected_runs(entry, start, now) if True]
                grace = analytics._grace(entry, cfg["min_grace_min"])
            except Exception:  # noqa: BLE001 (a malformed schedule must not take the page down)
                expected, grace = None, None
        per[name] = rel.automation_reliability(name, entry, runs, now, cfg, expected, grace)
    signals = []
    if data is not None and getattr(data, "runs", None):
        try:
            signals, _ = analytics.compute_signals(data, manifest or {}, now, cfg, {})
        except Exception:  # noqa: BLE001
            signals = []
    fleet = rel.fleet_reliability(per, runs, now)
    return {"cfg": cfg, "autos": autos, "runs": runs, "names": names, "per": per, "signals": signals, "fleet": fleet,
            "taxonomy": rel.error_taxonomy(runs, now), "regressions": rel.regressions(runs, cfg["regression_window_h"]),
            "machines": rel.machine_correlation(runs, now, cfg["lookback_h"], cfg["machine_min_failures"], cfg["machine_fail_rate"]),
            "remediation": rel.remediation_stats(runs)}


def automation_state(name: str, c: dict) -> str:
    """passing / degraded / failing / paused / no data (drives badges and fleet-status)."""
    entry = c["autos"].get(name) or {}
    if entry.get("status") == "paused":
        return "paused"
    mine = [r for r in c["runs"] if r["automation"] == name]
    if not mine:
        return "no data"
    sevs = {s["severity"] for s in c["signals"] if name in (s.get("automations") or [s.get("automation")])}
    p = c["per"].get(name) or {}
    rem = ((p.get("budget") or {}).get("remaining"))
    if sevs & {"critical", "high"} or (rem is not None and rem < 0):
        return "failing"
    if sevs or (rem is not None and rem < 0.25):
        return "degraded"
    return "passing"


# ------------------------------------------------------------------------------------------------------------ reliability
def build_reliability(out: Path, cfg: SiteConfig, c: dict, data, now: datetime) -> list[str]:
    if data is None or not c["runs"]:
        return [so._page("Reliability", "reliability", so._waiting("Reliability and SLO tracking"), cfg, out, "reliability/index.html")]
    per, fleet = c["per"], c["fleet"]
    meeting = sum(1 for p in per.values() if p["budget"]["remaining"] is None or p["budget"]["remaining"] >= 0)
    tiles = ('<div class="tiles">' + _tile(_pct(fleet["success_rate"]), "Fleet success rate") + _tile(f"{meeting}/{len(per)}", "Automations within error budget")
             + _tile(len(fleet["budget_exhausted"]), "Budgets exhausted", "red" if fleet["budget_exhausted"] else "")
             + _tile(fleet["open_episodes"], "Open failure episodes", "orange" if fleet["open_episodes"] else "")
             + _tile(_secs(fleet["mttr_s"]), "MTTR (mean time to recover)") + _tile(_secs(fleet["mtbf_s"]), "MTBF (mean time between failures)")
             + _tile(len(fleet["flaky"]), "Flaky automations", "yellow" if fleet["flaky"] else "") + "</div>")
    rows = []
    for name in sorted(per):
        p = per[name]
        b, ep, st, fl, du, ot = p["budget"], p["episodes"], p["streaks"], p["flakiness"], p["duration"], p["on_time"]
        state = automation_state(name, c)
        kind = {"passing": "ok", "degraded": "medium", "failing": "failing", "paused": "paused"}.get(state, "unknown")
        rem = b["remaining"]
        rem_cell = "-" if rem is None else _pill_budget(rem)
        p95s = [t["p95"] for t in du["trend"]]
        rows.append([esc(name), so._pill(state, kind), f"{p['slo']['target'] * 100:.2f}% / {p['slo']['window_days']} d",
                     f"{_pct(b['success_rate'])} ({b['failed']}/{b['runs']})", rem_cell,
                     esc("-" if p["burn_fast"] is None else f"{p['burn_fast']:.1f}x") + " / " + esc("-" if p["burn_slow"] is None else f"{p['burn_slow']:.1f}x"),
                     esc(_secs(ep["mttr_s"])), esc(_secs(ep["mtbf_s"])), f"{st['current_failures']} / {st['max_failures']}",
                     so._pill("flaky", "medium") + f" {fl['flips']} flips" if fl["flaky"] else esc("-" if fl["score"] is None else f"score {fl['score']}"),
                     esc(so._dur(du["p50"])), esc(so._dur(du["p95"])), esc(so._dur(du["p99"])),
                     esc("-" if not ot or ot["rate"] is None else f"{ot['rate'] * 100:.0f}% ({ot['on_time']}/{ot['expected']})"),
                     _spark(p95s, kind="ok")])
    table = so._table(["Automation", "State", "SLO / window", "Success (window)", "Budget left", "Burn 24h / 7d", "MTTR", "MTBF", "Fail streak now / max",
                       "Flakiness", "p50", "p95", "p99", "On time", "p95 trend (14 d)"], rows, "No automations yet.")
    an_rows = []
    for name in sorted(per):
        for a in per[name]["duration"]["anomalies"]:
            an_rows.append([esc(name), esc(a["started_at"]), so._pill(a["direction"], "medium"), esc(so._dur(a["duration_ms"])),
                            esc(so._dur(a["baseline_ms"])), esc(str(a["z"])), _jobs_link("", a["run_id"])])
    an_rows = an_rows[-40:]
    defs = ("<h2>How these are computed</h2><ul class=\"sub\">"
            "<li><b>SLO</b>: share of counted runs that must be ok (default 95% over 30 days; override per automation with <code>catalog.slo: {target, window_days}</code> in the manifest). "
            "Paused and maintenance-skipped runs are not counted.</li>"
            "<li><b>Error budget</b>: <code>(1 - SLO) x runs</code> failures are allowed; <i>budget left</i> = 1 - failures / allowed (negative = exhausted).</li>"
            "<li><b>Burn rate</b>: failure rate in the window divided by <code>(1 - SLO)</code>; 1x spends the budget exactly on schedule. "
            "Alerts: &ge;10x over 24 h, or &ge;2x over 7 d with &lt;50% budget left (at least 3 runs and 2 failures).</li>"
            "<li><b>MTTR / MTBF</b>: failure episodes per machine (first failure after an ok run, until the next ok run).</li>"
            "<li><b>Flakiness</b>: ok/failed flips in the last 20 runs; flaky = at least 4 flips and a flip score &ge; 0.4.</li>"
            "<li><b>Duration anomaly</b>: robust (median/MAD) z-score &ge; 3.5 against the previous 20 successful runs and at least 1.5x the median.</li>"
            "<li><b>On time</b>: expected scheduled runs that started within their grace period.</li></ul>")
    body = (tiles + '<p class="sub">Per automation over the SLO window. Data is advisory (unauthenticated client telemetry).</p>' + table
            + "<h2>Recent duration anomalies</h2>" + so._table(["Automation", "Started", "Direction", "Duration", "Typical", "Robust z", "Run"], an_rows,
                                                               "No duration anomalies.") + defs)
    return [so._page("Reliability", "reliability", body, cfg, out, "reliability/index.html")]


def _pill_budget(rem: float) -> str:
    pct = f"{rem * 100:.0f}%"
    return so._pill(pct, "failing" if rem < 0 else ("medium" if rem < 0.25 else "ok"))


# --------------------------------------------------------------------------------------------------------------- errors
RUNBOOK_LINK = '<a href="../docs/RUNBOOKS.html">runbook</a>'


def build_errors(out: Path, cfg: SiteConfig, c: dict, data, now: datetime) -> list[str]:
    analytics, rel = _libs()
    if data is None or not c["runs"]:
        return [so._page("Errors & remediation", "errors", so._waiting("Error analysis and remediation outcomes"), cfg, out, "errors/index.html")]
    tx, rg, mc, rm = c["taxonomy"], c["regressions"], c["machines"], c["remediation"]
    active_reg = [r for r in rg if r["last_seen"] >= so._iso(now - timedelta(hours=c["cfg"]["lookback_h"]))]
    unhealthy = [h for h in mc["hosts"] if h["unhealthy"]]
    tried = sum(p["fixed"] + p["not_fixed"] for p in rm["playbooks"])
    fixed = sum(p["fixed"] for p in rm["playbooks"])
    tiles = ('<div class="tiles">' + _tile(tx["failures"], "Failed runs (window)", "red" if tx["failures"] else "") + _tile(tx["signature_total"], "Distinct error signatures")
             + _tile(len(active_reg), "Active regressions", "orange" if active_reg else "") + _tile(len(unhealthy), "Unhealthy machines", "orange" if unhealthy else "")
             + _tile(_pct(fixed / tried) if tried else "-", "Remediation fix rate") + _tile(len(rm["needs_human"]), "Categories needing a person",
                                                                                               "yellow" if rm["needs_human"] else "") + "</div>")
    # needs attention
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    att = sorted(c["signals"], key=lambda s: (rank.get(s["severity"], 9), s["first_seen"]))[:12]
    att_rows = []
    for s in att:
        who = so._s(s.get("automation") or ", ".join(s.get("automations") or []) or s.get("host") or "fleet", 120)
        ev = (s.get("evidence") or [None])[-1]
        att_rows.append([so._pill(so._s(s["severity"], 20), {"critical": "failing", "high": "high", "medium": "medium"}.get(s["severity"], "unknown")),
                         f"<code>{esc(so._s(s['type'], 60))}</code>", esc(who), esc(so._s(s.get("detail"), 260)), esc(so._s(s.get("first_seen"), 30)),
                         _jobs_link("", ev) if ev else "-"])
    attention = "<h2>Needs attention</h2>" + so._table(["Severity", "Signal", "Where", "What", "Since", "Latest run"], att_rows, "Nothing needs attention.")
    # categories
    cat_rows = []
    for cat, n in tx["categories"].items():
        cat_rows.append([f"<code>{esc(cat)}</code>", str(n), _spark(tx["category_trend"].get(cat, []), kind="bad"),
                         esc(rel_runbook(cat)), RUNBOOK_LINK])
    cats = "<h2>Error categories</h2>" + so._table(["Category", "Failed runs", "Trend (14 d)", "First move", "Docs"], cat_rows, "No failures in the window.")
    # signatures
    sig_rows = []
    for s in tx["signatures"]:
        sig_rows.append([f'<a id="sig-{esc(s["signature"])}"></a><code>{esc(s["signature"])}</code>', f"<code>{esc(s['category'])}</code>", esc(s["sample"][:200]),
                         str(s["count"]), esc(s["first_seen"]), esc(s["last_seen"]), esc(", ".join(s["automations"][:3])),
                         esc(f"{len(s['hosts'])} machine(s)"), esc(", ".join(s["versions"][:3])), _spark(s["trend"], 90, 22, "bad"),
                         _jobs_link("", s["run_ids"][-1] if s["run_ids"] else "")])
    sigs = ("<h2>Top error signatures</h2><p class=\"sub\">A signature groups the same error with paths, ids, urls and numbers normalised away. "
            f"Showing {len(tx['signatures'])} of {tx['signature_total']}.</p>"
            + so._table(["Signature", "Category", "Example", "Runs", "First seen", "Last seen", "Automations", "Machines", "Versions", "Trend", "Latest run"],
                        sig_rows, "No error signatures."))
    reg_rows = [[esc(r["automation"]), f"<code>{esc(r['signature'])}</code>", f"<code>{esc(r['category'])}</code>", esc(r["sample"][:160]),
                 esc(f"{r['previous_version']} -> {r['version']}"), esc(r["first_seen"]), str(r["count"]), _jobs_link("", r["run_ids"][-1])] for r in rg[-30:]]
    regs = "<h2>Regressions after a release</h2>" + so._table(["Automation", "Signature", "Category", "Example", "Version", "First seen", "Runs", "Latest run"], reg_rows,
                                                               "No new error appeared right after a release.")
    host_rows = [[esc(h["host"]), str(h["runs"]), str(h["failed"]), _pct(h["fail_rate"]), esc(_pct(h["other_hosts_fail_rate"])),
                  esc(", ".join(h["automations_failing"][:4]) or "-"),
                  so._pill("machine problem", "failing") if h["unhealthy"] else so._pill("ok", "ok")] for h in mc["hosts"] if h["failed"] or h["unhealthy"]]
    ver_rows = [[esc(v["automation"]), esc(v["version"]), str(v["runs"]), str(v["failed"]), _pct(v["fail_rate"])]
                for v in sorted(mc["versions"], key=lambda v: -(v["fail_rate"] or 0)) if v["failed"]][:20]
    corr = ("<h2>Where failures concentrate</h2><p class=\"sub\">Last " + str(c["cfg"]["lookback_h"]) + " h. A machine that fails across automations while others are healthy "
            "is a machine problem (profile, VPN, disk, session), not a code problem.</p>"
            + so._table(["Machine", "Runs", "Failed", "Fail rate", "Other machines", "Automations failing", "Verdict"], host_rows, "No failures on any machine.")
            + "<h3>By automation version</h3>" + so._table(["Automation", "Version", "Runs", "Failed", "Fail rate"], ver_rows, "No failing version."))
    # remediation
    pb_rows = [[esc(p["playbook"]), str(p["attempts"]), str(p["fixed"]), str(p["not_fixed"]), str(p["skipped"]), str(p["escalated"]), str(p["recommended"]),
                _pct(p["fix_rate"]), esc(", ".join(p["categories"]))] for p in rm["playbooks"]]
    nh_rows = [[f"<code>{esc(n['category'])}</code>", esc(n["automation"]), str(n["runs"]), esc(n["last_seen"]), esc(n["runbook"]), _jobs_link("", n["run_id"])]
               for n in rm["needs_human"]]
    rec_rows = [[esc(r["automation"]), esc(r["playbook"]), f"<code>{esc(r['category'])}</code>", esc(r["detail"]), esc(r["ts"]), _jobs_link("", r["run_id"])]
                for r in rm["recommended"]]
    none_yet = not (rm["playbooks"] or rm["needs_human"] or rm["recommended"])
    remed = ("<h2>Proactive remediation</h2>"
             + ('<p class="sub">No remediation events reported yet. When an automation self-remediates (clears a stuck browser, refreshes a session, ...) the attempts '
                "appear here with the playbook, the outcome and whether it fixed the failure.</p>" if none_yet else "")
             + "<h3>Playbooks</h3>" + so._table(["Playbook", "Attempts", "Fixed", "Not fixed", "Skipped", "Escalated", "Recommended", "Fix rate", "Categories"], pb_rows,
                                                 "No playbook attempts.")
             + f"<h3>Unfixed - needs a person ({fixed} run(s) fixed automatically, {rm['unfixed_runs']} not)</h3>"
             + so._table(["Category", "Latest automation", "Runs", "Last seen", "First move", "Run"], nh_rows, "Nothing is waiting for a person.")
             + "<h3>Recommended actions</h3>" + so._table(["Automation", "Playbook", "Category", "Recommended action", "When", "Run"], rec_rows,
                                                           "No recommendations."))
    body = tiles + attention + cats + sigs + regs + corr + remed
    return [so._page("Errors & remediation", "errors", body, cfg, out, "errors/index.html")]


def rel_runbook(cat: str) -> str:
    _, rel = _libs()
    return rel.CATEGORY_RUNBOOK.get(cat, rel.CATEGORY_RUNBOOK["UNKNOWN"])


# --------------------------------------------------------------------------------------------------------------- metrics
def _label(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metrics_dict(c: dict, cfg: SiteConfig, now: datetime) -> dict:
    per = c["per"]
    sig_counts: dict = {}
    for s in c["signals"]:
        k = (s["type"], s["severity"])
        sig_counts[k] = sig_counts.get(k, 0) + 1
    autos = {}
    for name in sorted(per):
        p = per[name]
        autos[name] = {"state": automation_state(name, c), "runs": p["budget"]["runs"], "failed": p["budget"]["failed"], "success_ratio": p["budget"]["success_rate"],
                       "slo_target": p["slo"]["target"], "slo_window_days": p["slo"]["window_days"], "error_budget_remaining": p["budget"]["remaining"],
                       "burn_rate_24h": p["burn_fast"], "burn_rate_7d": p["burn_slow"], "mttr_seconds": p["episodes"]["mttr_s"], "mtbf_seconds": p["episodes"]["mtbf_s"],
                       "failure_streak": p["streaks"]["current_failures"], "flaky": 1 if p["flakiness"]["flaky"] else 0,
                       "duration_ms": {"p50": p["duration"]["p50"], "p95": p["duration"]["p95"], "p99": p["duration"]["p99"]},
                       "on_time_ratio": (p["on_time"] or {}).get("rate")}
    by_ring: dict = {}
    for r in c["runs"]:
        if r.get("status") in ("halted_paused", "skipped_maintenance"):
            continue
        ring = r.get("ring") if r.get("ring") in ("dev", "pilot", "broad") else "unknown"
        d = by_ring.setdefault(ring, {"runs": 0, "failed": 0})
        d["runs"] += 1
        d["failed"] += 1 if r.get("status") in ("failed", "self_heal_failed", "hung") else 0
    hosts = {r.get("host") for r in c["runs"] if r.get("host")}
    return {"schema_version": 1, "generated_at": so._iso(now), "commit": cfg.commit or "", "window_days": c["cfg"]["window_days"],
            "fleet": {"runs": c["fleet"]["runs"], "failed": c["fleet"]["failed"], "success_ratio": c["fleet"]["success_rate"], "mttr_seconds": c["fleet"]["mttr_s"],
                      "mtbf_seconds": c["fleet"]["mtbf_s"], "open_failure_episodes": c["fleet"]["open_episodes"], "machines": len(hosts),
                      "error_signatures": c["taxonomy"]["signature_total"], "regressions": len(c["regressions"])},
            "automations": autos, "rings": {k: by_ring[k] for k in sorted(by_ring)},
            "signals": [{"type": t, "severity": s, "count": n} for (t, s), n in sorted(sig_counts.items())],
            "error_categories": c["taxonomy"]["categories"]}


def metrics_prom(m: dict) -> str:
    out: list[str] = []

    def metric(name, help_, typ, samples):
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {typ}")
        for labels, val in samples:
            if val is None:
                continue
            lab = ("{" + ",".join(f'{k}="{_label(v)}"' for k, v in labels) + "}") if labels else ""
            out.append(f"{name}{lab} {val:.6g}" if isinstance(val, float) else f"{name}{lab} {val}")

    A = m["automations"]
    metric("autostation_build_info", "Site build identity (value is always 1).", "gauge", [([("commit", m["commit"][:12] or "unknown")], 1)])
    metric("autostation_fleet_runs", "Counted runs in the window.", "gauge", [([], m["fleet"]["runs"])])
    metric("autostation_fleet_failed_runs", "Failed runs in the window.", "gauge", [([], m["fleet"]["failed"])])
    metric("autostation_fleet_success_ratio", "Share of counted runs that were ok.", "gauge", [([], m["fleet"]["success_ratio"])])
    metric("autostation_fleet_mttr_seconds", "Mean time to recover from a failure episode.", "gauge", [([], m["fleet"]["mttr_seconds"])])
    metric("autostation_fleet_mtbf_seconds", "Mean time between failure episodes.", "gauge", [([], m["fleet"]["mtbf_seconds"])])
    metric("autostation_machines", "Machines that reported a run in the window.", "gauge", [([], m["fleet"]["machines"])])
    metric("autostation_automation_runs", "Counted runs per automation in the window.", "gauge", [([("automation", n)], a["runs"]) for n, a in A.items()])
    metric("autostation_automation_failed_runs", "Failed runs per automation in the window.", "gauge", [([("automation", n)], a["failed"]) for n, a in A.items()])
    metric("autostation_automation_success_ratio", "Success ratio per automation.", "gauge", [([("automation", n)], a["success_ratio"]) for n, a in A.items()])
    metric("autostation_slo_target", "Configured SLO target per automation.", "gauge", [([("automation", n)], a["slo_target"]) for n, a in A.items()])
    metric("autostation_error_budget_remaining_ratio", "Error budget left (negative = exhausted).", "gauge",
           [([("automation", n)], a["error_budget_remaining"]) for n, a in A.items()])
    metric("autostation_burn_rate", "Error budget burn rate (1 = on schedule).", "gauge",
           [([("automation", n), ("window", w)], a[k]) for n, a in A.items() for w, k in (("24h", "burn_rate_24h"), ("7d", "burn_rate_7d"))])
    metric("autostation_mttr_seconds", "MTTR per automation.", "gauge", [([("automation", n)], a["mttr_seconds"]) for n, a in A.items()])
    metric("autostation_mtbf_seconds", "MTBF per automation.", "gauge", [([("automation", n)], a["mtbf_seconds"]) for n, a in A.items()])
    metric("autostation_failure_streak", "Consecutive failed runs right now.", "gauge", [([("automation", n)], a["failure_streak"]) for n, a in A.items()])
    metric("autostation_flaky", "1 when the automation flips between ok and failed.", "gauge", [([("automation", n)], a["flaky"]) for n, a in A.items()])
    metric("autostation_run_duration_ms", "Successful run duration quantiles.", "gauge",
           [([("automation", n), ("quantile", q)], a["duration_ms"][k]) for n, a in A.items() for q, k in (("0.5", "p50"), ("0.95", "p95"), ("0.99", "p99"))])
    metric("autostation_on_time_ratio", "Share of expected scheduled runs that started on time.", "gauge",
           [([("automation", n)], a["on_time_ratio"]) for n, a in A.items()])
    metric("autostation_ring_runs", "Counted runs per ring.", "gauge", [([("ring", r)], d["runs"]) for r, d in m["rings"].items()])
    metric("autostation_ring_failed_runs", "Failed runs per ring.", "gauge", [([("ring", r)], d["failed"]) for r, d in m["rings"].items()])
    metric("autostation_signals", "Active compliance signals.", "gauge", [([("type", s["type"]), ("severity", s["severity"])], s["count"]) for s in m["signals"]])
    metric("autostation_error_category_runs", "Failed runs per error category in the window.", "gauge", [([("category", k)], v) for k, v in m["error_categories"].items()])
    return "\n".join(out) + "\n"


def fleet_status(c: dict, now: datetime) -> dict:
    names = c["names"]
    if not c["runs"]:
        return {"schema_version": 1, "generated_at": so._iso(now), "status": "unknown", "reasons": ["no run data yet"], "automations": {n: "no data" for n in names}}
    states = {n: automation_state(n, c) for n in names}
    reasons = []
    sev = {s["severity"] for s in c["signals"]}
    if sev & {"critical", "high"}:
        reasons.append("critical/high signals: " + ", ".join(sorted({s["type"] for s in c["signals"] if s["severity"] in ("critical", "high")})))
    if c["fleet"]["budget_exhausted"]:
        reasons.append("error budget exhausted: " + ", ".join(c["fleet"]["budget_exhausted"][:10]))
    amber = []
    if "medium" in sev:
        amber.append("medium signals: " + ", ".join(sorted({s["type"] for s in c["signals"] if s["severity"] == "medium"})))
    low = [n for n, p in c["per"].items() if p["budget"]["remaining"] is not None and 0 <= p["budget"]["remaining"] < 0.25]
    if low:
        amber.append("error budget under 25%: " + ", ".join(sorted(low)[:10]))
    status = "red" if reasons else ("amber" if amber else "green")
    return {"schema_version": 1, "generated_at": so._iso(now), "status": status, "reasons": reasons + amber,
            "counts": {k: sum(1 for v in states.values() if v == k) for k in ("passing", "degraded", "failing", "paused", "no data")},
            "automations": states}


def badge_svg(label: str, value: str) -> str:
    color = BADGE_COLORS.get(value, "#6e7781")
    lw, vw = 6 * len(label) + 14, 6 * len(value) + 14
    w = lw + vw
    l, v = esc(label), esc(value)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" aria-label="{l}: {v}"><title>{l}: {v}</title>'
            f'<rect width="{lw}" height="20" fill="#555"/><rect x="{lw}" width="{vw}" height="20" fill="{color}"/>'
            f'<g fill="#fff" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11" text-anchor="middle">'
            f'<text x="{lw / 2:.0f}" y="14">{l}</text><text x="{lw + vw / 2:.0f}" y="14">{v}</text></g></svg>\n')


# ---------------------------------------------------------------------------------------------------------------- search
def search_index(c: dict, manifest: dict, now: datetime) -> list[dict]:
    def clean(v, n):    # display strings only; angle brackets are dropped as defence in depth (the script uses textContent anyway)
        return so._s(v, n).replace("<", " ").replace(">", " ")
    items: list[dict] = []
    for t, u, s in (("Reliability / SLO", "reliability/index.html", "error budgets, MTTR, flakiness"), ("Errors & remediation", "errors/index.html", "signatures, regressions"),
                    ("Jobs", "jobs/index.html", "run history"), ("Machines", "machines/index.html", "heartbeats and versions"),
                    ("Queues & approvals", "queues/index.html", "queues, SLA"), ("Schedules", "schedules/index.html", "next runs"),
                    ("Audit", "audit/index.html", "releases, signing"), ("Install center", "install/index.html", "download an automation"),
                    ("Docs", "docs/index.html", "documentation")):
        items.append({"t": "page", "n": t, "s": s, "u": u})
    for name in c["names"]:
        entry = c["autos"].get(name) or {}
        items.append({"t": "automation", "n": name, "s": clean(entry.get("description"), 100), "u": f"jobs/index.html#auto={name}"})
    for h in sorted({clean(r.get("host"), 100) for r in c["runs"] if r.get("host")})[:60]:
        if NAME_RE.match(h):
            items.append({"t": "machine", "n": h, "s": "machine", "u": f"jobs/index.html#host={h}"})
    for s in c["taxonomy"]["signatures"][:100]:
        items.append({"t": "error", "n": clean(f"{s['category']}: {s['sample']}", 110), "s": f"{s['count']} run(s), signature {s['signature']}",
                      "u": f"errors/index.html#sig-{s['signature']}"})
    for r in sorted(c["runs"], key=lambda r: r["started_at"], reverse=True)[:350]:
        rid = so._id(r.get("run_id"))
        if rid:
            items.append({"t": "run", "n": rid, "s": clean(f"{r['automation']} on {r.get('host') or '?'} - {r.get('status')}", 120), "u": f"jobs/index.html#run={rid}"})
    return items[:MAX_SEARCH]


# --------------------------------------------------------------------------------------------------------------- entry
def build_obs(out: Path, cfg: SiteConfig, manifest: dict, data, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    out = Path(out)
    try:
        c = compute_all(manifest or {}, data, now)
    except Exception as exc:  # noqa: BLE001  (forged telemetry must not fail the site build: fall back to the manifest alone)
        import sys
        sys.stderr.write(f"::warning title=observability compute failed::{type(exc).__name__}; using the manifest only\n")
        c = compute_all(manifest or {}, None, now)
    pages: list[str] = []
    pages += guarded(out, cfg, "reliability", "Reliability", build_reliability, c, data, now, active="reliability")
    pages += guarded(out, cfg, "errors", "Errors and remediation", build_errors, c, data, now, active="errors")
    m = metrics_dict(c, cfg, now)
    write(out, "metrics.json", json.dumps(m, indent=2, sort_keys=True) + "\n")
    write(out, "metrics.prom", metrics_prom(m))
    fs = fleet_status(c, now)
    write(out, "fleet-status.json", json.dumps(fs, indent=2, sort_keys=True) + "\n")
    pages += ["metrics.json", "metrics.prom", "fleet-status.json"]
    fleet_word = {"green": "passing", "amber": "degraded", "red": "failing"}.get(fs["status"], "no data")
    write(out, "badges/fleet.svg", badge_svg("autostation", fleet_word))
    pages.append("badges/fleet.svg")
    for name, state in sorted(fs["automations"].items()):
        if NAME_RE.match(name):
            write(out, f"badges/automation-{name}.svg", badge_svg(name[:40], state))     # prefixed: no automation can overwrite badges/fleet.svg
            pages.append(f"badges/automation-{name}.svg")
    write(out, "search-ops.json", json.dumps(search_index(c, manifest or {}, now), separators=(",", ":"), sort_keys=True))
    write(out, "assets/gsearch.js", GSEARCH_JS.read_text(encoding="utf-8"))
    pages += ["search-ops.json", "assets/gsearch.js"]
    return pages
