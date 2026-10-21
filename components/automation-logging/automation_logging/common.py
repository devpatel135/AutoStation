"""Shared helpers. Imports wrapper_core (sibling component) and exposes small utilities used everywhere."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import wrapper_core  # noqa: F401
except ImportError:  # monorepo checkout: components/wrapper-core sits next to this component
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "wrapper-core"))
    import wrapper_core  # noqa: F401

from wrapper_core import manifest as wc_manifest  # noqa: E402
from wrapper_core import miniyaml  # noqa: E402
from wrapper_core.util import iso, parse_iso  # noqa: E402

KINDS = ("run", "heartbeat", "lifecycle")
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

__all__ = ["iso", "parse_iso", "utcnow", "fingerprint", "percentile", "read_json", "write_json", "error_fingerprint",
           "load_yaml", "wc_manifest", "miniyaml", "KINDS", "SEVERITY_ORDER", "timedelta", "datetime", "timezone"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fingerprint(*parts, n: int = 16) -> str:
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode("utf-8")).hexdigest()[:n]


_NUM = re.compile(r"0x[0-9a-f]+|[0-9a-f]{8}-[0-9a-f-]{27}|\d+", re.I)


def error_fingerprint(category: str | None, message: str | None) -> str:
    """Stable id for 'the same error': digits, hex ids and uuids in the message are normalised away."""
    norm = _NUM.sub("N", (message or "").lower())[:160]
    return fingerprint(category or "UNKNOWN", norm, n=12)


def percentile(values, p: float):
    """Nearest-rank percentile; None for an empty list."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = max(1, math.ceil(p / 100.0 * len(vals)))
    return vals[min(k, len(vals)) - 1]


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(p)


def load_yaml(path):
    return miniyaml.loads(Path(path).read_text(encoding="utf-8"))
