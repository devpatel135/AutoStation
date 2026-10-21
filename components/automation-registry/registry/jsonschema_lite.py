"""A tiny JSON-Schema (draft 2020-12 subset) validator: no third-party packages are available on CI images.

Supports: ``$ref`` (local ``#/$defs/..``), type, enum, const, pattern, minimum/maximum, minLength/maxLength,
minItems, items, properties, propertyNames, required, patternProperties, additionalProperties (bool or schema), anyOf, oneOf.
``format`` and unknown keywords are ignored.
"""
from __future__ import annotations

import re
from typing import Any

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(instance: Any, schema: dict) -> list[str]:
    errors: list[str] = []
    _check(instance, schema, schema, "$", errors)
    return errors


def _resolve(ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _check(v: Any, s: dict, root: dict, path: str, errs: list[str]) -> None:
    if "$ref" in s:
        _check(v, _resolve(s["$ref"], root), root, path, errs)
        return
    if "anyOf" in s:
        if not any(not _sub(v, alt, root, path) for alt in s["anyOf"]):
            errs.append(f"{path}: does not match any allowed alternative")
        return
    if "oneOf" in s:
        n = sum(1 for alt in s["oneOf"] if not _sub(v, alt, root, path))
        if n != 1:
            errs.append(f"{path}: must match exactly one alternative (matched {n})")
        return
    t = s.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_TYPES[x](v) for x in types):
            errs.append(f"{path}: expected {'|'.join(types)}, got {type(v).__name__}")
            return
    if "const" in s and v != s["const"]:
        errs.append(f"{path}: must equal {s['const']!r}")
    if "enum" in s and v not in s["enum"]:
        errs.append(f"{path}: {v!r} is not one of {s['enum']}")
    if isinstance(v, str):
        if "pattern" in s and not re.search(s["pattern"], v):
            errs.append(f"{path}: {v!r} does not match /{s['pattern']}/")
        if "minLength" in s and len(v) < s["minLength"]:
            errs.append(f"{path}: shorter than {s['minLength']} characters")
        if "maxLength" in s and len(v) > s["maxLength"]:
            errs.append(f"{path}: longer than {s['maxLength']} characters")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in s and v < s["minimum"]:
            errs.append(f"{path}: {v} is below minimum {s['minimum']}")
        if "maximum" in s and v > s["maximum"]:
            errs.append(f"{path}: {v} is above maximum {s['maximum']}")
    if isinstance(v, list):
        if "minItems" in s and len(v) < s["minItems"]:
            errs.append(f"{path}: needs at least {s['minItems']} item(s)")
        if "items" in s:
            for i, item in enumerate(v):
                _check(item, s["items"], root, f"{path}[{i}]", errs)
    if isinstance(v, dict):
        props = s.get("properties", {})
        for req in s.get("required", []):
            if req not in v:
                errs.append(f"{path}: missing required property '{req}'")
        pat_props = s.get("patternProperties", {})
        if "propertyNames" in s:
            for k in v:
                errs.extend(f"{path}.{k} (property name): {e.split(': ', 1)[-1]}" for e in _sub(str(k), s["propertyNames"], root, path))
        for k, val in v.items():
            matched = False
            if k in props:
                matched = True
                _check(val, props[k], root, f"{path}.{k}", errs)
            for pat, sub in pat_props.items():
                if re.search(pat, str(k)):
                    matched = True
                    _check(val, sub, root, f"{path}.{k}", errs)
            if not matched:
                ap = s.get("additionalProperties", True)
                if ap is False:
                    errs.append(f"{path}: unexpected property '{k}'")
                elif isinstance(ap, dict):
                    _check(val, ap, root, f"{path}.{k}", errs)


def _sub(v: Any, s: dict, root: dict, path: str) -> list[str]:
    out: list[str] = []
    _check(v, s, root, path, out)
    return out
