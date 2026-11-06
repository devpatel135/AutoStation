"""Classification audit query: scan the archive for LLM-assisted decisions.

``python -m automation_logging audit --archive <path>``

Finds run events where the LLM made a classification decision (Pattern 1/2/3 integrations). Each row
carries: automation, run_id, host, ring, event, from_class, to_class, confidence, reasoning, timestamp.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .common import parse_iso, utcnow

# Event names that indicate an LLM made a decision logged with ``decisionLog: true``.
_DECISION_EVENTS = frozenset({
    "page_classified_by_llm",
    "llm_classification",
    "llm_decision",
    "selector_healed_by_llm",
    "llm_output_processed",
})


def _is_decision_event(ev: dict) -> bool:
    name = ev.get("event", "")
    if name in _DECISION_EVENTS:
        return True
    data = ev.get("data") or {}
    return "confidence" in data and isinstance(data.get("confidence"), (int, float))


def run(archive: str, *, automation: str | None = None, days: int = 30,
        min_confidence: float | None = None, now: datetime | None = None) -> dict:
    """Scan the archive and return a list of LLM classification decisions."""
    archive_path = Path(archive)
    now = now or utcnow()
    cutoff = (now - timedelta(days=days)).date()
    items = []
    for run_dir in (archive_path / "run").glob("*") if (archive_path / "run").exists() else []:
        auto_name = run_dir.name
        if automation and auto_name != automation:
            continue
        for f in run_dir.glob("*/*/*/*/*.json"):
            rel = f.relative_to(run_dir).parts
            try:
                d = datetime(int(rel[0]), int(rel[1]), int(rel[2])).date()
            except (ValueError, IndexError):
                continue
            if d < cutoff:
                continue
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or rec.get("kind") != "run":
                continue
            run_id = rec.get("run_id", "")
            host = rec.get("host", "")
            ring = rec.get("ring", "")
            for ev in rec.get("events", []):
                if not isinstance(ev, dict) or not _is_decision_event(ev):
                    continue
                data = ev.get("data") or {}
                conf = data.get("confidence")
                if min_confidence is not None and (conf is None or conf < min_confidence):
                    continue
                items.append({
                    "ts": ev.get("ts") or rec.get("started_at"),
                    "automation": auto_name,
                    "run_id": run_id,
                    "host": host,
                    "ring": ring,
                    "event": ev.get("event"),
                    "from": data.get("from"),
                    "to": data.get("to") or data.get("kind"),
                    "confidence": conf,
                    "reasoning": (data.get("reasoning") or "")[:300] or None,
                })
    items.sort(key=lambda x: x.get("ts") or "")
    return {"total": len(items), "days": days, "automation": automation, "items": items}


def format_table(result: dict) -> str:
    rows = result["items"]
    if not rows:
        return f"No LLM classification decisions in the last {result['days']} days.\n"
    col_w = [18, 36, 7, 16, 10, 0.8]
    header = f"{'Timestamp':<20} {'Run ID':<20} {'Auto':<30} {'Event':<32} {'→':>16} {'Conf':>6}  Reasoning"
    sep = "-" * len(header)
    lines = [header, sep]
    for r in rows:
        ts = str(r.get("ts") or "")[:19]
        rid = str(r.get("run_id") or "")[:18]
        auto = str(r.get("automation") or "")[:28]
        ev = str(r.get("event") or "")[:30]
        to_cls = str(r.get("to") or r.get("from") or "")[:14]
        conf = f"{r.get('confidence', 0):.2f}" if r.get("confidence") is not None else "  -  "
        reason = (r.get("reasoning") or "")[:60]
        lines.append(f"{ts:<20} {rid:<20} {auto:<30} {ev:<32} {to_cls:>16} {conf:>6}  {reason}")
    lines.append(sep)
    lines.append(f"Total: {result['total']} decision event(s) in the last {result['days']} day(s).")
    return "\n".join(lines) + "\n"
