#!/usr/bin/env python3
"""Generate an AutoStation scaffold for every project in an inventory.

    python tools/uipath/scaffold.py inv/inventory.json --out scaffolds/ [--project NAME ...] [--owner team@example.com]
                                    [--owner-team "Team"] [--force]

Per project (``<out>/automation-<kebab-name>/``):

* a runnable payload from the right template - ``transactional`` for queue/REFramework projects, ``node`` (Playwright, written
  inline) for browser-only projects, ``python`` for everything else (needs-redesign projects too, with extra TODOs);
* TODO stubs mapped from the activities found: one ``migrate_<workflow>`` stub per source workflow with the activities and the
  suggested AutoStation call as comments (REFramework ``Framework/`` files are skipped: the template already covers them);
* ``manifest-entry.yaml`` - a registry stanza (paused until first release) whose schedule is derived from the Orchestrator export
  (cron -> slot), ``error_handling.max_retries`` from RetryScope/queue retries, classification from the inventory hint;
* ``MIGRATION.md`` - source inventory, mapping table, risks, parity checklist and cutover steps.

UiPath assets/credentials are referenced by NAME only; no value is ever read or copied.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "components" / "wrapper-core"))

import catalog as K  # noqa: E402
import new_automation as NA  # noqa: E402
from common import kebab, snake  # noqa: E402
from wrapper_core import miniyaml  # noqa: E402

LEVELS = ("public", "internal", "confidential", "restricted")


def automation_name(project: str) -> str:
    k = kebab(project)
    return "automation-" + (k if k else "project")


def pick_template(p: dict) -> str:
    rec = p["pattern"]["recommended"]
    if rec == "queue-transactional":
        return "transactional"
    if rec == "playwright-browser":
        return "node"
    return "python"


def stub_workflows(p: dict) -> list[dict]:
    skip_framework = p["reframework"]["detected"]
    out = []
    for w in p["workflows"]:
        low = w["file"].lower()
        if skip_framework and (low.startswith("framework/") or low in ("main.xaml",)):
            continue
        out.append(w)
    return out


def _equiv_for(p: dict, activity: str) -> str:
    for e in p["equivalents"]:
        if e["activity"] == activity:
            return e["autostation"]
    return "(control flow)" if K.CATEGORY_LABEL.get("control_flow") else ""


def stubs_python(p: dict, auto: str) -> str:
    lines = ["", "",
             "# " + "=" * 112,
             f"# MIGRATION MAP - generated from the UiPath inventory of '{p['name']}' ({p['pattern']['label']}).",
             "# One stub per source workflow. Delete this block when the migration is complete.",
             "# " + "=" * 112]
    used: set[str] = set()
    for w in stub_workflows(p):
        fn = "migrate_" + snake(w["file"].rsplit(".", 1)[0].replace("/", "_"))
        while fn in used:
            fn += "_2"
        used.add(fn)
        lines += ["", "", f"def {fn}():", f'    """TODO: migrate {w["file"]} ({sum(w["activities"].values())} activities)."""']
        for name, n in sorted(w["activities"].items(), key=lambda kv: (-kv[1], kv[0])):
            eq = _equiv_for(p, name)
            if eq == "(control flow)":
                continue
            lines.append(f"    # {name} x{n}  ->  {eq}")
        if w["invokes"]:
            lines.append("    # invokes: " + ", ".join(w["invokes"]))
        if w["attended"]:
            lines.append("    # ATTENDED activities here (" + ", ".join(w["attended"]) + "): remove the user interaction")
        lines.append(f'    raise NotImplementedError("TODO: migrate {w["file"]}")')
    return "\n".join(lines) + "\n"


def stubs_js(p: dict) -> str:
    lines = ["", "// " + "=" * 110, f"// MIGRATION MAP - generated from the UiPath inventory of '{p['name']}'. One block per source workflow.",
             "// Delete when the migration is complete.", "// " + "=" * 110]
    for w in stub_workflows(p):
        lines.append(f"//\n// TODO: migrate {w['file']}")
        for name, n in sorted(w["activities"].items(), key=lambda kv: (-kv[1], kv[0])):
            eq = _equiv_for(p, name)
            if eq != "(control flow)":
                lines.append(f"//   {name} x{n}  ->  {eq}")
        if w["attended"]:
            lines.append("//   ATTENDED (" + ", ".join(w["attended"]) + "): remove the user interaction")
    return "\n".join(lines) + "\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def node_payload(out: Path, auto: str, p: dict, desc: str) -> list[Path]:
    write(out / "automation.yaml", f"name: {auto}\nversion: 0.1.0\nruntime: node\nentrypoint: main.js\nargs: []\n"
                                   'requires: {wrapper_core: ">=0.1.0,<1.0.0", execution_framework: ">=0.1.0,<1.0.0"}\ntimeout_s: 1800\n')
    main = (f"'use strict';\n/**\n * {auto}: {desc}\n *\n * Migrated from UiPath project '{p['name']}' (browser automation). Runs in the user's Edge profile (SSO/cert);\n"
            " * credentials never live in this file. Selectors self-heal through fw.resilience (LLM pattern 3, validated and cached).\n"
            " * Rules: docs/AUTOMATION_STANDARD.md. Honours --dry-run (no side effects).\n */\n"
            "const path = require('node:path');\n"
            "const fw = require(process.env.AUTOMAGIX_FRAMEWORK_DIR || path.resolve(__dirname, '..', '..', 'execution-framework'));\n\n"
            "fw.wrapper.entrypoint(async (framework) => {\n  const w = framework.wrapper;\n  w.logEvent('start', { dry_run: w.isDryRun() });\n"
            "  const session = await framework.browser.launch({ headless: String(w.param('headless', 'true')) !== 'false', channel: w.param('channel', 'msedge') });\n"
            "  try {\n    const { page } = session;\n"
            f"    const rp = framework.resilience.resilientPage(page, {{ caller: '{auto}' }});\n"
            "    // TODO: page.goto(url) / rp.click(selector) / rp.fill(selector, value) ... per the migration map below\n"
            "    void rp;\n    w.setResult({ migrated: false });\n  } finally {\n    await session.close();\n  }\n});\n")
    write(out / "main.js", main + stubs_js(p))
    write(out / "README.md", f"# {auto}\n\n{desc}\n\nNode/Playwright payload scaffolded from UiPath project `{p['name']}`. See `MIGRATION.md` for the mapping and parity checklist.\n"
                             "Needs Node 18+, `npm ci --omit=dev` in `components/execution-framework`, and Microsoft Edge.\n")
    return sorted(out.rglob("*"))


def manifest_entry(p: dict, auto: str, owner: str, owner_team: str) -> dict:
    orch = p.get("orchestrator") or {}
    level = p["classification_hint"]["level"]
    slots, notes, tz = [], [], "UTC"
    for s in orch.get("schedules", []):
        if not s.get("enabled", True):
            notes.append(f"schedule '{s['name']}' is disabled in Orchestrator; not carried over")
            continue
        if s.get("slot"):
            slot = dict(s["slot"])
            slot["id"] = kebab(s["name"]) or slot["type"]
            slot["params"] = {"mode": "both"} if p["pattern"]["recommended"] == "queue-transactional" else {}
            slots.append(slot)
            tz = s.get("timezone") or tz
        else:
            notes.append(s.get("note") or f"schedule '{s['name']}' needs manual conversion")
    ids = set()
    for s in slots:  # unique slot ids
        base, i = s["id"], 2
        while s["id"] in ids:
            s["id"] = f"{base}-{i}"
            i += 1
        ids.add(s["id"])
    if not slots:
        slots = [{"id": "daily", "type": "daily", "time": "09:00", "params": {"mode": "both"} if p["pattern"]["recommended"] == "queue-transactional" else {}}]
    retries = max(p["retry_numbers"] + [q["max_retries"] for q in orch.get("queues", []) if q.get("max_retries")] + [3])
    systems = sorted({s.split(":", 1)[1] for s in p["systems"]}) or ["TBD"]
    return {auto: {
        "description": (p["description"] or f"Migrated from UiPath project {p['name']}")[:200],
        "owner": owner, "owner_team": owner_team, "systems": systems, "data_classification": level,
        "status": "paused", "paused_reason": "migration scaffold - awaiting first release",
        "llm_allowed_classification": "internal" if LEVELS.index(level) >= 1 else "public",
        "rings": {}, "versions": {},
        "schedule": {"timezone": tz, "expected_duration_ms": {"baseline": 120000, "warn_threshold": 2.0},
                     "maintenance_windows": [{"days": ["Saturday", "Sunday"]}], "slots": slots},
        "error_handling": {"max_retries": retries, "network_retry_wait_s": 60, "backoff_multiplier": 2.0, "skip_on_data_error": True,
                           "escalate_after_retries": True},
        "notify_on_complete": False,
        "catalog": {"summary": (p["description"] or f"Migrated from UiPath project {p['name']}")[:200], "systems": systems, "owner_team": owner_team,
                    "data_classification": level, "process_owner": owner, "links": [], "minutes_per_item": 0,
                    "tags": ["uipath-migration", p["pattern"]["recommended"]]},
    }}, notes


def migration_md(p: dict, auto: str, template: str, sched_notes: list[str]) -> str:
    L = [f"# Migration: {p['name']} -> {auto}", "",
         f"* **Source:** UiPath project `{p['name']}` (`{p['path']}`), Studio {p['studio_version'] or '?'}, {p['expression_language'] or '?'} expressions",
         f"* **Target pattern:** {p['pattern']['label']} (template: `{template}`)",
         f"* **Complexity:** {p['complexity']['score']} ({p['complexity']['level']}); **estimated effort:** {p['effort_days']} days (planning estimate)",
         f"* **Data classification hint:** {p['classification_hint']['level']}"
         + (f" (keywords: {', '.join(p['classification_hint']['keywords'])})" if p["classification_hint"]["keywords"] else ""), ""]
    L += ["## Why this target", ""] + [f"* {x}" for x in p["pattern"]["reasoning"]] + [""]
    if p["risks"]:
        L += ["## Risks", "", "| Severity | Id | Detail |", "|---|---|---|"] + [f"| {r['severity']} | {r['id']} | {r['message']} |" for r in p["risks"]] + [""]
    L += ["## Activity mapping", "", "| UiPath activity | Count | AutoStation |", "|---|---|---|"]
    L += [f"| `{e['activity']}` | {e['count']} | {e['autostation']} |" for e in p["equivalents"]] + [""]
    L += ["## Source facts (names only - values are never copied)", "",
          f"* Queues: {', '.join(p['queues']) or '-'}", f"* Assets: {', '.join(p['assets']) or '-'}",
          f"* Credentials (re-create in Windows Credential Manager as `AUTOMAGIX/<name>`): {', '.join(p['credentials']) or '-'}",
          f"* Systems touched: {', '.join(p['systems']) or '-'}"]
    if p["reframework"]["detected"]:
        L.append("* REFramework: yes - `Process.xaml` logic goes into `process_transaction()`; Config.xlsx settings become manifest `assets`/slot `params`")
    L.append("")
    if sched_notes:
        L += ["## Schedule notes", ""] + [f"* {n}" for n in sched_notes] + [""]
    L += ["## Parity checklist", "",
          "- [ ] Inputs identified and reproducible in dev (same source data for both robots)",
          "- [ ] Business rules re-implemented; every UiPath `BusinessRuleException` path maps to `wrapper.DataError`",
          "- [ ] System-exception behaviour matches (retry counts, backoff, escalation) - see `error_handling` in `manifest-entry.yaml`",
          "- [ ] Credentials re-created in Credential Manager / SSO; **no secrets in the repo**; the old hard-coded ones rotated",
          "- [ ] Attended-only steps removed or replaced by params/files/rules",
          "- [ ] Selectors/timeouts verified against the real target; healing events reviewed",
          "- [ ] `python -m wrapper_core assist review` clean of HIGH findings; `python -m pytest` green",
          "- [ ] Parallel run: outputs compared with `tools/uipath/parity.py` for N consecutive runs -> verdict GO",
          "- [ ] Owner, classification, schedule and `catalog` fields completed in the registry MR",
          "- [ ] Promoted dev -> pilot -> broad (MR approvals); monitors/alerts routed to the owner list", "",
          "## Cutover", "",
          "1. Disable the UiPath trigger (do not delete the process yet).", "2. Watch two scheduled AutoStation runs on the dashboard.",
          "3. Keep UiPath available for rollback for the agreed window; then archive the project and retire the robot (licence true-down).", ""]
    return "\n".join(L)


def scaffold_project(p: dict, out_root: Path, owner: str, owner_team: str, force: bool) -> dict:
    auto = automation_name(p["name"])
    template = pick_template(p)
    out = out_root / auto
    if out.exists():
        if not force:
            raise FileExistsError(f"{out} exists (use --force)")
        shutil.rmtree(out)
    desc = (p["description"] or f"Migrated from UiPath project {p['name']}").replace('"', "'")[:200]
    if template == "node":
        node_payload(out, auto, p, desc)
    else:
        values = {"name": auto, "description": desc, "owner": owner, "owner_team": owner_team,
                  "classification": p["classification_hint"]["level"], "queue_name": auto[len("automation-"):]}
        NA.render_tree(TOOLS / "templates" / f"automation-{'transactional' if template == 'transactional' else 'python'}", out, values)
        main = out / "main.py"
        text = main.read_text(encoding="utf-8")
        marker = '\n\nif __name__ == "__main__":'
        stub = stubs_python(p, auto)
        text = text.replace(marker, stub + marker, 1) if marker in text else text + stub
        write(main, text)
    entry, notes = manifest_entry(p, auto, owner, owner_team)
    write(out / "manifest-entry.yaml", miniyaml.dumps(entry))
    write(out / "MIGRATION.md", migration_md(p, auto, template, notes))
    return {"project": p["name"], "automation": auto, "template": template, "path": str(out), "schedule_notes": notes}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inventory", help="inventory.json written by inventory.py")
    ap.add_argument("--out", required=True)
    ap.add_argument("--project", action="append", default=[], help="only these projects (repeatable)")
    ap.add_argument("--owner", default="owner-team@example.com", help="owning distribution list (placeholder by default)")
    ap.add_argument("--owner-team", default="TODO owner team")
    ap.add_argument("--force", action="store_true", help="overwrite existing scaffold directories")
    a = ap.parse_args(argv)
    try:
        inv = json.loads(Path(a.inventory).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: cannot read inventory: {exc}", file=sys.stderr)
        return 2
    if "@" not in a.owner:
        print("error: --owner must be an e-mail/distribution list", file=sys.stderr)
        return 2
    projects = [p for p in inv.get("projects", []) if not a.project or p["name"] in a.project]
    if not projects:
        print("error: no matching projects in the inventory", file=sys.stderr)
        return 1
    out_root = Path(a.out)
    results = []
    for p in projects:
        try:
            r = scaffold_project(p, out_root, a.owner, a.owner_team, a.force)
        except FileExistsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        results.append(r)
        print(f"  {p['name']:<28} -> {r['automation']:<36} template={r['template']}")
    write(out_root / "scaffold-index.json", json.dumps({"scaffolds": results}, indent=2, sort_keys=True) + "\n")
    print(f"{len(results)} scaffold(s) in {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
