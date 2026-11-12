#!/usr/bin/env python3
"""Fetch the telemetry the endpoints pushed to the private logging repo into a local directory laid out as ``<out>/ingest/...``,
ready for ``python -m automation_logging ingest --source dir --from <out> --out <archive>``.

Only what analytics reads is fetched: the legacy ``ingest`` branch plus the monthly ``ingest-YYYY-MM`` branches for the last
``--months`` months (depth 1, so history never grows the download). Read-only: nothing is deleted from the logs repo.

  python tools/fetch_logs.py --remote https://github.com/OWNER/REPO-logs.git --out logs-src [--months 2] [--token-env LOGS_TOKEN]

The token is passed to git through an environment-provided header, never on the command line or in the URL.
Exit codes: 0 fetched (possibly nothing yet), 1 git failure.
"""
from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def month_branches(now: datetime, months: int) -> list[str]:
    out, y, m = [], now.year, now.month
    for _ in range(max(1, months)):
        out.append(f"ingest-{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def _git(args: list[str], cwd: Path, token: str | None) -> subprocess.CompletedProcess:
    cmd = ["git"]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
    return subprocess.run(cmd + args, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=600)


def fetch(remote: str, out: Path, months: int = 2, token: str | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    out = Path(out)
    stats = {"branches": [], "missing": [], "files": 0}
    work = Path(tempfile.mkdtemp(prefix="fetch-logs-"))
    try:
        r = _git(["init", "-q"], work, None)
        if r.returncode:
            raise RuntimeError("git init failed")
        for br in ["ingest"] + month_branches(now, months):
            r = _git(["fetch", "-q", "--depth", "1", remote, f"+refs/heads/{br}:refs/remotes/o/{br}"], work, token)
            if r.returncode:
                stats["missing"].append(br)       # a branch that does not exist (yet) is normal
                continue
            dest = work / "wt" / br
            dest.mkdir(parents=True, exist_ok=True)
            r = _git(["--work-tree", str(dest), "checkout", f"refs/remotes/o/{br}", "--", "ingest"], work, None)
            if r.returncode:
                stats["missing"].append(br)
                continue
            stats["branches"].append(br)
            src = dest / "ingest"
            if src.is_dir():
                for p in src.rglob("*"):
                    if p.is_symlink():
                        continue
                    if p.is_file():
                        target = out / "ingest" / p.relative_to(src)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(p, target)
                        stats["files"] += 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--remote", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--months", type=int, default=2)
    ap.add_argument("--token-env", default="LOGS_TOKEN", help="name of the environment variable holding the read token")
    a = ap.parse_args(argv)
    try:
        stats = fetch(a.remote, Path(a.out), a.months, os.environ.get(a.token_env) or None)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"fetch_logs: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"fetch_logs: {stats['files']} file(s) from {len(stats['branches'])} branch(es); absent: {', '.join(stats['missing']) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
