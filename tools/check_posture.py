#!/usr/bin/env python3
"""OpenSSF-Scorecard-style security posture check, implemented here because only GitHub-owned actions may run.

    python tools/check_posture.py [--repo owner/name] [--markdown out.md]

Static checks (always): policy files present, every workflow action pinned to a SHA, no third-party actions, least-privilege permissions
and timeouts (tools/check_workflows.py), no credential-shaped strings (tools/check_secrets.py), Dependabot configured.
Live checks (only when GITHUB_TOKEN can read the settings; otherwise reported as SKIP, never as a pass): rulesets protect the default branch and
the append-only registry-artifacts branch against deletion and force-push, secret scanning + push protection are on, and the default workflow token is read-only.
Exit code 1 only for a FAIL; SKIP is informational. The markdown table goes to $GITHUB_STEP_SUMMARY when set.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_workflows  # noqa: E402

Result = Tuple[str, str, str]      # (check, PASS|FAIL|SKIP, detail)
REQUIRED_FILES = ["SECURITY.md", ".github/CODEOWNERS", ".github/dependabot.yml", ".github/PULL_REQUEST_TEMPLATE.md", "docs/SECURITY.md"]
PROTECTED_BRANCHES = ("~DEFAULT_BRANCH", "refs/heads/registry-artifacts", "refs/heads/triggers")
# spellings of "the default branch" a ruleset may legitimately use
DEFAULT_BRANCH_ALIASES = {"~DEFAULT_BRANCH": ("~DEFAULT_BRANCH", "~ALL", "refs/heads/main", "refs/heads/master")}


def check_files(root: Path = ROOT) -> List[Result]:
    missing = [f for f in REQUIRED_FILES if not (root / f).is_file()]
    return [("policy files", "FAIL" if missing else "PASS", ("missing: " + ", ".join(missing)) if missing else f"{len(REQUIRED_FILES)} present")]


def check_workflow_rules(root: Path = ROOT) -> List[Result]:
    wf = sorted((root / ".github" / "workflows").glob("*.yml"))
    errs: List[str] = []
    for f in wf:
        errs += check_workflows.check_file(f, root)
    unpinned = [e for e in errs if "pinned to a full commit SHA" in e]
    third = [e for e in errs if "not GitHub-owned" in e]
    other = [e for e in errs if e not in unpinned and e not in third]
    return [("actions pinned to commit SHAs", "FAIL" if unpinned else "PASS", f"{len(unpinned)} unpinned" if unpinned else f"{len(wf)} workflow(s)"),
            ("only GitHub-owned actions", "FAIL" if third else "PASS", f"{len(third)} third-party use(s)" if third else "ok"),
            ("workflow rules (permissions, timeouts, injection)", "FAIL" if other else "PASS", f"{len(other)} problem(s): {other[0][:120]}" if other else "ok")]


def check_secrets_scan(root: Path = ROOT) -> List[Result]:
    r = subprocess.run([sys.executable, str(root / "tools" / "check_secrets.py")], capture_output=True, text=True, timeout=300, cwd=str(root))
    return [("no credential-shaped strings committed", "PASS" if r.returncode == 0 else "FAIL", (r.stdout.strip().splitlines() or [""])[-1])]


def evaluate_rulesets(rulesets: Iterable[Dict[str, Any]]) -> List[Result]:
    """Each ruleset dict is the full object (conditions + rules + enforcement)."""
    out: List[Result] = []
    for branch in PROTECTED_BRANCHES:
        ok = False
        for rs in rulesets:
            if rs.get("enforcement") != "active" or rs.get("target") != "branch":
                continue
            include = ((rs.get("conditions") or {}).get("ref_name") or {}).get("include") or []
            rules = {r.get("type") for r in rs.get("rules") or []}
            names = DEFAULT_BRANCH_ALIASES.get(branch, (branch, "~ALL"))
            if any(n in include for n in names) and {"deletion", "non_fast_forward"} <= rules:
                if rs.get("bypass_actors"):
                    out.append((f"ruleset protects {branch}", "FAIL", "the ruleset has bypass actors: they defeat it"))
                    ok = None
                    break
                ok = True
        if ok is None:
            continue
        out.append((f"ruleset protects {branch}", "PASS" if ok else "FAIL", "no deletion / force-push" if ok else "no active ruleset blocks deletion and force-push"))
    return out


def evaluate_repo(repo: Dict[str, Any]) -> List[Result]:
    sa = repo.get("security_and_analysis")
    if not isinstance(sa, dict):
        return [("secret scanning + push protection", "SKIP", "token cannot read security settings (needs repo admin)")]
    on = lambda k: (sa.get(k) or {}).get("status") == "enabled"      # noqa: E731
    ok = on("secret_scanning") and on("secret_scanning_push_protection")
    return [("secret scanning + push protection", "PASS" if ok else "FAIL", "enabled" if ok else "turn on in Settings > Code security (tools/harden_repo.py does it)")]


def evaluate_workflow_permissions(doc: Optional[Dict[str, Any]]) -> List[Result]:
    if not isinstance(doc, dict):
        return [("default workflow token is read-only", "SKIP", "token cannot read Actions settings (needs repo admin)")]
    ok = doc.get("default_workflow_permissions") == "read"
    approve = bool(doc.get("can_approve_pull_request_reviews"))
    return [("default workflow token is read-only", "PASS" if ok else "FAIL", "read" if ok else str(doc.get("default_workflow_permissions"))),
            ("workflow tokens cannot approve pull requests", "FAIL" if approve else "PASS",
             "can_approve_pull_request_reviews is on: a workflow could approve its own change" if approve else "off")]


def live_checks(repo: str, token: str, opener=None) -> List[Result]:
    from ops_issues import GitHub
    kw = {"opener": opener} if opener else {}
    gh = GitHub(repo, token, **kw)
    out: List[Result] = []
    code, listing = gh.request("GET", f"/repos/{repo}/rulesets")
    if code == 200 and isinstance(listing, list):
        full = []
        for item in listing[:30]:
            c, rs = gh.request("GET", f"/repos/{repo}/rulesets/{int(item.get('id', 0))}")
            if c == 200 and isinstance(rs, dict):
                full.append(rs)
        out += evaluate_rulesets(full)
    else:
        out.append(("rulesets", "SKIP", f"could not list rulesets (HTTP {code})"))
    code, info = gh.request("GET", f"/repos/{repo}")
    out += evaluate_repo(info if code == 200 and isinstance(info, dict) else {})
    code, perms = gh.request("GET", f"/repos/{repo}/actions/permissions/workflow")
    out += evaluate_workflow_permissions(perms if code == 200 else None)
    return out


def _cell(text: str) -> str:
    return re.sub(r"[|\r\n]+", " ", str(text))[:200]


def render_markdown(results: List[Result]) -> str:
    icon = {"PASS": "pass", "FAIL": "**FAIL**", "SKIP": "skip"}
    rows = ["### Security posture", "", "| Check | Result | Detail |", "|---|---|---|"]
    rows += [f"| {_cell(c)} | {icon.get(s, s)} | {_cell(d)} |" for c, s, d in results]
    return "\n".join(rows) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--markdown", default="")
    a = ap.parse_args(argv)
    results = check_files() + check_workflow_rules() + check_secrets_scan()
    token = os.environ.get("GITHUB_TOKEN", "")
    if a.repo and token:
        try:
            results += live_checks(a.repo, token)
        except Exception as exc:  # noqa: BLE001
            results.append(("live settings", "SKIP", f"{type(exc).__name__}: {str(exc)[:100]}"))
    else:
        results.append(("live settings (rulesets, secret scanning, token permissions)", "SKIP", "no GITHUB_TOKEN / repo given"))
    md = render_markdown(results)
    print(md)
    for target in (a.markdown, os.environ.get("GITHUB_STEP_SUMMARY", "")):
        if target:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(md)
    bad = [r for r in results if r[1] == "FAIL"]
    for c, _, d in bad:
        print(f"::error title=posture::{c} - {d}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
