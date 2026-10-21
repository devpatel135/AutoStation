#!/usr/bin/env python3
"""Fail if a credential-shaped string is committed.  python tools/check_secrets.py [paths...]

Scans git-tracked files (or the given paths) for GitHub tokens, private key blocks, cloud keys and long bearer-looking literals.
A line containing ``secret-scan: allow`` is skipped (use for test fixtures that are deliberately fake).
GitHub secret scanning + push protection (tools/harden_repo.py) is the second layer; this one also runs offline and in CI.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = {
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b"),
    "URL with embedded password": re.compile(r"https?://[^/\s:@]+:[^/\s:@${}]{6,}@(?!localhost|127\.0\.0\.1)[\w.-]+"),
}
SKIP_SUFFIX = {".zip", ".exe", ".png", ".ico", ".jpg", ".gif", ".woff", ".woff2", ".lock", ".dpapi"}


def tracked() -> list[Path]:
    # committed files plus new, not-yet-committed ones (not ignored): a local run then sees what CI will see
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / f for f in sorted(set(out.split("\0"))) if f]


def scan_text(text: str, name: str) -> list[str]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if "secret-scan: allow" in line:
            continue
        for label, rx in PATTERNS.items():
            if rx.search(line):
                hits.append(f"{name}:{n}: {label}")
    return hits


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or tracked()
    bad: list[str] = []
    for f in files:
        if f.suffix.lower() in SKIP_SUFFIX or not f.is_file() or f.stat().st_size > 2_000_000:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        bad += scan_text(text, str(f.relative_to(ROOT)) if f.is_absolute() and ROOT in f.parents else str(f))
    for b in bad:
        print("ERROR", b)
    print(f"scanned {len(files)} file(s): {len(bad)} credential-shaped string(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
