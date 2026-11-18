#!/usr/bin/env python3
"""Health-gated promotion of the staged rollout: dev -> pilot -> broad (one ring per run, per package).

``build_registry.py --auto-promote`` puts a NEW version in ring ``dev`` only. This tool (run by ``promote.yml`` every couple of hours)
decides, per component / automation, whether the version that runs in ring N may move to ring N+1:

  1. soak     the version has been in the source ring for at least ``--soak-pilot-h`` (dev -> pilot) / ``--soak-broad-h`` (pilot -> broad)
              hours. The manifest carries no timestamps, so the first time this tool sees ``name|ring|version`` it records it in
              ``rollout-state.json`` (committed by the bot): soak counts from the first observation.
  2. health   the live health report is trustworthy (``ops_issues.report_trust``: loaded records, fresh, no failed builder) and not older
              than ``--max-report-age-h``, and holds no FAILED / HUNG / REGRESSION_AFTER_RELEASE / ERROR_BUDGET_BURN signal for that
              automation (a version-specific signal only blocks that version). A MACHINE_UNHEALTHY signal has no automation: it holds
              back the shared components (wrapper-core, execution-framework), which every machine runs.
  3. issues   no open ``ops-signal`` issue titled for that name, and no open delivery-pipeline alert (pipe-release / pipe-site / pipe-site-verify / pipe-site-stale /
              pipe-installers / pipe-canary): while the chain that delivers to machines is red, nothing is promoted.
  4. state    the package is not paused, the fleet is not paused, the manifest is valid.

Everything that is not a promotion is a reason, written to the step summary. A package blocked for a reason other than soak for at least
``--stuck-h`` hours is listed under ``stuck`` (the workflow raises one ops issue for it, and resolves it when the block clears).
Pause / rollback / fleet-pause are never gated: this tool only ever moves rings UP, one step, through ``registry.ops.promote``.

Exit codes: 0 = decision made (see the JSON), 2 = unusable input.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
COMPONENTS = ROOT / "components"
for p in (HERE, COMPONENTS / "wrapper-core", COMPONENTS / "automation-registry"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ops_issues import report_trust  # noqa: E402

RINGS = ("dev", "pilot", "broad")
BLOCKING_TYPES = ("FAILED", "HUNG", "REGRESSION_AFTER_RELEASE", "ERROR_BUDGET_BURN")
MACHINE_TYPES = ("MACHINE_UNHEALTHY",)
DEFAULT_SOAK_H = {"pilot": 6.0, "broad": 24.0}
DEFAULT_MAX_REPORT_AGE_H = 6.0
DEFAULT_STUCK_H = 24.0
STATE_VERSION = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _vkey(text) -> tuple:
    out = []
    for piece in str(text).replace("-", ".").split("."):
        out.append((1, int(piece)) if piece.isdigit() else (0, 0))
    return tuple(out)


def empty_state() -> dict:
    return {"version": STATE_VERSION, "first_seen": {}, "blocked_since": {}, "stuck_alerts": []}


def load_state(path: Path | None) -> dict:
    if path and Path(path).exists():
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
        if isinstance(doc, dict):
            st = empty_state()
            for k in ("first_seen", "blocked_since"):
                if isinstance(doc.get(k), dict):
                    st[k] = {str(a): str(b) for a, b in doc[k].items()}
            if isinstance(doc.get("stuck_alerts"), list):
                st["stuck_alerts"] = [str(x) for x in doc["stuck_alerts"]]
            return st
    return empty_state()


def _packages(manifest: dict):
    for kind in ("components", "automations"):
        for name, entry in sorted((manifest.get(kind) or {}).items()):
            yield kind[:-1], name, (entry or {})


def _issue_names_it(issue: dict, name: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9_-])" + re.escape(name) + r"(?![A-Za-z0-9_-])", str(issue.get("title") or ""), re.I) is not None


# pipeline alerts that mean "the delivery chain is red": while one is open nothing is promoted. (ci, promote and backup-registry alerts do not
# change what machines download, so they do not hold promotions back.)
DELIVERY_PIPES = re.compile(r"autostation-signal:pipe-(?:release|site|site-verify|site-stale|installers|canary)(?![A-Za-z0-9_-])")


def _is_pipe_issue(issue: dict) -> bool:
    return DELIVERY_PIPES.search(str(issue.get("body") or "")) is not None


def _is_gate_issue(issue: dict) -> bool:
    """The gate's own 'promotion is stuck' issue names the package, but must not itself keep it blocked."""
    return "autostation-signal:promote-stuck-" in str(issue.get("body") or "")


