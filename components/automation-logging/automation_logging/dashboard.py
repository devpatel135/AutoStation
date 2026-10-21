"""Static metrics dashboard for GitLab Pages: ``index.html`` + ``data.json`` + ``signals.json`` + ``runs-latest.json``.

The page is server-rendered with no inline data blobs and no script that touches log content. Every log-derived string
(error messages, synopses, LLM diagnoses, host names...) passes through ``esc()``.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from .common import write_json
from . import analytics


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def fmt_ms(ms) -> str:
    if ms is None:
        return "-"
    s = ms / 1000
    if s < 1:
        return f"{int(ms)} ms"
    if s < 60:
        return f"{s:.1f} s"
    return f"{int(s // 60)}m {int(s % 60):02d}s"


def pct(x) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def spark(values: list, w: int = 120, h: int = 28, cls: str = "ok") -> str:
    vals = [float(v or 0) for v in values]
    if not vals or max(vals) == 0:
        return f'<svg class="spark {esc(cls)}" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true"><line x1="0" y1="{h - 2}" x2="{w}" y2="{h - 2}"/></svg>'
    mx = max(vals)
    step = w / max(1, len(vals) - 1)
    pts = " ".join(f"{i * step:.1f},{h - 2 - (v / mx) * (h - 4):.1f}" for i, v in enumerate(vals))
    return f'<svg class="spark {esc(cls)}" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true"><polyline points="{pts}"/></svg>'


def pill(text, kind: str) -> str:
    return f'<span class="pill {esc(kind)}">{esc(text)}</span>'


CSS = """
:root{--bg:#0f1117;--surface:#181c27;--surface2:#1e2335;--fg:#e2e4eb;--muted:#8a90a8;--border:#2a3048;--accent:#4f8ef7;
--green:#34d399;--yellow:#fbbf24;--red:#f87171;--purple:#c084fc;--orange:#fb923c;color-scheme:dark;
--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;--sans:'Inter',system-ui,-apple-system,Segoe UI,sans-serif}
@media (prefers-color-scheme:light){:root:not([data-theme="dark"]){--bg:#f5f6fa;--surface:#fff;--surface2:#eef0f8;--fg:#111320;--muted:#5a6080;
--border:#dde0f0;--accent:#1f4dcc;--green:#15803d;--yellow:#b45309;--red:#dc2626;--purple:#7c3aed;--orange:#c2410c;color-scheme:light}}
:root[data-theme="light"]{--bg:#f5f6fa;--surface:#fff;--surface2:#eef0f8;--fg:#111320;--muted:#5a6080;--border:#dde0f0;--accent:#1f4dcc;
--green:#15803d;--yellow:#b45309;--red:#dc2626;--purple:#7c3aed;--orange:#c2410c;color-scheme:light}
:root[data-theme="dark"]{color-scheme:dark}
*{box-sizing:border-box;margin:0;padding:0}
body{font:14px/1.6 var(--sans);background:var(--bg);color:var(--fg);padding:1.5rem 16px 3rem}
.page{max-width:1100px;margin:0 auto}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;margin-bottom:1.25rem}
.eyebrow{font:11px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--accent)}
h1{font-size:1.5rem;font-weight:800}
.sub{color:var(--muted);font-size:12.5px}
h2{font:700 .75rem var(--mono);text-transform:uppercase;letter-spacing:.1em;color:var(--accent);margin:2rem 0 .8rem;padding-bottom:.4rem;border-bottom:1px solid var(--border)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:.8rem 1rem}
.card .k{font:10px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.card .v{font-size:1.5rem;font-weight:800}
.card .s{font-size:12px;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:.9rem}
.acard{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:.9rem 1rem;border-left-width:3px}
.acard.healthy{border-left-color:var(--green)}.acard.degraded{border-left-color:var(--yellow)}.acard.failing{border-left-color:var(--red)}
.acard.paused{border-left-color:var(--purple)}.acard.unknown{border-left-color:var(--muted)}
.acard h3{font:700 13px var(--mono);word-break:break-all}
.acard .desc{font-size:12px;color:var(--muted);margin:.2rem 0 .5rem}
.syn{font-size:12.5px;margin:.5rem 0;color:var(--fg)}
.strip{display:flex;gap:3px;margin:.4rem 0}
.strip i{width:12px;height:12px;border-radius:2px;background:var(--muted);display:block}
.strip i.success{background:var(--green)}.strip i.partial{background:var(--yellow)}.strip i.failed,.strip i.hung,.strip i.self_heal_failed{background:var(--red)}
.strip i.halted_paused,.strip i.skipped_maintenance{background:var(--purple)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;font-size:12px;margin-top:.4rem}.kv b{color:var(--muted);font-weight:600}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;padding:6px 10px;font:10px var(--mono);letter-spacing:.07em;text-transform:uppercase;background:var(--surface2);border:1px solid var(--border);color:var(--muted)}
td{padding:7px 10px;border:1px solid var(--border);background:var(--surface);vertical-align:top;overflow-wrap:anywhere}
.tw{overflow-x:auto}
.pill{display:inline-block;font:700 10px var(--mono);padding:2px 7px;border-radius:3px;background:var(--surface2);white-space:nowrap}
.pill.critical,.pill.failing{background:#3b1010;color:var(--red)}.pill.high{background:#3a2208;color:var(--orange)}
.pill.medium,.pill.degraded{background:#33290a;color:var(--yellow)}.pill.low,.pill.unknown{color:var(--muted)}
.pill.healthy,.pill.ok{background:#0b2a1d;color:var(--green)}.pill.paused{background:#25103a;color:var(--purple)}
@media (prefers-color-scheme:light){:root:not([data-theme="dark"]) .pill.critical,:root:not([data-theme="dark"]) .pill.failing{background:#fee2e2}
:root:not([data-theme="dark"]) .pill.high{background:#ffedd5}:root:not([data-theme="dark"]) .pill.medium,:root:not([data-theme="dark"]) .pill.degraded{background:#fef3c7}
:root:not([data-theme="dark"]) .pill.healthy,:root:not([data-theme="dark"]) .pill.ok{background:#dcfce7}:root:not([data-theme="dark"]) .pill.paused{background:#ede9fe}}
.diag{font-size:12px;color:var(--muted)}.diag b{color:var(--fg)}
.spark{stroke:var(--green);fill:none;stroke-width:1.6}.spark.bad{stroke:var(--red)}.spark line{stroke:var(--border);stroke-width:1}
.ok-note{padding:1rem;border:1px dashed var(--border);border-radius:8px;color:var(--muted)}
button.theme{background:var(--surface);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:.3rem .7rem;font:12px var(--mono);cursor:pointer}
code{font:12px var(--mono);background:var(--surface2);padding:1px 5px;border-radius:3px}
@media (max-width:640px){h1{font-size:1.2rem}.grid{grid-template-columns:1fr}}
"""


def render(report: dict, runs_latest: list[dict]) -> str:
    fleet = report["fleet"]
    sigs = report["signals"]
    parts: list[str] = []
    A = parts.append
    A("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
      "<title>AUTOSTATION Fleet Dashboard</title>"
      "<link rel=\"stylesheet\" href=\"https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=IBM+Plex+Mono:wght@400;700&display=swap\">"
      f"<style>{CSS}</style></head><body><div class=\"page\">")
    A(f"<header><div><div class=\"eyebrow\">AUTOSTATION &middot; Fleet dashboard</div><h1>Automation fleet health</h1>"
      f"<div class=\"sub\">Generated {esc(report['generated_at'])} &middot; last {esc(report['window_days'])} days &middot; "
      f"overall {pill(fleet['status'], {'healthy': 'ok', 'paused': 'paused', 'degraded': 'medium', 'attention': 'high', 'critical': 'critical'}.get(fleet['status'], 'unknown'))}"
      f"{' &middot; ' + pill('FLEET PAUSED', 'paused') if report.get('fleet_paused') else ''}</div></div>"
      "<button class=\"theme\" type=\"button\" onclick=\"var r=document.documentElement;r.dataset.theme=(r.dataset.theme==='light'?'dark':'light')\">toggle theme</button></header>")

    # --- summary cards
    imp = report["impact"]
    sc = fleet["signals"]
    cards = [("Automations", fleet["automations"], f"{fleet['hosts']} host(s)"),
             ("Runs", fleet["total"], f"{fleet['success']} ok / {fleet['partial']} partial / {fleet['failed'] + fleet['hung']} failed"),
             ("Success rate", pct(fleet["success_rate"]), "excludes paused / maintenance skips"),
             ("Active signals", fleet["signals_total"], f"{sc.get('critical', 0)} critical, {sc.get('high', 0)} high, {sc.get('medium', 0)} medium"),
             ("Items processed", imp["items_30d"], f"{imp['items_7d']} in last 7 days")]
    if imp.get("hours_saved_30d") is not None:
        cards.append(("Est. hours saved", imp["hours_saved_30d"], "last 30 days (catalog estimate)"))
    A("<h2>Fleet summary</h2><div class=\"cards\">" + "".join(
        f"<div class=\"card\"><div class=\"k\">{esc(k)}</div><div class=\"v\">{esc(v)}</div><div class=\"s\">{esc(s)}</div></div>" for k, v, s in cards) + "</div>")

    # --- signals
    A("<h2>Active compliance signals</h2>")
    if not sigs:
        A("<div class=\"ok-note\">No active signals. Every scheduled run is accounted for.</div>")
    else:
        A("<div class=\"tw\"><table><tr><th>Severity</th><th>Signal</th><th>Automation</th><th>Host</th><th>Detail</th><th>Diagnosis</th><th>First seen</th><th>Owner</th></tr>")
        for s in sigs:
            d = s.get("diagnosis")
            diag = (f"<div class=\"diag\"><b>{esc(d.get('root_cause'))}</b><br>Next: {esc(d.get('suggested_action'))} "
                    f"({esc(d.get('confidence'))} confidence)</div>") if d else f"<span class=\"diag\">{esc(s.get('diagnosis_reason') or '-')}</span>"
            who = s.get("automation") or ", ".join(s.get("automations") or []) or "-"
            A(f"<tr><td>{pill(s['severity'], s['severity'])}</td><td><code>{esc(s['type'])}</code></td><td>{esc(who)}</td><td>{esc(s.get('host') or '-')}</td>"
              f"<td>{esc(s.get('detail'))}</td><td>{diag}</td><td>{esc(s.get('first_seen'))}</td><td>{esc(s.get('owner') or '-')}</td></tr>")
        A("</table></div>")

    # --- automation cards
    A("<h2>Automations</h2><div class=\"grid\">")
    for name, a in sorted(report["automations"].items()):
        last = a.get("last_run") or {}
        strip = "".join(f"<i class=\"{esc(r['status'])}\" title=\"{esc(r['started_at'])} {esc(r['status'])}\"></i>" for r in reversed(a["recent_runs"]))
        runs_spark = spark([d["runs"] for d in a["daily"]])
        fail_spark = spark([d["failed"] for d in a["daily"]], cls="bad")
        it = a["items"]
        A(f"<div class=\"acard {esc(a['health'])}\"><div style=\"display:flex;justify-content:space-between;gap:.5rem\"><h3>{esc(name)}</h3>{pill(a['health'], a['health'])}</div>"
          f"<div class=\"desc\">{esc(a.get('description') or 'not in manifest')} &middot; owner {esc(a.get('owner') or '-')}</div>"
          f"<div class=\"strip\" aria-label=\"last runs\">{strip}</div>"
          f"<div class=\"syn\">{esc(last.get('synopsis') or 'No runs in window.')}</div>"
          f"<div class=\"kv\"><b>Last run</b><span>{esc(last.get('started_at') or '-')} ({esc(last.get('status') or '-')})</span>"
          f"<b>Runs</b><span>{a['runs']['total']} &middot; success {pct(a['runs']['success_rate'])}</span>"
          f"<b>Duration</b><span>p50 {fmt_ms(a['duration_ms']['p50'])} / p95 {fmt_ms(a['duration_ms']['p95'])}</span>"
          f"<b>Items</b><span>{it['total']} &middot; {it['failed']} failed &middot; skip {pct(it['skip_rate'])}</span>"
          f"<b>Runs/day</b><span>{runs_spark}</span><b>Failures/day</b><span>{fail_spark}</span>"
          f"<b>Versions</b><span>{esc(json.dumps(a.get('ring_versions') or {}))}</span></div>"
          + (f"<div class=\"diag\" style=\"margin-top:.4rem\">Paused: {esc(a.get('paused_reason'))}</div>" if a["health"] == "paused" else "") + "</div>")
    A("</div>")

    # --- item-level
    A("<h2>Item-level telemetry</h2><div class=\"tw\"><table><tr><th>Automation</th><th>Items</th><th>Throughput /min</th><th>P50</th><th>P95</th><th>P99</th>"
      "<th>Skip rate</th><th>Skip reasons</th><th>Item failure rate</th><th>Run failure rate</th><th>Runs ok with failed items</th></tr>")
    for name, a in sorted(report["automations"].items()):
        it = a["items"]
        if not it["total"]:
            continue
        A(f"<tr><td>{esc(name)}</td><td>{it['total']}</td><td>{esc(it['throughput_per_min'])}</td><td>{fmt_ms(it['duration_ms']['p50'])}</td>"
          f"<td>{fmt_ms(it['duration_ms']['p95'])}</td><td>{fmt_ms(it['duration_ms']['p99'])}</td><td>{pct(it['skip_rate'])}</td>"
          f"<td>{esc(', '.join(f'{k}: {v}' for k, v in it['skip_reasons'].items()) or '-')}</td><td>{pct(it['item_failure_rate'])}</td>"
          f"<td>{pct(it['run_failure_rate'])}</td><td>{it['runs_with_item_failures']}</td></tr>")
    A("</table></div>")

    # --- retries + LLM
    rt = report["retries"]
    A(f"<h2>Intelligent retry</h2><p class=\"sub\">{rt['total_events']} retry event(s) in {rt['runs_with_retries']} run(s) ({pct(rt['retry_run_rate'])} of runs).</p>"
      "<div class=\"tw\"><table><tr><th>Category</th><th>Events</th><th>Recovered</th><th>Escalated</th><th>Skipped</th><th>Recovery rate</th></tr>")
    for cat, c in rt["by_category"].items():
        A(f"<tr><td><code>{esc(cat)}</code></td><td>{c['events']}</td><td>{c['recovered']}</td><td>{c['escalated']}</td><td>{c['skipped']}</td><td>{pct(c['recovery_rate'])}</td></tr>")
    A("</table></div>")
    ll = report["llm"]
    A(f"<h2>LLM usage</h2><div class=\"cards\"><div class=\"card\"><div class=\"k\">Calls</div><div class=\"v\">{ll['calls']}</div><div class=\"s\">error rate {pct(ll['error_rate'])}</div></div>"
      f"<div class=\"card\"><div class=\"k\">Avg latency</div><div class=\"v\">{fmt_ms(ll['duration_ms']['avg'])}</div><div class=\"s\">p95 {fmt_ms(ll['duration_ms']['p95'])}</div></div>"
      f"<div class=\"card\"><div class=\"k\">Synopses</div><div class=\"v\">{ll['synopsis_from_llm']}</div><div class=\"s\">{ll['synopsis_deterministic']} deterministic fallback</div></div>"
      f"<div class=\"card\"><div class=\"k\">Decisions / schema failures</div><div class=\"v\">{ll['decisions']} / {ll['schema_failures']}</div><div class=\"s\">audit trail entries</div></div></div>")

    # --- versions
    A("<h2>Fleet versions</h2><div class=\"tw\"><table><tr><th>Host</th><th>Ring</th><th>Last heartbeat</th><th>wrapper-core</th><th>execution-framework</th><th>Automations</th></tr>")
    for h in report["versions"]["hosts"]:
        comp = lambda c: f"{esc(c['version'])} {pill('ok', 'ok') if c['ok'] else pill('target ' + str(c['target']), 'medium')}"  # noqa: E731
        autos = "<br>".join(f"{esc(n)} {esc(v['version'])} {pill('ok', 'ok') if v['ok'] else pill('target ' + str(v['target']), 'medium')}" for n, v in h["automations"].items())
        A(f"<tr><td>{esc(h['host'])}</td><td>{esc(h['ring'])}</td><td>{esc(h['last_seen'])}</td><td>{comp(h['components']['wrapper-core'])}</td>"
          f"<td>{comp(h['components']['execution-framework'])}</td><td>{autos or '-'}</td></tr>")
    A("</table></div>")

    # --- recent runs
    A("<h2>Latest runs</h2><div class=\"tw\"><table><tr><th>Started</th><th>Automation</th><th>Host</th><th>Status</th><th>Duration</th><th>Synopsis</th></tr>")
    for r in runs_latest[:25]:
        kind = {"success": "ok", "partial": "medium", "failed": "critical", "hung": "critical", "self_heal_failed": "critical"}.get(r["status"], "paused")
        A(f"<tr><td>{esc(r['started_at'])}</td><td>{esc(r['automation'])}</td><td>{esc(r.get('host'))}</td><td>{pill(r['status'], kind)}</td>"
          f"<td>{fmt_ms(r.get('duration_ms'))}</td><td>{esc(r.get('synopsis'))}</td></tr>")
    A("</table></div></div></body></html>")
    return "".join(parts)


def build_site(report: dict, data: analytics.Data, out_dir: str | Path, signals: list[dict] | None = None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    runs_latest = [analytics.run_summary(r) for r in sorted(data.runs, key=lambda r: r["started_at"], reverse=True)[:50]]
    (out / "index.html").write_text(render(report, runs_latest), encoding="utf-8")
    write_json(out / "data.json", report)
    write_json(out / "signals.json", {"schema_version": 1, "generated_at": report["generated_at"],
                                      "signals": signals if signals is not None else report["signals"]})
    write_json(out / "runs-latest.json", {"schema_version": 1, "generated_at": report["generated_at"], "runs": runs_latest})
    return {"files": ["index.html", "data.json", "signals.json", "runs-latest.json"], "out": str(out)}
