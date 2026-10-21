#!/usr/bin/env python3
"""Human-readable release notes: what changed since the previous published build.

    python tools/release_notes.py --out notes.md [--prev-body-file old-release-body.md] [--head HEAD] [--repo owner/name]
                                  [--installer-sha256 HEX] [--installer-name NAME]

* commits since the last build, grouped by conventional-style subject (feat, fix, security, perf, ci, docs, ...); merge commits and the
  release bot's own manifest commits are folded away
* a manifest diff: new/changed component and automation versions, ring moves, status changes (pauses/resumes)
* the base commit is remembered in the notes as an HTML comment (`<!-- notes-base:<sha> -->`), so the next build starts exactly there
Commit subjects are written by people with push access but are still sanitised (no markup, mentions or issue references).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "components" / "wrapper-core"))

from ops_issues import sanitize  # noqa: E402
from wrapper_core import miniyaml  # noqa: E402

MANIFEST = "components/automation-registry/manifest.yaml"
BASE_RE = re.compile(r"<!-- notes-base:([0-9a-f]{40}) -->")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SUBJECT_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]{1,40})\))?(?P<bang>!)?:\s*(?P<desc>.+)$")
GROUPS = [("security", "Security"), ("feat", "Features"), ("fix", "Fixes"), ("perf", "Performance"), ("deps", "Dependencies"),
          ("ci", "Pipelines"), ("build", "Build"), ("docs", "Documentation"), ("refactor", "Refactoring"), ("test", "Tests"), ("chore", "Maintenance"),
          ("other", "Other changes")]
KNOWN = {k for k, _ in GROUPS}
MAX_PER_GROUP = 25


def parse_subject(subject: str) -> Dict[str, Any]:
    s = (subject or "").strip()
    m = SUBJECT_RE.match(s)
    if m and m["type"] in KNOWN | {"registry"}:
        return {"type": m["type"], "scope": m["scope"] or "", "desc": m["desc"].strip(), "breaking": bool(m["bang"])}
    return {"type": "other", "scope": "", "desc": s, "breaking": False}


def is_noise(subject: str) -> bool:
    s = (subject or "").strip().lower()
    return (not s or s.startswith("merge ") or s.startswith("revert \"revert") or "[skip ci]" in s
            or (s.startswith("registry:") and "release" in s and "rings" in s))


def extract_base(body: Optional[str]) -> Optional[str]:
    m = BASE_RE.search(body or "")
    return m.group(1) if m else None


def group_commits(commits: List[Dict[str, str]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for c in commits:
        if is_noise(c.get("subject", "")):
            continue
        p = parse_subject(c["subject"])
        key = p["type"] if p["type"] in KNOWN else "other"
        out.setdefault(key, []).append(dict(p, sha=c.get("sha", "")))
    return out


def _load(text: Optional[str]) -> Dict[str, Any]:
    if not text:
        return {}
    try:
        doc = miniyaml.loads(text)
    except Exception:  # noqa: BLE001  (a manifest that does not parse yields no diff, never a crash)
        return {}
    return doc if isinstance(doc, dict) else {}


def manifest_diff(old_text: Optional[str], new_text: Optional[str]) -> List[str]:
    old, new = _load(old_text), _load(new_text)
    lines: List[str] = []
    for kind, label in (("components", "component"), ("automations", "automation")):
        o, n = old.get(kind) or {}, new.get(kind) or {}
        if not isinstance(o, dict) or not isinstance(n, dict):
            continue
        for name in sorted(set(o) | set(n)):
            safe = sanitize(name, 60)
            if name not in o:
                lines.append(f"New {label} **{safe}**" + _rings(n[name]))
                continue
            if name not in n:
                lines.append(f"Removed {label} **{safe}**")
                continue
            ov, nv = _versions(o[name]), _versions(n[name])
            for v in sorted(set(nv) - set(ov)):
                lines.append(f"{label.capitalize()} **{safe}** {sanitize(v, 30)} published")
            orings, nrings = _ring_map(o[name]), _ring_map(n[name])
            for ring in ("dev", "pilot", "broad"):
                if orings.get(ring) != nrings.get(ring) and nrings.get(ring):
                    lines.append(f"**{safe}**: {ring} ring {sanitize(orings.get(ring) or 'none', 30)} to {sanitize(nrings[ring], 30)}")
            if isinstance(o[name], dict) and isinstance(n[name], dict) and o[name].get("status") != n[name].get("status") and n[name].get("status"):
                lines.append(f"**{safe}** status {sanitize(o[name].get('status') or 'unset', 20)} to {sanitize(n[name]['status'], 20)}")
    of, nf = old.get("fleet") or {}, new.get("fleet") or {}
    if isinstance(of, dict) and isinstance(nf, dict) and bool(of.get("paused")) != bool(nf.get("paused")):
        lines.append("Fleet " + ("PAUSED" if nf.get("paused") else "resumed"))
    return lines


def _versions(entry: Any) -> Dict[str, Any]:
    v = entry.get("versions") if isinstance(entry, dict) else None
    return {str(k): x for k, x in v.items()} if isinstance(v, dict) else {}


def _ring_map(entry: Any) -> Dict[str, str]:
    r = entry.get("rings") if isinstance(entry, dict) else None
    return {str(k): str(x) for k, x in r.items()} if isinstance(r, dict) else {}


def _rings(entry: Any) -> str:
    r = _ring_map(entry)
    return " (" + ", ".join(f"{k} {sanitize(v, 30)}" for k, v in sorted(r.items())) + ")" if r else ""


def render_notes(commits: List[Dict[str, str]], diff: List[str], head: str, base: Optional[str], repo: str = "",
                 installer_name: str = "", installer_sha256: str = "") -> str:
    lines = ["Rolling release rebuilt on every change. The stable file name always points at the latest build; SHA-256 files and a build-provenance "
             "attestation sit next to each installer (`gh attestation verify <file> --repo " + (repo or "<owner>/<repo>") + "`).",
             "Unsigned unless a certificate is configured: Windows SmartScreen shows 'Windows protected your PC' - More info - Run anyway "
             "(check the SHA-256 first). Per-user install, no admin rights, nothing to install first.", ""]
    if installer_name and re.fullmatch(r"[0-9a-f]{64}", installer_sha256 or ""):
        lines += [f"`{sanitize(installer_name, 80)}` SHA-256: `{installer_sha256}`", ""]
    if diff:
        lines += ["## Registry", ""] + [f"- {d}" for d in diff[:60]] + [""]
    groups = group_commits(commits)
    if groups:
        lines += ["## Changes", ""]
        for key, title in GROUPS:
            items = groups.get(key) or []
            if not items:
                continue
            lines.append(f"### {title}")
            for it in items[:MAX_PER_GROUP]:
                scope = f"**{sanitize(it['scope'], 40)}**: " if it["scope"] else ""
                short = f" ({it['sha'][:7]})" if SHA_RE.match(it.get("sha", "")) else ""
                lines.append(f"- {'**BREAKING** ' if it['breaking'] else ''}{scope}{sanitize(it['desc'], 140)}{short}")
            if len(items) > MAX_PER_GROUP:
                lines.append(f"- ... and {len(items) - MAX_PER_GROUP} more")
            lines.append("")
    elif not diff:
        lines += ["No functional changes since the previous build.", ""]
    if SHA_RE.match(head or ""):
        lines.append(f"<!-- notes-base:{head} -->")
    return "\n".join(lines).rstrip() + "\n"


# ---- git glue ---------------------------------------------------------------------------------------------------------------------
def git(*args: str, cwd: Optional[Path] = None) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd or ROOT), capture_output=True, text=True, timeout=60)
    return r.stdout if r.returncode == 0 else ""


def resolve(ref: str) -> Optional[str]:
    out = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    return out if SHA_RE.match(out) else None


def collect(base: Optional[str], head: str, limit: int = 300) -> Tuple[List[Dict[str, str]], Optional[str], Optional[str]]:
    usable = bool(base) and resolve(base) is not None and subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, head], cwd=str(ROOT), capture_output=True, timeout=60).returncode == 0
    log = git("log", f"--max-count={limit}", "--format=%H%x1f%s", f"{base}..{head}" if usable else head)
    commits = []
    for line in log.splitlines():
        sha, _, subject = line.partition("\x1f")
        if SHA_RE.match(sha):
            commits.append({"sha": sha, "subject": subject})
    old = git("show", f"{base}:{MANIFEST}") if usable else None
    new = git("show", f"{head}:{MANIFEST}")
    return commits, old or None, new or None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--prev-body-file", default="")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--repo", default="")
    ap.add_argument("--installer-name", default="")
    ap.add_argument("--installer-sha256", default="")
    a = ap.parse_args(argv)
    head = resolve(a.head)
    if not head:
        print(f"cannot resolve {a.head}", file=sys.stderr)
        return 2
    prev = Path(a.prev_body_file).read_text(encoding="utf-8") if a.prev_body_file and Path(a.prev_body_file).is_file() else ""
    base = extract_base(prev)
    commits, old, new = collect(base, head)
    notes = render_notes(commits, manifest_diff(old, new), head, base, a.repo, a.installer_name, a.installer_sha256)
    Path(a.out).write_text(notes, encoding="utf-8")
    print(f"wrote {a.out}: {len(commits)} commit(s) since {base[:7] if base else 'the beginning (last ' + str(len(commits)) + ')'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