def _open(issues) -> list[dict]:
    return [i for i in (issues or []) if isinstance(i, dict) and str(i.get("state", "open")).lower() == "open" and not _is_gate_issue(i)]


def fetch_open_issues(repo: str, token: str) -> list[dict]:
    """Open ``ops-signal`` issues through tools/ops_issues.py's GitHub client. Raises when the listing is partial or fails."""
    import ops_issues
    gh = ops_issues.GitHub(repo, token)
    return [i for i in gh.list_ops_issues() if str(i.get("state")).lower() == "open"]


def sync_alerts(res: dict) -> list[str]:
    """Raise one ops issue per package whose promotion has been blocked for --stuck-h hours; resolve the ones that cleared."""
    import subprocess
    notes: list[str] = []
    ops = str(HERE / "ops_issues.py")
    for b in res.get("stuck") or []:
        cmd = [sys.executable, ops, "raise", "--id", f"promote-stuck-{b['name']}", "--type", "PROMOTION_STUCK", "--severity", "medium",
               "--title", f"Promotion stuck: {b['name']} {b['version']} cannot move to {b['to']}",
               "--detail", f"{b['name']} {b['version']} has been held back from ring {b['to']} for {b.get('blocked_h', '?')} h: {b['reason']}. "
                           "Fix the cause (see the promote workflow summary) or promote it by hand with the ops-action workflow."]
        rc = subprocess.run(cmd, check=False, timeout=120).returncode
        notes.append(f"raised promote-stuck-{b['name']} (exit {rc})")
    for iid in res.get("recovered") or []:
        rc = subprocess.run([sys.executable, ops, "resolve", "--id", iid, "--note", "The promotion is no longer blocked."], check=False, timeout=120).returncode
        notes.append(f"resolved {iid} (exit {rc})")
    return notes


def health_verdict(report, now: datetime, max_age_h: float) -> tuple[bool, str]:
    """Is the live health report good enough to promote on? (an empty or stale report must never read as 'healthy')."""
    if report is None:
        return False, "no health report is available"
    ok, why = report_trust(report)
    if not ok:
        return False, f"the health report cannot prove health ({why})"
    gen = parse_iso(report.get("generated_at"))
    if gen is None:
        return False, "the health report has no generated_at"
    age_h = (now - gen).total_seconds() / 3600.0
    if age_h > max_age_h:
        return False, f"the health report is {age_h:.1f} h old (limit {max_age_h:g} h)"
    return True, ""


def blocking_signals(report: dict, kind: str, name: str, version: str) -> list[dict]:
    out = []
    for s in report.get("signals") or []:
        if not isinstance(s, dict):
            continue
        typ = str(s.get("type") or "").upper()
        if typ in BLOCKING_TYPES and s.get("automation") == name and (not s.get("version") or str(s.get("version")) == str(version)):
            out.append(s)
        elif typ in MACHINE_TYPES and kind == "component":
            out.append(s)
    return out


def candidate(entry: dict) -> tuple[str, str, str] | None:
    """(source ring, target ring, version) for the single next step, or None when every ring is level (or a rollback made a lower ring newer)."""
    rings = entry.get("rings") or {}
    for src, dst in (("pilot", "broad"), ("dev", "pilot")):          # the furthest-along step first
        s, d = rings.get(src), rings.get(dst)
        if s and (not d or _vkey(s) > _vkey(d)):
            # a version may only skip ahead of its source ring's own source: dev -> pilot first, then pilot -> broad
            return src, dst, str(s)
    return None


