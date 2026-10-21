"""Deterministic offline stand-in for the LLM endpoint (``AUTOSTATION_LLM_ENDPOINT=mock://``).

Used by dev ring, tests, and CI smoke tests so the whole platform runs end-to-end with no network. It reads the
same message layout ``llm_call`` sends to a real endpoint: system prompt (ending in ``[caller: x]``) and a user
message holding ``<instructions>``, ``<untrusted_data>`` and an optional ``<schema>``.

Tests can register handlers: ``mockllm.register(lambda caller, instr, data, schema: ..., name="x")``.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional, Union

from . import schema as schema_mod

Handler = Callable[[str, str, str, Optional[dict]], Optional[Union[str, dict]]]
_handlers: list = []  # (name|None, fn); later registrations win only by being asked first-come, first-served


def register(fn: Handler, name: str | None = None) -> None:
    """Add a handler. Re-registering the same ``name`` replaces it; unnamed handlers accumulate."""
    if name is not None:
        _handlers[:] = [(n, f) for n, f in _handlers if n != name]
    _handlers.append((name, fn))


def clear() -> None:
    _handlers.clear()


_TRANSIENT = re.compile(r"timeout|timed out|temporar|unavailable|reset|refused|503|502|504|rate limit|dns|vpn", re.I)
_PERMANENT = re.compile(r"keyerror|attributeerror|typeerror|nameerror|syntax|permission denied|not found|invalid|schema|assert", re.I)


def _section(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\n?(.*?)\n?</{tag}>", text, re.S)
    return m.group(1) if m else ""


def complete(messages: list[dict]) -> str:
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    cm = re.search(r"\[caller: ([^\]]*)\]", system)
    caller = cm.group(1) if cm else "unknown"
    instructions, data, schema_text = _section(user, "instructions"), _section(user, "untrusted_data"), _section(user, "schema")
    schema = json.loads(schema_text) if schema_text.strip() else None

    for _name, fn in list(_handlers):
        out = fn(caller, instructions, data, schema)
        if out is not None:
            return out if isinstance(out, str) else json.dumps(out)

    if caller.startswith("assist/"):
        out = _mock_assist(caller, instructions, data)
        if out is not None:
            return json.dumps(out)

    low = instructions.lower()
    if "selector no longer matches" in low:
        return json.dumps(_mock_heal(instructions, data))
    if "classify this automation error" in low:
        blob = data
        transient = bool(_TRANSIENT.search(blob)) and not _PERMANENT.search(blob)
        return json.dumps({"category": "transient" if transient else "permanent", "retry": transient,
                           "reasoning": "mock: error text looks transient" if transient else "mock: error text looks permanent"})
    if "synopsis" in low or "summary of this automation run" in low:
        return _mock_synopsis(data)
    if "diagnos" in low and schema:
        return json.dumps({"root_cause": "mock diagnosis: see error category and traceback tail",
                           "category": _guess_category(data), "suggested_action": "Review the failing step and retry after fixing the cause.",
                           "confidence": "low"})
    if schema:
        return json.dumps(schema_mod.example(schema))
    return "mock response"


def _guess_category(text: str) -> str:
    for cat in ("NETWORK_ERROR", "AUTH_EXPIRED", "SYSTEM_UNAVAILABLE", "DATA_ERROR", "LOGIC_ERROR"):
        if cat in text:
            return cat
    return "UNKNOWN"


def _mock_synopsis(data: str) -> str:
    try:
        ctx = json.loads(data)
    except ValueError:
        return "Mock synopsis: the run completed."
    if isinstance(ctx, dict):
        items = ctx.get("items", {})
        return (f"Mock synopsis: run {ctx.get('status', 'finished')} after {ctx.get('duration_ms', 0)} ms; "
                f"{items.get('total', 0)} item(s) handled ({items.get('failed', 0)} failed).")
    return "Mock synopsis: the run completed."



_SKELETON = '''import wrapper_core as wrapper


def process(record):
    """Handle one record. Raise wrapper.DataError for bad data so the item is skipped, not the run."""
    return record


@wrapper.entrypoint
def main():
    records = [{"id": 1}, {"id": 2}]  # TODO: fetch real work items
    for rec in records:
        wrapper.run_item(rec["id"], process, rec)
    wrapper.log_event("done", {"count": len(records)})


if __name__ == "__main__":
    raise SystemExit(main())
'''

_DESCRIPTOR = '''name: automation-new
version: 0.1.0
runtime: python
entrypoint: main.py
requires: {wrapper_core: ">=0.1.0,<1.0.0", execution_framework: ">=0.1.0,<1.0.0"}
timeout_s: 1800
'''


_TEST = """import main


def test_main_runs():
    assert main.main() == 0
"""


def _mock_assist(caller: str, instructions: str, data: str):
    step = caller.rsplit("/", 1)[-1]
    if caller == "assist/scaffold":
        return {"files": [{"path": "main.py", "content": _SKELETON}, {"path": "automation.yaml", "content": _DESCRIPTOR},
                          {"path": "tests/test_main.py", "content": _TEST}]}
    if caller == "assist/review":
        return {"findings": [], "summary": "mock review: no additional findings"}
    if caller == "assist/test" or step == "4-tests":
        return {"tests": _TEST, "covers": ["entrypoint exit code"]}
    if step == "1-requirements":
        return {"summary": "mock requirements", "inputs": ["schedule slot params"], "outputs": ["run summary"],
                "target_systems": [{"name": "TBD", "access": "user session", "has_api": True}], "data_classification": "internal",
                "llm_allowed": True, "schedule": "daily 09:00", "constraints": ["unattended"], "risks": ["target availability"],
                "open_questions": []}
    if step == "2-design":
        return {"approach": "headless", "reason": "mock", "patterns": [], "components": [{"name": "main", "role": "process items"}],
                "items": "one item per record", "error_handling": {"max_retries": 3, "skip_on_data_error": True},
                "schedule": {"slots": [{"id": "daily", "type": "daily", "time": "09:00"}]}, "manifest_entry": {"data_classification": "internal"}}
    if step == "3-scaffold":
        return {"files": [{"path": "main.py", "content": _SKELETON}, {"path": "automation.yaml", "content": _DESCRIPTOR}]}
    if step == "5-document":
        return {"markdown": chr(10).join(["# Design", "## Goal", "## Requirements", "## Design", "## Scaffold overview", "## Test plan", "## Risks and open questions", ""])}
    return None


_SNAP_LINE = re.compile(r"^\[\d+\] (\w+)(.*?) sel=(\S+)$", re.M)


def _mock_heal(instructions: str, data: str) -> dict:
    """Selector healing stand-in: pick the snapshot element whose label best overlaps the old selector's words."""
    m = re.search(r'Original selector: "((?:[^"\\]|\\.)*)"', instructions)
    orig = m.group(1) if m else ""
    kind = re.search(r"Intended action: (\w+)", instructions)
    words = [w for w in re.findall(r"[a-z]{3,}", orig.lower()) if w not in ("btn", "button", "input", "div", "span")]
    best, best_score = None, 0
    for tag, rest, sel in _SNAP_LINE.findall(data):
        score = sum(2 for w in words if w in rest.lower() or w in sel.lower())
        if tag in ("button", "a", "input", "select", "textarea", "summary"):
            score += 1 if score else 0
        if score > best_score:
            best, best_score = sel, score
    return {"selector": best or "body", "action": kind.group(1) if kind else "click", "reasoning": "mock: closest label match"}
