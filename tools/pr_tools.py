#!/usr/bin/env python3
"""Pull-request triage without third-party actions: path labels, a size label and ONE sticky comment (promotion checklist, risk notes).

    python tools/pr_tools.py triage --pr 12 [--repo owner/name] [--dry-run]     (GITHUB_TOKEN: pull-requests write)
    python tools/pr_tools.py plan --paths-file changed.txt --additions 10 --deletions 2     (offline: print labels + comment)

Changed files come from the REST API (never from a checkout of the PR branch), so a hostile PR cannot run anything here.
On a fork PR the token is read-only: the writes fail with a notice and the job still passes - labels are a convenience, not a gate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_issues import GitHub, sanitize  # noqa: E402

MARKER = "<!-- autostation-pr-bot -->"
AREAS: List[Tuple[str, str]] = [
    ("components/wrapper-core/", "area:wrapper-core"), ("components/execution-framework/", "area:framework"),
    ("components/automation-builder/", "area:installer"), ("components/automation-logging/", "area:logging"),
    ("components/automation-registry/manifest.yaml", "registry-change"), ("components/automation-registry/", "area:registry"),
    ("components/automations/", "area:automation"), (".github/", "area:pipelines"), ("docs/", "area:docs"),
    ("tools/site/", "area:site"), ("tools/build_site.py", "area:site"), ("tools/", "area:tools"),
]
SENSITIVE = ("components/wrapper-core/wrapper_core/secrets.py", "components/wrapper-core/wrapper_core/selfheal.py",
             "components/wrapper-core/wrapper_core/transport.py", "components/wrapper-core/wrapper_core/trust.py",
             "components/automation-builder/installer/", "components/automation-builder/scripts/", ".github/",
             "tools/build_registry.py", "tools/sign_registry.py", "tools/harden_repo.py", "tools/setup_live.py")
SIZES = [(10, "size:XS"), (50, "size:S"), (250, "size:M"), (1000, "size:L")]
MANIFEST = "components/automation-registry/manifest.yaml"
PATH_RE = re.compile(r"^[A-Za-z0-9_./@+ -]{1,300}$")
MANAGED_PREFIXES = ("area:", "size:", "registry-change", "security-sensitive", "needs-promotion-review")
LABEL_COLORS = {"registry-change": "5319e7", "security-sensitive": "b60205", "needs-promotion-review": "d93f0b"}


def clean_paths(paths: Iterable[str]) -> List[str]:
    """Normalised changed paths. A name with unusual characters is KEPT (dropping it would hide a sensitive file from the labels and
    the review checklist); it is only ever shown through ``display``."""
    return sorted({p for p in (str(x).strip() for x in paths) if p and len(p) <= 300 and ".." not in p.split("/")})


def display(path: str, limit: int = 120) -> str:
    """A path made safe to show inside a markdown code span."""
    return sanitize(path, limit, markup=False).replace("`", "'")


def labels_for(paths: Iterable[str], changed_lines: int) -> List[str]:
    paths = clean_paths(paths)
    labels = set()
    for p in paths:
        for prefix, label in AREAS:
            if p == prefix or p.startswith(prefix):
                labels.add(label)
                break
        if any(p == s or p.startswith(s) for s in SENSITIVE):
            labels.add("security-sensitive")
    if MANIFEST in paths:
        labels.add("needs-promotion-review")
    labels.add(next((name for limit, name in SIZES if changed_lines < limit), "size:XL"))
    return sorted(labels)


def render_comment(paths: Iterable[str], additions: int, deletions: int, labels: List[str]) -> str:
    paths = clean_paths(paths)
    total = max(0, int(additions)) + max(0, int(deletions))
    lines = [MARKER, "### PR triage (automatic)", "",
             f"**Size** {total} changed line(s) across {len(paths)} file(s) - `{next((l for l in labels if l.startswith('size:')), 'size:?')}`"]
    areas = [l for l in labels if l.startswith("area:")]
    if areas:
        lines.append("**Areas** " + ", ".join(f"`{a}`" for a in areas))
    if total >= 1000:
        lines.append("\nThis is a very large change. Consider splitting it so each part can be reviewed (and rolled back) on its own.")
    sensitive = [p for p in paths if any(p == s or p.startswith(s) for s in SENSITIVE)]
    if sensitive:
        lines += ["", "**Security-sensitive files touched** - CODEOWNERS review is required (what runs on endpoints, how it is released, how keys are handled):"]
        lines += [f"- `{display(p)}`" for p in sensitive[:15]]
        if len(sensitive) > 15:
            lines.append(f"- ... and {len(sensitive) - 15} more")
    if MANIFEST in paths:
        lines += ["", "**Registry change - promotion checklist** (docs/GOVERNANCE.md):",
                  "- [ ] Every changed ring points at a version that exists in `versions:` with a sha256",
                  "- [ ] dev to pilot: `assist review` has no HIGH findings, lifecycle test passed, installed on a test machine and the first run looks right",
                  "- [ ] pilot to broad: at least 5 consecutive pilot runs with no FAILED / HUNG / MISSED signals (link the dashboard)",
                  "- [ ] Rollback plan: the previous version is still listed and the `ops-action` rollback was rehearsed",
                  "- [ ] Pause/resume reasons are single-line and safe to show to users"]
    if any(p.startswith(".github/workflows/") for p in paths):
        lines += ["", "Workflow files changed: every `uses:` must stay pinned to a commit SHA (`python tools/pin_actions.py`), jobs need `timeout-minutes`, "
                  "and inputs must reach shells through `env:`. `python tools/check_workflows.py` enforces this."]
    lines += ["", "<sub>Maintained by `tools/pr_tools.py`; edit freely, it rewrites only this comment.</sub>"]
    return "\n".join(lines)


def list_pr_files(gh: GitHub, pr: int, max_pages: int = 10) -> Tuple[List[str], int, int]:
    names: List[str] = []
    adds = dels = 0
    for page in range(1, max_pages + 1):
        code, data = gh.request("GET", f"/repos/{gh.repo}/pulls/{int(pr)}/files?per_page=100&page={page}")
        if code != 200 or not isinstance(data, list):
            raise RuntimeError(f"listing PR files failed (HTTP {code})")
        for f in data:
            names.append(str(f.get("filename", "")))
            if f.get("previous_filename"):                      # a rename OUT of a sensitive directory must still count as touching it
                names.append(str(f["previous_filename"]))
            adds += int(f.get("additions") or 0)
            dels += int(f.get("deletions") or 0)
        if len(data) < 100:
            break
    return names, adds, dels


def apply_labels(gh: GitHub, pr: int, wanted: List[str]) -> None:
    code, cur = gh.request("GET", f"/repos/{gh.repo}/issues/{int(pr)}/labels?per_page=100")
    current = [l["name"] for l in cur] if code == 200 and isinstance(cur, list) else []
    stale = [n for n in current if n.startswith(MANAGED_PREFIXES) and n not in wanted]
    for name in stale:
        gh.request("DELETE", f"/repos/{gh.repo}/issues/{int(pr)}/labels/{name.replace(':', '%3A')}")
    for name in wanted:
        gh.request("POST", f"/repos/{gh.repo}/labels", {"name": name, "color": LABEL_COLORS.get(name, "0e8a16" if name.startswith("size:") else "ededed")})
    code, _ = gh.request("POST", f"/repos/{gh.repo}/issues/{int(pr)}/labels", {"labels": wanted})
    if code not in (200, 201):
        raise RuntimeError(f"labelling failed (HTTP {code})")


def find_bot_comment(gh: GitHub, pr: int, max_pages: int = 20) -> Optional[dict]:
    """The sticky comment: carries the marker AND was written by a bot account (a user pasting the marker cannot hijack it)."""
    for page in range(1, max_pages + 1):
        code, data = gh.request("GET", f"/repos/{gh.repo}/issues/{int(pr)}/comments?per_page=100&page={page}")
        if code != 200 or not isinstance(data, list):
            return None
        for c in data:
            if MARKER in str(c.get("body", "")) and (c.get("user") or {}).get("type") == "Bot":
                return c
        if len(data) < 100:
            return None
    return None


def upsert_comment(gh: GitHub, pr: int, body: str) -> None:
    mine = find_bot_comment(gh, pr)
    if mine:
        code, _ = gh.request("PATCH", f"/repos/{gh.repo}/issues/comments/{int(mine['id'])}", {"body": body})
    else:
        code, _ = gh.request("POST", f"/repos/{gh.repo}/issues/{int(pr)}/comments", {"body": body})
    if code not in (200, 201):
        raise RuntimeError(f"comment failed (HTTP {code})")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("triage")
    t.add_argument("--pr", type=int, required=True)
    t.add_argument("--repo", default="")
    t.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("plan")
    p.add_argument("--paths-file", required=True)
    p.add_argument("--additions", type=int, default=0)
    p.add_argument("--deletions", type=int, default=0)
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        paths = Path(a.paths_file).read_text(encoding="utf-8").splitlines()
        labels = labels_for(paths, a.additions + a.deletions)
        print(json.dumps({"labels": labels}))
        print(render_comment(paths, a.additions, a.deletions, labels))
        return 0
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = a.repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN is not set")
    gh = GitHub(repo, token)
    names, adds, dels = list_pr_files(gh, a.pr)
    labels = labels_for(names, adds + dels)
    body = render_comment(names, adds, dels, labels)
    print(f"labels: {', '.join(labels)}")
    if a.dry_run:
        print(body)
        return 0
    try:
        apply_labels(gh, a.pr, labels)
        upsert_comment(gh, a.pr, body)
    except RuntimeError as exc:
        print(f"::notice::could not write labels or the comment ({exc}); this is normal for pull requests from forks (read-only token)")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### PR triage\n\nlabels: {', '.join(labels)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
