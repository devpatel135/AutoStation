#!/usr/bin/env python3
"""Stage a Docker build context that works from the monorepo AND from a split-out automation-builder project.

    make_context.py <out-dir>          # then: docker build -f <out>/builder/docker/Dockerfile <out>

Layout produced (what the Dockerfile COPYs):
    <out>/builder/{scripts,installer,prompts,docker}/...
    <out>/wrapper-core/{wrapper_core/,VERSION}

wrapper-core is taken from $WRAPPER_CORE_PATH or ../wrapper-core (monorepo). In a split project CI downloads it first
(`ci_installer_config.py wrapper-core --out DIR`) and points WRAPPER_CORE_PATH at it.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILDER = HERE.parent
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".git")


def find_wrapper_core() -> Path:
    for cand in (os.environ.get("WRAPPER_CORE_PATH"), BUILDER.parent / "wrapper-core"):
        if cand and (Path(cand) / "wrapper_core").is_dir():
            return Path(cand)
    raise SystemExit("make_context.py: wrapper-core not found (set WRAPPER_CORE_PATH to the dir containing wrapper_core/)")


def make_context(out: Path) -> Path:
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    wc = find_wrapper_core()
    for sub in ("scripts", "installer", "prompts", "docker"):
        shutil.copytree(BUILDER / sub, out / "builder" / sub, ignore=IGNORE)
    shutil.copytree(wc / "wrapper_core", out / "wrapper-core" / "wrapper_core", ignore=IGNORE)
    version_file = wc / "VERSION"
    version = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else "0.0.0"
    (out / "wrapper-core" / "VERSION").write_text(version + "\n", encoding="utf-8")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(make_context(Path(sys.argv[1])))
