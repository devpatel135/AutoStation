"""Schema validation for structured LLM output with zero required dependencies.

Accepted ``schema`` forms:

* a pydantic v2 model class (used if pydantic is importable)
* a JSON-Schema subset dict: type, properties, required, items, enum, anyOf, minimum, maximum, minLength,
  maxLength, additionalProperties
* a *shape* dict, e.g. ``{"vendor": "string", "amount": "number", "tags": ["string"], "kind": "a|b|c"}``;
  append ``?`` to a type name for nullable (``"string?"``). ``{"type": "json_object"}`` means any object.
"""
from __future__ import annotations

import json
from typing import Any

_JSON_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_JS_KEYWORDS = {"type", "properties", "required", "items", "enum", "anyOf", "minimum", "maximum", "minLength",
                "maxLength", "additionalProperties", "description", "title", "$schema", "default", "minItems"}


def is_pydantic(schema: Any) -> bool:
    return hasattr(schema, "model_validate") and hasattr(schema, "model_json_schema")


def is_json_schema(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    if "properties" in schema or "$schema" in schema or "anyOf" in schema:
        return True
    return schema.get("type") in _JSON_TYPES and set(schema) <= _JS_KEYWORDS


def to_json_schema(schema: Any) -> dict:
    if is_pydantic(schema):
        return schema.model_json_schema()
    if is_json_schema(schema):
        return schema
    if schema == {"type": "json_object"}:
        return {"type": "object"}
    return {"type": "object", "properties": {k: _shape_to_js(v) for k, v in schema.items()},
            "required": [k for k, v in schema.items() if not (isinstance(v, str) and v.endswith("?"))]}


def _shape_to_js(v: Any) -> dict:
    if isinstance(v, dict):
        return to_json_schema(v)
    if isinstance(v, list):
        return {"type": "array", "items": _shape_to_js(v[0]) if v else {}}
    if isinstance(v, str):
        nullable = v.endswith("?")
        t = v[:-1] if nullable else v
        if "|" in t:
            js: dict = {"enum": t.split("|")}
        elif t == "any":
            js = {}
        elif t in _JSON_TYPES:
            js = {"type": t}
        else:
            raise ValueError(f"unknown shape type {v!r}")
        if nullable:
            return {"anyOf": [js, {"type": "null"}]}
        return js
    raise ValueError(f"unsupported shape element {v!r}")


def describe(schema: Any) -> str:
    """JSON text to show the model."""
    return json.dumps(to_json_schema(schema), separators=(",", ":"))


def validate(value: Any, schema: Any) -> tuple[bool, str]:
    """Return (ok, error_message). Never raises for bad data."""
    if is_pydantic(schema):
        try:
            schema.model_validate(value)
            return True, ""
        except Exception as exc:  # pydantic.ValidationError
            return False, str(exc)[:500]
    errs: list[str] = []
    _check(value, to_json_schema(schema), "$", errs)
    return (not errs), "; ".join(errs[:5])


def _type_ok(v: Any, t: str) -> bool:
    if t == "object":
        return isinstance(v, dict)
    if t == "array":
        return isinstance(v, list)
    if t == "string":
        return isinstance(v, str)
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "null":
        return v is None
    return True


def _check(v: Any, s: dict, path: str, errs: list[str]) -> None:
    if "anyOf" in s:
        for alt in s["anyOf"]:
            sub: list[str] = []
            _check(v, alt, path, sub)
            if not sub:
                return
        errs.append(f"{path}: matches none of the allowed alternatives")
        return
    t = s.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(v, x) for x in types):
            errs.append(f"{path}: expected {'|'.join(types)}, got {type(v).__name__}")
            return
    if "enum" in s and v not in s["enum"]:
        errs.append(f"{path}: {v!r} not in {s['enum']}")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in s and v < s["minimum"]:
            errs.append(f"{path}: {v} < minimum {s['minimum']}")
        if "maximum" in s and v > s["maximum"]:
            errs.append(f"{path}: {v} > maximum {s['maximum']}")
    if isinstance(v, str):
        if "minLength" in s and len(v) < s["minLength"]:
            errs.append(f"{path}: shorter than {s['minLength']}")
        if "maxLength" in s and len(v) > s["maxLength"]:
            errs.append(f"{path}: longer than {s['maxLength']}")
    if isinstance(v, dict):
        props = s.get("properties", {})
        for req in s.get("required", []):
            if req not in v:
                errs.append(f"{path}: missing required '{req}'")
        for k, sub_schema in props.items():
            if k in v:
                _check(v[k], sub_schema, f"{path}.{k}", errs)
        if s.get("additionalProperties") is False:
            for k in v:
                if k not in props:
                    errs.append(f"{path}: unexpected property '{k}'")
    if isinstance(v, list) and "items" in s:
        for i, item in enumerate(v):
            _check(item, s["items"], f"{path}[{i}]", errs)


def example(schema: Any) -> Any:
    """A minimal valid instance (used by the offline mock LLM)."""
    return _example(to_json_schema(schema))


def _example(s: dict) -> Any:
    if "anyOf" in s:
        return _example(s["anyOf"][0])
    if "enum" in s:
        return s["enum"][0]
    t = s.get("type")
    t = t[0] if isinstance(t, list) else t
    if t == "object" or "properties" in s:
        props = s.get("properties", {})
        required = s.get("required", list(props))
        return {k: _example(v) for k, v in props.items() if k in required}
    if t == "array":
        return []
    if t == "string":
        return "mock"
    if t == "integer":
        return int(s.get("minimum", 0))
    if t == "number":
        return float(s.get("minimum", 0))
    if t == "boolean":
        return False
    return None