def plan(manifest: dict, state: dict, report, issues, now: datetime | None = None, soak_h: dict | None = None,
         max_report_age_h: float = DEFAULT_MAX_REPORT_AGE_H, stuck_h: float = DEFAULT_STUCK_H, only: str | None = None) -> dict:
    """The decision. Pure: returns {promotions, blocked, waiting, stuck, recovered, state}."""
    now = now or _now()
    soak = dict(DEFAULT_SOAK_H, **(soak_h or {}))
    new_state = empty_state()
    old_first = dict(state.get("first_seen") or {})
    old_blocked = dict(state.get("blocked_since") or {})
    open_issues = _open(issues)
    pipe_open = [i for i in open_issues if _is_pipe_issue(i)]
    healthy, health_why = health_verdict(report, now, max_report_age_h)
    fleet_paused = bool((manifest.get("fleet") or {}).get("paused"))

    promotions: list[dict] = []
    blocked: list[dict] = []
    waiting: list[dict] = []

    for kind, name, entry in _packages(manifest):
        rings = entry.get("rings") or {}
        # record first sight of every (name, ring, version) so soak survives between runs; forget versions that left the rings
        for ring in RINGS:
            v = rings.get(ring)
            if v:
                key = f"{name}|{ring}|{v}"
                new_state["first_seen"][key] = old_first.get(key) or iso(now)
        cand = candidate(entry)
        if not cand or (only and name != only):
            continue
        src, dst, ver = cand
        seen = parse_iso(new_state["first_seen"][f"{name}|{src}|{ver}"]) or now
        soaked_h = max(0.0, (now - seen).total_seconds() / 3600.0)
        need = soak["pilot" if dst == "pilot" else "broad"]
        item = {"name": name, "kind": kind, "from": src, "to": dst, "version": ver, "soaked_h": round(soaked_h, 1), "soak_h": need}
        bkey = f"{name}|{dst}|{ver}"

        reason = None
        if fleet_paused:
            reason = "the fleet is paused"
        elif kind == "automation" and entry.get("status") == "paused":
            reason = f"{name} is paused ({entry.get('paused_reason') or 'no reason'})"
        if reason is None and soaked_h < need:
            item["reason"] = f"soaking in {src}: {soaked_h:.1f} of {need:g} h"
            waiting.append(item)
            continue
        if reason is None and not healthy:
            reason = health_why
        if reason is None:
            sigs = blocking_signals(report, kind, name, ver)
            if sigs:
                kinds = sorted({str(s.get("type")).upper() for s in sigs})
                reason = f"open health signal(s) for {name} {ver}: {', '.join(kinds)}"
        if reason is None:
            mine = [i for i in open_issues if _issue_names_it(i, name)]
            if mine:
                reason = f"open ops issue #{mine[0].get('number', '?')} names {name}"
            elif pipe_open:
                reason = f"a pipeline alert is open (#{pipe_open[0].get('number', '?')}): nothing is promoted while the release pipeline is red"
        if reason:
            item["reason"] = reason
            since = parse_iso(old_blocked.get(bkey)) or now
            new_state["blocked_since"][bkey] = iso(since)
            item["blocked_h"] = round((now - since).total_seconds() / 3600.0, 1)
            blocked.append(item)
            continue
        item["reason"] = "soak complete and healthy"
        promotions.append(item)

    stuck = [b for b in blocked if b.get("blocked_h", 0) >= stuck_h]
    stuck_ids = sorted({f"promote-stuck-{b['name']}" for b in stuck})
    recovered = [i for i in state.get("stuck_alerts") or [] if i not in stuck_ids]
    new_state["stuck_alerts"] = stuck_ids
    return {"promotions": promotions, "blocked": blocked, "waiting": waiting, "stuck": stuck, "stuck_ids": stuck_ids,
            "recovered": recovered, "healthy": healthy, "health_note": health_why, "state": new_state}


def apply_promotions(manifest_path: Path, promotions: list[dict]) -> list[str]:
    """Promote through the registry ops (the same code ``registry promote`` runs) and persist with its validation + history."""
    from registry import ops, store  # noqa: WPS433
    m = store.load_manifest(manifest_path)
    done: list[str] = []
    for p in promotions:
        res = ops.promote(m, p["name"], p["to"], p["version"])
        if res.manifest_changed:
            done.append(f"{p['name']} {p['version']} -> {p['to']}")
    if done:
        store.save_manifest(manifest_path, m)
        for line in done:
            store.append_history(manifest_path, "promote", line.split(" ")[0], f"{line} (promote-gate: soak complete, healthy)", "promote-gate")
    return done


