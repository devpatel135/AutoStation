#!/usr/bin/env python3
"""Run every component's test suite in its own process (their conftest/sys.path setups must not mix).

    python tools/test_all.py            # everything available on this machine
    python tools/test_all.py --only wrapper-core automation-logging
Exit code is non-zero if any suite fails. Node suites run only if `node` is on PATH (otherwise reported as SKIPPED).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
C = ROOT / "components"

SUITES: list[tuple[str, Path, list[str], str]] = [
    ("wrapper-core", C / "wrapper-core", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], "py"),
    ("automation-logging", C / "automation-logging", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], "py"),
    ("automation-registry", C / "automation-registry", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], "py"),
    ("automation-builder", C / "automation-builder", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], "py"),
    ("execution-framework", C / "execution-framework", ["node", "--test", "test/"], "node"),
    ("automation-hello-world", C / "automations" / "automation-hello-world", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], "py"),
    ("automation-invoice-router", C / "automations" / "automation-invoice-router", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], "py"),
    ("automation-notifier", C / "automations" / "automation-notifier", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], "py"),
    ("automation-portal-report", C / "automations" / "automation-portal-report", ["node", "--test", "test/"], "node"),
    ("tools", ROOT / "tools", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], "py"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    results = []
    for name, cwd, cmd, kind in SUITES:
        if args.only and name not in args.only:
            continue
        if not cwd.exists() or (kind == "py" and not any(cwd.rglob("test_*.py"))) or (kind == "node" and not any(cwd.rglob("*.test.js"))):
            results.append((name, "MISSING", 0.0, ""))
            continue
        if kind == "node" and not shutil.which("node"):
            results.append((name, "SKIPPED (node not installed)", 0.0, ""))
            continue
        t = time.time()
        env = dict(os.environ, PYTHONPATH=str(C / "wrapper-core"))
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
        tail = (r.stdout.strip().splitlines() or r.stderr.strip().splitlines() or [""])[-1]
        results.append((name, "PASS" if r.returncode == 0 else "FAIL", time.time() - t, tail))
        if r.returncode != 0:
            print(f"--- {name} output ---\n{r.stdout[-3000:]}\n{r.stderr[-1500:]}")
            if os.environ.get("GITHUB_ACTIONS"):  # surface the failure as a check annotation (visible without log access)
                import re
                failed = re.findall(r"^(?:FAILED|ERROR) .*$", r.stdout, re.M)[:12]
                body = "\n".join(failed) or (r.stdout[-1500:] + r.stderr[-1000:])
                body = body.replace("%", "%25").replace("\r", "").replace("\n", "%0A")[:3500]
                print(f"::error title={name} failed::{body}")
    print()
    for name, status, secs, tail in results:
        print(f"{status:<28} {name:<28} {secs:6.1f}s  {tail}")
    return 1 if any(s == "FAIL" for _, s, _, _ in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
