"""AUTOSTATION registry tooling: manifest operations, validation, portal + admin panel generator.

The shared manifest model lives in ``wrapper_core`` (the same code the endpoints use). This package finds it via
``PYTHONPATH`` or, inside the monorepo, ``../wrapper-core``.
"""
from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.1.0"


def _ensure_wrapper_core() -> None:
    try:
        import wrapper_core  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / "wrapper-core", here.parents[1] / "vendor" / "wrapper-core"):
        if (cand / "wrapper_core").is_dir():
            sys.path.insert(0, str(cand))
            return


_ensure_wrapper_core()
