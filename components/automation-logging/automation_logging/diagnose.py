"""LLM error diagnosis for FAILED / HUNG / ITEM_FAILURES signal groups.

Safeguards (assessment: "prompt injection via log content", "data classification in LLM context"):
* log text is passed ONLY through ``context`` (untrusted data); the instruction prompt is a constant.
* every event is redacted (``sensitive_keys``) before the context is assembled.
* output is schema-validated by ``llm_call_ex``; invalid output => no diagnosis (never raw model text).
* automations whose ``data_classification`` exceeds ``llm_allowed_classification`` are never sent.
* results are cached by error fingerprint, and the number of LLM calls per pipeline run is capped.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .common import iso, read_json, utcnow, write_json
from . import analytics

ELIGIBLE = ("FAILED", "HUNG", "ITEM_FAILURES")
CALLER = "automation-logging/diagnose"
TEMPLATE = "diagnose@1"
PROMPT = ("Diagnose this automation failure for an operations engineer. Identify the most likely root cause, pick the "
          "closest error category, and suggest one concrete next action. Base the answer only on the analysed data.")
SCHEMA = {"root_cause": "string",
          "category": "NETWORK_ERROR|AUTH_EXPIRED|SYSTEM_UNAVAILABLE|DATA_ERROR|LOGIC_ERROR|UNKNOWN",
          "suggested_action": "string", "confidence": "low|medium|high"}


@contextmanager
def _llm_env(entry: dict):
    from wrapper_core import runctx
    keys = {"AUTOMAGIX_LLM_ALLOWED_CLASSIFICATION": entry.get("llm_allowed_classification"),
            "AUTOMAGIX_DATA_CLASSIFICATION": entry.get("data_classification")}
    saved = {k: os.environ.get(k) for k in keys}
    for k, v in keys.items():
        if v:
            os.environ[k] = str(v)
        else:
            os.environ.pop(k, None)
    tmp = tempfile.mkdtemp(prefix="amx-ci-llm-")
    prev = getattr(runctx, "_current", None)
    runctx.set_current(runctx.RunContext(Path(tmp), "ci-diagnose", "automation-logging", "ci"))
    try:
        yield
    finally:
        runctx.set_current(prev)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def build_context(signal: dict, data: analytics.Data) -> str:
    """Redacted, size-bounded JSON for the model (untrusted data)."""
    from wrapper_core.redact import redacted_json
    run = data.runs_by_id.get((signal.get("evidence") or [None])[-1])
    ctx = {"signal_type": signal["type"], "automation": signal.get("automation"), "error_category": signal.get("error_category"),
           "error_message": signal.get("error_message"), "detail": signal.get("detail")}
    if run:
        events = [e for e in run.get("events", []) if e.get("event") != "llm_call"]
        retries = [e for e in events if e.get("event") == "retry_event"]
        ctx.update(status=run.get("status"), exit_code=run.get("exit_code"), duration_ms=run.get("duration_ms"),
                   items=run.get("items"), error=run.get("error"), self_heal_errors=(run.get("self_heal") or {}).get("errors"),
                   retry_events=json.loads(redacted_json(retries, 3000)) if retries else [],
                   recent_events=redacted_json(events[-30:], 5000))
    return json.dumps(ctx, default=str)[:12000]


def diagnose_signals(signals: list[dict], data: analytics.Data, manifest: dict, cache_path: str | Path,
                     max_calls: int = 20, llm_call=None) -> tuple[list[dict], dict]:
    """Attach ``diagnosis`` to eligible signals. Returns (signals, stats). ``llm_call`` is injectable for tests."""
    from wrapper_core.llm import classification_allowed, llm_call_ex
    call = llm_call or llm_call_ex
    cache = read_json(cache_path, {}) or {}
    autos = manifest.get("automations") or {}
    stats = {"eligible": 0, "calls": 0, "cached": 0, "skipped_classification": 0, "skipped_cap": 0, "failed": 0}
    groups: dict[str, dict] = {}   # group key -> diagnosis result (so one call serves many hosts)
    for s in signals:
        if s["type"] not in ELIGIBLE:
            continue
        stats["eligible"] += 1
        entry = autos.get(s.get("automation")) or {}
        key = "|".join([s.get("automation") or "", s.get("type"), s.get("error_fp") or "", s.get("error_category") or ""])
        if key in groups:
            s["diagnosis"], s["diagnosis_reason"] = groups[key]["diagnosis"], groups[key]["reason"]
            continue
        with _llm_env(entry):
            if not entry or not classification_allowed(None):  # unknown automation => unknown classification => fail closed
                res = {"diagnosis": None, "reason": "classification"}
                stats["skipped_classification"] += 1
            elif key in cache:
                res = {"diagnosis": cache[key]["diagnosis"], "reason": "cache"}
                stats["cached"] += 1
            elif stats["calls"] >= max_calls:
                res = {"diagnosis": None, "reason": "cap"}
                stats["skipped_cap"] += 1
            else:
                stats["calls"] += 1
                r = call(PROMPT, build_context(s, data), schema=SCHEMA, caller=CALLER, template=TEMPLATE, timeout_s=60)
                value = getattr(r, "value", r)
                if isinstance(value, dict):
                    res = {"diagnosis": value, "reason": "llm"}
                    cache[key] = {"diagnosis": value, "ts": iso(), "signal_type": s["type"]}
                else:
                    res = {"diagnosis": None, "reason": "llm_unavailable"}
                    stats["failed"] += 1
        groups[key] = res
        s["diagnosis"], s["diagnosis_reason"] = res["diagnosis"], res["reason"]
    _prune(cache)
    write_json(cache_path, cache)
    return signals, stats


def _prune(cache: dict, max_entries: int = 500) -> None:
    if len(cache) > max_entries:
        for k in sorted(cache, key=lambda k: cache[k].get("ts", ""))[:len(cache) - max_entries]:
            cache.pop(k)
