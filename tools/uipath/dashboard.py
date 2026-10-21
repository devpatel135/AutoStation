#!/usr/bin/env python3
"""Migration dashboard: where every UiPath project is on its way to AutoStation, what is left, and what retiring it saves.

    python tools/uipath/dashboard.py --inventory inv/inventory.json --status migration/status.yaml --out dash/
                                     [--program program.yaml] [--now 2026-10-21]

Writes ``index.html`` (single file, light/dark, no external assets) and ``dashboard.json`` (the same numbers, machine readable).
The status file is human-edited YAML (schema: migration/README.md); projects missing from it are "inventoried".
The licence figures are an ESTIMATE driven by ``program.yaml`` inputs - they are not a quote or a commitment.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "components" / "wrapper-core"))

import htmlutil as H  # noqa: E402
from wrapper_core import miniyaml  # noqa: E402

STAGES = ["inventoried", "classified", "scaffolded", "in_dev", "pilot", "broad", "retired"]
STAGE_LABEL = {"inventoried": "Inventoried", "classified": "Classified", "scaffolded": "Scaffolded", "in_dev": "In dev", "pilot": "Pilot ring",
               "broad": "Broad ring", "retired": "UiPath retired"}


def load_yaml(path):
    if not path:
        return {}
    doc = miniyaml.load(path)
    return doc if isinstance(doc, dict) else {}


def safe_link(url) -> str | None:
    u = str(url or "").strip()
    if not u:
        return None
    low = u.lower()
    if low.startswith(("http://", "https://")) or ("://" not in low and ":" not in low.split("/")[0]):
        return u
    return None


def parse_date(v):
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def compute(inv: dict, status: dict, program: dict, today: date) -> dict:
    warnings: list[str] = []
    st_projects = status.get("projects") or {}
    inv_projects = {p["name"]: p for p in inv.get("projects", [])}
    names = sorted(set(inv_projects) | set(st_projects), key=str.lower)
    waves_meta = status.get("waves") or {}
    rows = []
    for name in names:
        p = inv_projects.get(name)
        s = st_projects.get(name) or {}
        stage = str(s.get("status", "inventoried"))
        if stage not in STAGES:
            warnings.append(f"{name}: unknown status {stage!r}; treated as inventoried (valid: {', '.join(STAGES)})")
            stage = "inventoried"
        if name not in inv_projects:
            warnings.append(f"{name}: in the status file but not in the inventory (effort unknown)")
        if name not in st_projects:
            pass  # simply not started
        effort = float(p["effort_days"]) if p else 0.0
        idx = STAGES.index(stage)
        spent = s.get("effort_spent_days")
        if spent is not None:
            try:
                remaining = max(effort - float(spent), 0.0)
            except (TypeError, ValueError):
                remaining = effort * (1 - idx / (len(STAGES) - 1))
        else:
            remaining = effort * (1 - idx / (len(STAGES) - 1))
        if stage == "retired":
            remaining = 0.0
        target = parse_date(s.get("target_date"))
        blockers = [str(b) for b in (s.get("blockers") or [])] if isinstance(s.get("blockers"), list) else ([str(s["blockers"])] if s.get("blockers") else [])
        parity = s.get("parity") if isinstance(s.get("parity"), dict) else {}
        robots = s.get("robots_retired") or 0
        if isinstance(robots, dict):
            ru, ra = int(robots.get("unattended", 0) or 0), int(robots.get("attended", 0) or 0)
        else:
            ru, ra = int(robots or 0), 0
        rows.append({"name": name, "pattern": p["pattern"]["recommended"] if p else "", "pattern_label": p["pattern"]["label"] if p else "",
                     "effort_days": effort, "remaining_days": round(remaining, 1), "stage": stage, "stage_index": idx,
                     "wave": str(s.get("wave", "unassigned")), "owner": str(s.get("owner", "")), "target_date": target.isoformat() if target else "",
                     "overdue": bool(target and target < today and stage != "retired"), "blockers": blockers,
                     "parity": {"verdict": str(parity.get("verdict", "")).upper(), "date": str(parity.get("date", "")), "link": safe_link(parity.get("report"))},
                     "risks_high": sum(1 for r in p["risks"] if r["severity"] == "high") if p else 0,
                     "robots_retired": {"unattended": ru if stage == "retired" else 0, "attended": ra if stage == "retired" else 0},
                     "notes": str(s.get("notes", ""))})
    funnel = [{"stage": st, "label": STAGE_LABEL[st], "count": sum(1 for r in rows if r["stage_index"] >= i)} for i, st in enumerate(STAGES)]
    waves: dict[str, dict] = {}
    for r in rows:
        w = waves.setdefault(r["wave"], {"wave": r["wave"], "projects": 0, "effort_days": 0.0, "remaining_days": 0.0, "retired": 0})
        w["projects"] += 1
        w["effort_days"] += r["effort_days"]
        w["remaining_days"] += r["remaining_days"]
        w["retired"] += 1 if r["stage"] == "retired" else 0
    wave_list = []
    for key in sorted(waves, key=lambda k: (k == "unassigned", k)):
        meta = waves_meta.get(key) or waves_meta.get(int(key) if key.isdigit() else key) or {}
        w = waves[key]
        wave_list.append({**w, "effort_days": round(w["effort_days"], 1), "remaining_days": round(w["remaining_days"], 1),
                          "name": str((meta or {}).get("name", "")), "target": str((meta or {}).get("target", ""))})
    # licence estimate -------------------------------------------------------------------------------------
    lic = program.get("licence") or program.get("license") or {}
    robots = program.get("robots") or {}
    cu, ca = float(lic.get("annual_cost_per_unattended_robot", 0) or 0), float(lic.get("annual_cost_per_attended_robot", 0) or 0)
    ru_total, ra_total = int(robots.get("unattended_total", 0) or 0), int(robots.get("attended_total", 0) or 0)
    ru_ret = sum(r["robots_retired"]["unattended"] for r in rows)
    ra_ret = sum(r["robots_retired"]["attended"] for r in rows)
    orch = float(lic.get("annual_orchestrator_cost", 0) or 0)
    all_retired = bool(rows) and all(r["stage"] == "retired" for r in rows)
    potential = ru_total * cu + ra_total * ca + orch
    realised = ru_ret * cu + ra_ret * ca + (orch if all_retired else 0.0)
    estimator = {"configured": bool(lic or robots), "currency": str(lic.get("currency", "USD")), "unattended_total": ru_total, "attended_total": ra_total,
                 "unattended_retired": ru_ret, "attended_retired": ra_ret, "annual_potential": round(potential, 2), "annual_realised": round(realised, 2),
                 "annual_remaining": round(potential - realised, 2), "orchestrator_cost_counted": all_retired and orch > 0,
                 "disclaimer": "ESTIMATE from program.yaml inputs; not a quote or a commitment"}
    parity_counts = {k: sum(1 for r in rows if r["parity"]["verdict"] == k) for k in ("GO", "NO-GO")}
    parity_counts["pending"] = sum(1 for r in rows if r["parity"]["verdict"] not in ("GO", "NO-GO") and r["stage_index"] >= STAGES.index("in_dev"))
    totals = {"projects": len(rows), "retired": sum(1 for r in rows if r["stage"] == "retired"), "effort_days": round(sum(r["effort_days"] for r in rows), 1),
              "remaining_days": round(sum(r["remaining_days"] for r in rows), 1), "blocked": sum(1 for r in rows if r["blockers"]),
              "overdue": sum(1 for r in rows if r["overdue"])}
    history = [{"date": str(h.get("date", "")), "remaining_days": float(h.get("remaining_days", 0) or 0), "retired": int(h.get("retired", 0) or 0)}
               for h in (status.get("history") or []) if isinstance(h, dict)]
    return {"schema_version": 1, "as_of": today.isoformat(), "program": str((status.get("program") or {}).get("name", "UiPath to AutoStation migration")),
            "totals": totals, "funnel": funnel, "waves": wave_list, "projects": rows, "parity": parity_counts, "licence": estimator,
            "history": history, "warnings": warnings}


def sparkline(history: list[dict], key: str = "remaining_days") -> str:
    pts = [h[key] for h in history]
    if len(pts) < 2:
        return ""
    w, h, pad = 360, 70, 6
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1
    xy = [(pad + i * (w - 2 * pad) / (len(pts) - 1), h - pad - (v - lo) * (h - 2 * pad) / span) for i, v in enumerate(pts)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img" aria-label="remaining effort over time">'
            f'<polyline fill="none" stroke="var(--accent)" stroke-width="2" points="{path}"/></svg>'
            f'<div class="sub">{H.esc(history[0]["date"])} - {H.esc(history[-1]["date"])}: {pts[0]:g} to {pts[-1]:g} days remaining</div>')


def render(d: dict) -> str:
    t, lic = d["totals"], d["licence"]
    body = ['<div class="grid">' + H.tile("Projects", t["projects"]) + H.tile("UiPath retired", t["retired"], f"{t['projects'] - t['retired']} to go")
            + H.tile("Effort remaining", f"{t['remaining_days']} d", f"of {t['effort_days']} d planned") + H.tile("Blocked", t["blocked"]) + H.tile("Overdue", t["overdue"])
            + H.tile("Parity GO", d["parity"]["GO"], f"{d['parity']['NO-GO']} no-go, {d['parity']['pending']} pending") + "</div>"]
    if d["warnings"]:
        body.append('<div class="note warn"><b>Data warnings</b><ul>' + "".join(f"<li>{H.esc(w)}</li>" for w in d["warnings"]) + "</ul></div>")
    top = max((f["count"] for f in d["funnel"]), default=1) or 1
    body.append("<h2>Migration funnel</h2>" + H.table(["Stage", "Projects at or beyond", ""], [[f["label"], f["count"], H.bar(100 * f["count"] / top)] for f in d["funnel"]], raw_cols=(2,)))
    body.append("<h2>Waves and burn-down</h2>" + H.table(
        ["Wave", "Name", "Target", "Projects", "Retired", "Effort (d)", "Remaining (d)", "Progress"],
        [[w["wave"], w["name"], w["target"], w["projects"], w["retired"], w["effort_days"], w["remaining_days"],
          H.bar(100 * (1 - w["remaining_days"] / w["effort_days"]) if w["effort_days"] else 0)] for w in d["waves"]], raw_cols=(7,)))
    if d["history"]:
        body.append("<h3>Remaining effort over time</h3>" + sparkline(d["history"]))
    rows = []
    for r in d["projects"]:
        link = H.esc(r["parity"]["link"]) if r["parity"]["link"] else ""
        v = r["parity"]["verdict"]
        pv = (H.pill(v, "go" if v == "GO" else "nogo") if v in ("GO", "NO-GO") else H.pill("pending", "info"))
        if link:
            pv += f' <a href="{link}" rel="noopener noreferrer">report</a>'
        rows.append([H.esc(r["name"]), H.esc(r["pattern_label"] or "-"), H.esc(r["wave"]), H.pill(STAGE_LABEL[r["stage"]], "green" if r["stage"] in ("broad", "retired") else "info"),
                     H.esc(r["owner"]), H.esc(r["target_date"]) + (" " + H.pill("overdue", "high") if r["overdue"] else ""),
                     "<br>".join(H.esc(b) for b in r["blockers"]) or "-", pv, H.esc(f"{r['remaining_days']} / {r['effort_days']} d")])
    body.append("<h2>Projects</h2>" + H.table(["Project", "Target pattern", "Wave", "Status", "Owner", "Target date", "Blockers", "Parity", "Remaining / planned"], rows, raw_cols=tuple(range(9))))
    body.append("<h2>Licence savings estimator</h2>")
    if lic["configured"]:
        cur = H.esc(lic["currency"])
        body.append('<div class="note warn"><b>ESTIMATE.</b> Computed from <code>program.yaml</code> inputs (robots x annual cost). Not a quote or a commitment.</div>')
        body.append('<div class="grid">' + H.tile("Realised / yr", f"{lic['annual_realised']:,.0f} {lic['currency']}", f"{lic['unattended_retired']} unattended + {lic['attended_retired']} attended robots retired")
                    + H.tile("Remaining / yr", f"{lic['annual_remaining']:,.0f} {lic['currency']}", f"{lic['unattended_total'] - lic['unattended_retired']} unattended + {lic['attended_total'] - lic['attended_retired']} attended left")
                    + H.tile("Potential / yr", f"{lic['annual_potential']:,.0f} {lic['currency']}", "at full migration") + "</div>")
        body.append(f"<p class='sub'>Currency {cur}. Orchestrator cost is counted only once every project is retired.</p>")
    else:
        body.append('<p class="sub">Provide <code>--program program.yaml</code> (robots and annual licence cost) to see the estimate.</p>')
    return H.page(d["program"], "AUTOSTATION - UiPath migration dashboard", f"As of {d['as_of']}. Source: inventory.json + the hand-maintained status file.", "".join(body))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--status", default=None, help="migration status YAML (optional: everything is 'inventoried' without it)")
    ap.add_argument("--program", default=None, help="program.yaml with robots + licence costs")
    ap.add_argument("--out", required=True)
    ap.add_argument("--now", default=None, help="YYYY-MM-DD (default today)")
    a = ap.parse_args(argv)
    try:
        inv = json.loads(Path(a.inventory).read_text(encoding="utf-8"))
        status = load_yaml(a.status)
        program = load_yaml(a.program)
    except (OSError, ValueError, miniyaml.YamlError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    today = parse_date(a.now) if a.now else date.today()
    if today is None:
        print("error: --now must be YYYY-MM-DD", file=sys.stderr)
        return 2
    d = compute(inv, status, program, today)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "dashboard.json", "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(d, indent=2, sort_keys=True) + "\n")
    with open(out / "index.html", "w", encoding="utf-8", newline="\n") as f:
        f.write(render(d))
    t = d["totals"]
    print(f"{t['projects']} project(s), {t['retired']} retired, {t['remaining_days']} of {t['effort_days']} effort days remaining; wrote {out / 'index.html'}")
    for w in d["warnings"]:
        print("  warning:", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
