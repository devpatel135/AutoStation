#!/usr/bin/env python3
"""Make components/execution-framework runnable on a bare Windows machine: bundle a Node runtime and playwright-core.

    python tools/build_framework_bundle.py [--node-version 20.18.1] [--skip-node] [--skip-npm]

In place (the extra files are git-ignored), so the normal packagers (`package.py component execution-framework`,
`tools/build_registry.py`) produce the full, self-contained bundle:

  execution-framework/node/node.exe        official Node.js for Windows x64, SHA-256 verified against nodejs.org's SHASUMS256.txt
  execution-framework/node_modules/        playwright-core (exact version pinned in package.json, zero dependencies) via
                                           `npm install --omit=dev --ignore-scripts` (needs npm on PATH; CI uses actions/setup-node)

No browser is downloaded: automations drive the Edge that ships with Windows (`channel: msedge`).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "components" / "execution-framework"
DEFAULT_NODE = "20.18.1"


def parse_shasums(text: str, filename: str) -> str:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            return parts[0].lower()
    raise ValueError(f"{filename} not listed in SHASUMS256.txt")


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "autostation-build"}), timeout=300) as r:  # noqa: S310
        return r.read()


def install_node(version: str, dest: Path) -> Path:
    base = f"https://nodejs.org/dist/v{version}"
    name = f"node-v{version}-win-x64.zip"
    expected = parse_shasums(fetch(f"{base}/SHASUMS256.txt").decode("utf-8"), name)
    data = fetch(f"{base}/{name}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise SystemExit(f"SHA-256 mismatch for {name}: expected {expected}, got {actual}")
    out = dest / "node"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        member = f"node-v{version}-win-x64/node.exe"
        (out / "node.exe").write_bytes(z.read(member))
        for lic in (f"node-v{version}-win-x64/LICENSE",):
            if lic in z.namelist():
                (out / "LICENSE-node.txt").write_bytes(z.read(lic))
    print(f"node {version} -> {out / 'node.exe'} (sha256 verified {actual[:12]}...)")
    return out / "node.exe"


def install_modules(dest: Path) -> None:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise SystemExit("npm not found on PATH (use actions/setup-node, or pass --skip-npm)")
    subprocess.run([npm, "install", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund", "--no-package-lock"], cwd=str(dest), check=True)
    pkg = json.loads((dest / "package.json").read_text(encoding="utf-8"))
    want = pkg["dependencies"]["playwright-core"]
    got = json.loads((dest / "node_modules" / "playwright-core" / "package.json").read_text(encoding="utf-8"))["version"]
    if got != want:
        raise SystemExit(f"playwright-core {got} installed but package.json pins {want}")
    print(f"playwright-core {got} installed into {dest / 'node_modules'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node-version", default=DEFAULT_NODE)
    ap.add_argument("--skip-node", action="store_true")
    ap.add_argument("--skip-npm", action="store_true")
    ap.add_argument("--dest", default=str(FW))
    a = ap.parse_args()
    dest = Path(a.dest)
    if not a.skip_node:
        install_node(a.node_version, dest)
    if not a.skip_npm:
        install_modules(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
