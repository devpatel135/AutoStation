"""Minimal SemVer parsing and range matching (`>=0.1.0,<1.0.0`)."""
from __future__ import annotations

import re

_V = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?\s*$")


def parse(version: str) -> tuple:
    m = _V.match(str(version))
    if not m:
        raise ValueError(f"invalid version: {version!r}")
    major, minor, patch = (int(x or 0) for x in m.group(1, 2, 3))
    pre = m.group(4)
    # a pre-release sorts BEFORE the release: (1,0,0,0,'rc1') < (1,0,0,1,'')
    return (major, minor, patch, 0 if pre else 1, pre or "")


def compare(a: str, b: str) -> int:
    pa, pb = parse(a), parse(b)
    return (pa > pb) - (pa < pb)


_OPS = {
    ">=": lambda c: c >= 0,
    "<=": lambda c: c <= 0,
    "==": lambda c: c == 0,
    "!=": lambda c: c != 0,
    ">": lambda c: c > 0,
    "<": lambda c: c < 0,
}


def satisfies(version: str, spec: str | None) -> bool:
    """True if `version` satisfies every comma-separated constraint in `spec`."""
    if not spec or not str(spec).strip() or str(spec).strip() == "*":
        return True
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        for op in (">=", "<=", "==", "!=", ">", "<"):
            if part.startswith(op):
                if not _OPS[op](compare(version, part[len(op):].strip())):
                    return False
                break
        else:
            if compare(version, part) != 0:  # bare version == exact
                return False
    return True


def validate_spec(spec: str) -> None:
    """Raise ValueError if `spec` is not a parseable constraint string."""
    satisfies("0.0.0", spec)
