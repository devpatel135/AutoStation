"""Locate wrapper_core (for miniyaml / versions) without requiring it to be installed.

Search order: already importable -> $WRAPPER_CORE_PATH -> ../../wrapper-core (monorepo) -> /opt/wrapper-core (Docker image).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def ensure_wrapper_core() -> None:
    try:
        import wrapper_core  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve().parent
    candidates = [os.environ.get("WRAPPER_CORE_PATH"), here.parent.parent / "wrapper-core", Path("/opt/wrapper-core")]
    for c in candidates:
        if c and (Path(c) / "wrapper_core").is_dir():
            sys.path.insert(0, str(c))
            return
    raise ImportError("wrapper_core not found: set WRAPPER_CORE_PATH to the directory containing wrapper_core/")


ensure_wrapper_core()
from wrapper_core import miniyaml, versions  # noqa: E402,F401
