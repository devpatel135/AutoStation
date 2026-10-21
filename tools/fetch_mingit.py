#!/usr/bin/env python3
"""Download MinGit (git + ssh for Windows, ~30 MB) with a pinned SHA-256, for bundling into installers.

    python tools/fetch_mingit.py --out build/mingit.zip

The pin below is the digest published on the git-for-windows release page. A mismatch aborts: the file is never used.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = "2.56.0.2"
TAG = "v2.56.0.windows.2"
NAME = f"MinGit-{VERSION}-64-bit.zip"
SHA256 = "da35e72aa21c005a5a0d298cfbae110bc1609a815730ea0dde84b01a1b3cd3be"
URL = f"https://github.com/git-for-windows/git/releases/download/{TAG}/{NAME}"


def fetch(out: Path, url: str = URL, sha256: str = SHA256) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": "autostation-build"})
    with urllib.request.urlopen(req, timeout=300) as r:  # noqa: S310
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise SystemExit(f"SHA-256 mismatch for {NAME}: expected {sha256}, got {got}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
    if "cmd/git.exe" not in names or "usr/bin/ssh.exe" not in names:
        raise SystemExit("MinGit archive is missing cmd/git.exe or usr/bin/ssh.exe")
    print(f"{NAME} -> {out} (sha256 verified {got[:12]}...)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    fetch(Path(ap.parse_args().out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
