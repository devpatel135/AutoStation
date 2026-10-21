#!/usr/bin/env python3
"""Pin every `uses: owner/repo@<tag>` in .github/workflows to the commit SHA the tag points at (keeps the tag as a comment).

    python tools/pin_actions.py [--check]

A moving tag can be re-pointed by whoever controls the action's repo; a 40-hex SHA cannot. Dependabot (github-actions ecosystem)
keeps the pins current. --check exits 1 if any `uses:` is not SHA-pinned (CI runs this).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
USES = re.compile(r"^(?P<pre>\s*-?\s*uses:\s*)(?P<repo>[\w.-]+/[\w./-]+)@(?P<ref>[^\s#]+)(?P<rest>.*)$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def resolve(repo: str, ref: str, cache: dict) -> str:
    key = (repo, ref)
    if key not in cache:
        base = "/".join(repo.split("/")[:2])
        out = subprocess.run(["git", "ls-remote", f"https://github.com/{base}.git", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"],
                             capture_output=True, text=True, timeout=60, check=True).stdout.split("\n")
        shas = {line.split("\t")[1]: line.split("\t")[0] for line in out if "\t" in line}
        sha = shas.get(f"refs/tags/{ref}^{{}}") or shas.get(f"refs/tags/{ref}")
        if not sha:
            raise SystemExit(f"cannot resolve {repo}@{ref}")
        cache[key] = sha
    return cache[key]


def main() -> int:
    check = "--check" in sys.argv
    cache: dict = {}
    bad = 0
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        lines = wf.read_text(encoding="utf-8").split("\n")
        changed = False
        for i, line in enumerate(lines):
            m = USES.match(line)
            if not m or m["repo"].startswith("./") or SHA.match(m["ref"]):
                continue
            if check:
                print(f"{wf.name}:{i + 1}: not pinned: {m['repo']}@{m['ref']}")
                bad += 1
                continue
            lines[i] = f"{m['pre']}{m['repo']}@{resolve(m['repo'], m['ref'], cache)} # {m['ref']}"
            changed = True
        if changed:
            wf.write_text("\n".join(lines), encoding="utf-8", newline="\n")
            print(f"pinned {wf.name}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
