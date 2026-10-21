"""Asset overrides (UiPath Orchestrator assets with per-robot / per-user values), non-secret configuration only.

Precedence, lowest to highest (each layer only overrides the keys it sets):

    1. ring default      ``config/assets.json``  (written by self-heal from the registry manifest ``assets:`` block; never edit by hand)
    2. machine override  ``%ProgramData%\\AUTOMAGIX\\assets.json`` (admin-managed, e.g. Intune), or ``$AUTOMAGIX_MACHINE_ASSETS``
    3. user override     ``config/assets.user.json``  (this user's own tweaks)
    4. slot parameters   ``asset_<name>`` parameters of the schedule slot that started the run (typed to match the ring default)
    5. dev env override  ``AMX_ASSET_<NAME>`` (handled by ``credentials.get_asset``; development only)

Overrides are validated hard because they are an input channel: names ``[A-Za-z][A-Za-z0-9_.-]{0,63}``, values are scalars (strings up to
4096 characters) or small lists/dicts of scalars (depth <= 3), files <= 256 KB, and **secrets are refused** - names such as password / token /
secret / api key / credential and values that look like keys, tokens or URLs with passwords are dropped with a problem report. Use
``activities.credentials`` (Windows Credential Manager) for secrets. When a ring default exists, an override must have the same type
(``int`` may become ``float``); a mismatch is dropped, not coerced silently.

    from wrapper_core import assetconf
    values, sources = assetconf.effective_assets(with_sources=True)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .paths import Paths, get_logger

MAX_FILE_BYTES = 256 * 1024
MAX_STR = 4096
MAX_ITEMS = 200
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_SECRET_NAME = re.compile(r"(?i)(^|[_.\-])(pass(word|wd|phrase)?|secret|token|api[_-]?key|apikey|private[_-]?key|credential|credentials|bearer|auth)([_.\-]|$)")
_SECRET_VALUE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|"
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|://[^/\s:@]+:[^/\s@]+@")


def machine_assets_path() -> Optional[Path]:
    env = os.environ.get("AUTOMAGIX_MACHINE_ASSETS")
    if env:
        return Path(env)
    pd = os.environ.get("ProgramData")
    return Path(pd) / "AUTOMAGIX" / "assets.json" if pd else None


def user_assets_path(paths: Optional[Paths] = None) -> Path:
    return (paths or Paths()).config / "assets.user.json"


def _kind(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return "other"


def _value_problems(v: Any, depth: int = 0) -> List[str]:
    if v is None or isinstance(v, (bool, int, float)):
        return []
    if isinstance(v, str):
        if len(v) > MAX_STR:
            return [f"string longer than {MAX_STR} characters"]
        if _SECRET_VALUE.search(v):
            return ["value looks like a secret (key, token or URL with a password)"]
        return []
    if depth >= 3:
        return ["nesting deeper than 3 levels"]
    if isinstance(v, list):
        if len(v) > MAX_ITEMS:
            return [f"list longer than {MAX_ITEMS} items"]
        return [p for x in v for p in _value_problems(x, depth + 1)][:3]
    if isinstance(v, dict):
        if len(v) > MAX_ITEMS:
            return [f"mapping with more than {MAX_ITEMS} keys"]
        out: List[str] = []
        for k, x in v.items():
            if not isinstance(k, str) or len(k) > 128:
                out.append("mapping keys must be short strings")
            elif _SECRET_NAME.search(k):
                out.append(f"nested key {k!r} looks like a secret")
            out.extend(_value_problems(x, depth + 1))
        return out[:3]
    return ["unsupported value type"]


def validate_assets(values: Any, base: Optional[dict] = None) -> Tuple[Dict[str, Any], List[str]]:
    """Return ``(clean, problems)``: ``clean`` holds only the entries that passed; ``problems`` explains every dropped one."""
    problems: List[str] = []
    if not isinstance(values, dict):
        return {}, ["assets must be a JSON object of name -> value"]
    clean: Dict[str, Any] = {}
    for name, v in values.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            problems.append(f"asset name {str(name)[:40]!r} is invalid (letters, digits, '_', '.', '-'; must start with a letter; max 64)")
            continue
        if _SECRET_NAME.search(name):
            problems.append(f"asset {name!r} looks like a secret: use activities.credentials, not assets")
            continue
        bad = _value_problems(v)
        if bad:
            problems.append(f"asset {name!r}: {bad[0]}")
            continue
        if base is not None and name in base and base[name] is not None and v is not None:
            bk, vk = _kind(base[name]), _kind(v)
            if bk != vk and not (bk == "float" and vk == "int"):
                problems.append(f"asset {name!r}: override is {vk} but the ring default is {bk}")
                continue
        clean[name] = v
    return clean, problems


def _read_layer(path: Optional[Path], label: str, base: dict, problems: List[str]) -> Dict[str, Any]:
    if path is None:
        return {}
    try:
        if not path.is_file():
            return {}
        if path.stat().st_size > MAX_FILE_BYTES:
            problems.append(f"{label}: file larger than {MAX_FILE_BYTES} bytes ignored")
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        problems.append(f"{label}: unreadable or not valid JSON (ignored)")
        return {}
    clean, probs = validate_assets(data, base)
    problems.extend(f"{label}: {p}" for p in probs)
    return clean


def _coerce_param(raw: str, like: Any) -> Any:
    """Slot parameters arrive as strings: convert to the type of the ring default (None if it cannot be done safely)."""
    k = _kind(like) if like is not None else "str"
    s = str(raw)
    if k == "bool":
        low = s.strip().lower()
        if low in ("true", "1", "yes", "y", "on"):
            return True
        if low in ("false", "0", "no", "n", "off"):
            return False
        raise ValueError("not a boolean")
    if k == "int":
        return int(s.strip())
    if k == "float":
        return float(s.strip())
    if k in ("list", "dict"):
        v = json.loads(s)
        if _kind(v) != k:
            raise ValueError(f"not a JSON {k}")
        return v
    return s


def effective_assets(paths: Optional[Paths] = None, params: Optional[Dict[str, str]] = None, with_sources: bool = False):
    """The merged assets for this run (see the module docstring for precedence). Never raises: bad layers are dropped and logged.

    ``with_sources=True`` returns ``(values, {name: "ring"|"machine"|"user"|"param"})`` for diagnostics (``wrapper_core assets show``)."""
    paths = paths or Paths()
    problems: List[str] = []
    try:
        raw = json.loads(paths.assets_json.read_text(encoding="utf-8")) if paths.assets_json.is_file() else {}
        base = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        base = {}
    values: Dict[str, Any] = dict(base)
    sources: Dict[str, str] = {k: "ring" for k in base}
    for label, path in (("machine", machine_assets_path()), ("user", user_assets_path(paths))):
        layer = _read_layer(path, f"{label} assets", base, problems)
        values.update(layer)
        sources.update({k: label for k in layer})
    if params is None:
        from . import params as _params
        params = _params()
    for key, raw_v in (params or {}).items():
        if not str(key).startswith("asset_"):
            continue
        name = str(key)[len("asset_"):]
        match = next((n for n in list(base) + list(values) if n.lower() == name.lower()), name)  # params are lower-cased by the runner
        clean, probs = validate_assets({match: raw_v if isinstance(raw_v, str) else str(raw_v)}, None)
        if probs:
            problems.extend(f"slot parameter {key}: {p}" for p in probs)
            continue
        try:
            typed = _coerce_param(str(raw_v), values.get(match, base.get(match)))
        except (ValueError, TypeError) as exc:
            problems.append(f"slot parameter {key}: {exc}")
            continue
        if _value_problems(typed):
            problems.append(f"slot parameter {key}: {_value_problems(typed)[0]}")
            continue
        values[match] = typed
        sources[match] = "param"
    if problems:
        log = get_logger(paths)
        for p in problems[:20]:
            log.warning("asset override ignored: %s", p)
    return (values, sources) if with_sources else values


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m wrapper_core assets show|validate FILE``"""
    import argparse
    ap = argparse.ArgumentParser(prog="wrapper_core assets")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="print the effective assets and which layer each value comes from")
    v = sub.add_parser("validate", help="check an overrides file without installing it")
    v.add_argument("file")
    args = ap.parse_args(argv)
    if args.cmd == "show":
        values, sources = effective_assets(with_sources=True)
        for name in sorted(values):
            print(f"{name} = {json.dumps(values[name])}   [{sources.get(name, '?')}]")
        return 0
    p = Path(args.file)
    if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
        print("file missing or larger than 256 KB")
        return 1
    try:
        clean, problems = validate_assets(json.loads(p.read_text(encoding="utf-8")))
    except ValueError:
        print("not valid JSON")
        return 1
    for pr in problems:
        print("PROBLEM", pr)
    print(f"{len(clean)} usable asset(s), {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
