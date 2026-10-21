"""Automatic run synopsis (assessment: standard wrapper behaviour, not optional).

LLM first (redacted events only); deterministic fallback when the LLM is disabled/unavailable so every dashboard row
is readable regardless.
"""
from __future__ import annotations

import json

from .llm import llm_call
from .redact import redacted_json

PROMPT = ("Write a 2-3 sentence plain-English summary of this automation run for an operations dashboard. "
          "State what was processed, anything skipped or flagged and why, and the outcome. No markdown.")


def _fmt_duration(ms: int) -> str:
    s = ms // 1000
    if s < 60:
        return f"{s}s" if s else f"{ms} ms"
    m, s = divmod(s, 60)
    return f"{m}m {s:02d}s"


def deterministic(record: dict) -> str:
    items = record.get("items") or {}
    status = record.get("status", "finished")
    parts = [f"{record.get('automation', 'Automation')} {status.replace('_', ' ')} in {_fmt_duration(int(record.get('duration_ms', 0)))}."]
    if items.get("total"):
        parts.append(f"{items['total']} item(s): {items.get('success', 0)} succeeded, {items.get('skipped', 0)} skipped, {items.get('failed', 0)} failed.")
    err = record.get("error")
    if err:
        parts.append(f"Error {err.get('category', 'UNKNOWN')}: {str(err.get('message', ''))[:160]}")
    retries = [e for e in record.get("events", []) if e.get("event") == "retry_event"]
    if retries:
        parts.append(f"{len(retries)} retry attempt(s) were made.")
    if record.get("halt_reason"):
        parts.append(f"Halted: {record['halt_reason']}.")
    return " ".join(parts)


def build(record: dict, use_llm: bool = True) -> tuple[str, str]:
    """Return (synopsis, source) where source is 'llm' or 'deterministic'."""
    if use_llm and record.get("status") not in ("halted_paused", "skipped_maintenance"):
        events = [e for e in record.get("events", []) if e.get("event") != "llm_call"]
        ctx = json.dumps({"automation": record.get("automation"), "status": record.get("status"),
                          "duration_ms": record.get("duration_ms"), "items": record.get("items"),
                          "error": record.get("error"), "params_keys": sorted((record.get("params") or {}).keys()),
                          "events": redacted_json(events, 6000)}, default=str)
        out = llm_call(PROMPT, ctx, caller="wrapper-core/synopsis", template="synopsis@1",
                       classification=None, timeout_s=30)
        if isinstance(out, str) and out.strip():
            return out.strip()[:800], "llm"
    return deterministic(record), "deterministic"