def summary_markdown(res: dict, applied: list[str], dry: bool) -> str:
    lines = ["### Staged rollout", ""]
    if applied:
        lines += [("Would promote" if dry else "Promoted") + ":"] + [f"- {a}" for a in applied] + [""]
    else:
        lines += ["Nothing promoted this run.", ""]
    if not res["healthy"]:
        lines += [f"Health gate closed: {res['health_note']}", ""]
    for title, key in (("Blocked", "blocked"), ("Soaking", "waiting")):
        if res[key]:
            lines += [f"**{title}**", ""] + [f"- {i['name']} {i['version']} ({i['from']} -> {i['to']}): {i['reason']}" for i in res[key]] + [""]
    return "\n".join(lines)


def _load_json(path: str | None):
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        v = float(raw)
        return v if v >= 0 else default
    except ValueError:
        return default


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", default=str(COMPONENTS / "automation-registry" / "manifest.yaml"))
    ap.add_argument("--state", default=str(COMPONENTS / "automation-registry" / "rollout-state.json"))
    ap.add_argument("--report", default=None, help="live health report (analyze --out report.json)")
    ap.add_argument("--issues", default=None, help="JSON list of the open issues (as returned by the GitHub issues API)")
    ap.add_argument("--fetch-issues", default=None, metavar="OWNER/REPO",
                    help="read the open ops-signal issues from GitHub (GITHUB_TOKEN); failing to read them stops the run: no blind promotion")
    ap.add_argument("--only", default=None, help="consider just this package (workflow_dispatch input)")
    ap.add_argument("--alerts", action="store_true", help="with --apply: raise/resolve the 'promotion stuck' ops issues (GITHUB_TOKEN)")
    ap.add_argument("--soak-pilot-h", type=float, default=_env_float("AUTOSTATION_SOAK_PILOT_H", DEFAULT_SOAK_H["pilot"]))
    ap.add_argument("--soak-broad-h", type=float, default=_env_float("AUTOSTATION_SOAK_BROAD_H", DEFAULT_SOAK_H["broad"]))
    ap.add_argument("--max-report-age-h", type=float, default=DEFAULT_MAX_REPORT_AGE_H)
    ap.add_argument("--stuck-h", type=float, default=DEFAULT_STUCK_H)
    ap.add_argument("--apply", action="store_true", help="write the promotions into the manifest and the state file (default: only decide)")
    ap.add_argument("--out", default=None, help="write the decision JSON here")
    ap.add_argument("--summary", default=None, help="append the markdown summary here (GITHUB_STEP_SUMMARY)")
    a = ap.parse_args(argv)

    mpath = Path(a.manifest)
    try:
        from registry import store
        manifest = store.load_manifest(mpath)
    except Exception as exc:  # noqa: BLE001
        print(f"::error title=promote-gate::cannot read the manifest: {exc}")
        return 2
    report = _load_json(a.report)
    issues = _load_json(a.issues)
    if a.issues and not isinstance(issues, list):
        print("::error title=promote-gate::the issues file is unreadable; refusing to promote blind")
        return 2
    if a.only and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", a.only):
        print("::error title=promote-gate::--only must be a package name")
        return 2
    if a.fetch_issues:
        try:
            issues = fetch_open_issues(a.fetch_issues, os.environ.get("GITHUB_TOKEN", ""))
        except Exception as exc:  # noqa: BLE001
            print(f"::error title=promote-gate::cannot read the open issues ({exc}); refusing to promote blind")
            return 2
    state = load_state(Path(a.state))
    res = plan(manifest, state, report, issues or [], _now(), {"pilot": a.soak_pilot_h, "broad": a.soak_broad_h}, a.max_report_age_h, a.stuck_h, a.only)

    applied: list[str] = []
    if a.apply:
        applied = apply_promotions(mpath, res["promotions"])
        # the soak clock of a package that just moved starts in its new ring
        for p in res["promotions"]:
            if f"{p['name']} {p['version']} -> {p['to']}" in applied:
                res["state"]["first_seen"].setdefault(f"{p['name']}|{p['to']}|{p['version']}", iso(_now()))
        Path(a.state).write_text(json.dumps(res["state"], indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    else:
        applied = [f"{p['name']} {p['version']} -> {p['to']}" for p in res["promotions"]]
    if a.apply and a.alerts:
        for note in sync_alerts(res):
            print(note)
    out = {k: v for k, v in res.items() if k != "state"}
    out["applied"] = applied if a.apply else []
    text = json.dumps(out, indent=2, sort_keys=True)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8", newline="\n")
    md = summary_markdown(res, applied, dry=not a.apply)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as fh:
            fh.write(md + "\n")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
