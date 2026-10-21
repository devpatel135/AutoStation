"""Optional telemetry from automation-logging (signals.json / runs). Everything degrades gracefully when absent."""
from __future__ import annotations

import json
from pathlib import Path

_SEV_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_signals(path: str | Path | None) -> list[dict]:
    if not path:
        return []
    p = Path(path)
    if p.is_dir():
        p = p / "signals.json"
    doc = _read(p) if p.is_file() else None
    if isinstance(doc, dict):
        doc = doc.get("signals", [])
    return [s for s in doc or [] if isinstance(s, dict)]


def load_runs(path: str | Path | None, limit: int = 5000) -> list[dict]:
    """A runs.json (list or {"runs": [...]}) or a directory of run logs (SPEC 6.2, ``kind: run``)."""
    if not path:
        return []
    p = Path(path)
    if p.is_file():
        doc = _read(p)
        runs = doc.get("runs", []) if isinstance(doc, dict) else doc
        return [r for r in runs or [] if isinstance(r, dict)]
    if p.is_dir():
        rj = p / "runs.json"
        if rj.is_file():
            return load_runs(rj)
        runs = []
        for i, f in enumerate(sorted(p.rglob("*.json"))):
            if i >= limit:
                break
            d = _read(f)
            if isinstance(d, dict) and d.get("kind") == "run":
                runs.append({k: d.get(k) for k in ("run_id", "automation", "status", "started_at", "ended_at", "duration_ms",
                                                   "synopsis", "ring", "host", "automation_version", "items")})
        return runs
    return []


def summarize(names: list[str], signals: list[dict], runs: list[dict]) -> dict[str, dict]:
    """name -> {status, last_run, signals, counts}. ``status``: healthy | degraded | failing | unknown."""
    out: dict[str, dict] = {}
    for name in names:
        mine = sorted((r for r in runs if r.get("automation") == name), key=lambda r: str(r.get("started_at") or ""))
        sigs = [s for s in signals if s.get("automation") == name]
        if not mine and not sigs:
            continue
        last = mine[-1] if mine else None
        worst = max((_SEV_RANK.get(str(s.get("severity")), 1) for s in sigs), default=0)
        status = "healthy"
        if last and last.get("status") in ("failed", "hung", "self_heal_failed"):
            status = "failing"
        elif worst >= 3:
            status = "failing"
        elif worst >= 1 or (last and last.get("status") == "partial"):
            status = "degraded"
        elif not last:
            status = "unknown"
        counts: dict[str, int] = {}
        for r in mine:
            counts[str(r.get("status"))] = counts.get(str(r.get("status")), 0) + 1
        out[name] = {"status": status, "last_run": last, "signals": sigs, "counts": counts}
    return out
