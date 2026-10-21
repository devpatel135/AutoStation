"""Redaction of sensitive event fields before any LLM context is assembled (SPEC §6.1, §9)."""
from __future__ import annotations

import copy
import json
from typing import Any, Iterable

REDACTED = "[REDACTED]"


def redact_event(event: dict) -> dict:
    """Return a copy of an event with every key listed in `sensitive_keys` replaced by [REDACTED].

    Nested dicts are searched, so ``sensitive_keys: ["ssn"]`` redacts ``data.customer.ssn`` as well.
    """
    out = copy.deepcopy(event)
    keys = set(out.get("sensitive_keys") or [])
    if keys and isinstance(out.get("data"), dict):
        out["data"] = _redact(out["data"], keys)
    out.pop("sensitive_keys", None)
    return out


def _redact(obj: Any, keys: set) -> Any:
    if isinstance(obj, dict):
        return {k: (REDACTED if k in keys else _redact(v, keys)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v, keys) for v in obj]
    return obj


def redact_events(events: Iterable[dict]) -> list[dict]:
    return [redact_event(e) for e in events]


def redacted_json(events: Iterable[dict], max_chars: int | None = None) -> str:
    text = json.dumps(redact_events(events), separators=(",", ":"), default=str)
    if max_chars and len(text) > max_chars:
        head = max_chars * 2 // 3
        tail = max_chars - head - 40
        text = text[:head] + f'..."<truncated {len(text) - max_chars} chars>"...' + text[-tail:]
    return text
