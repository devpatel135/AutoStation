"""Synthetic fixtures for the test-suite (builders live in automation_logging.synthetic)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from automation_logging import analytics
from automation_logging.synthetic import (automation_entry, heartbeat_record, lifecycle_record, manifest_dict, run_record,  # noqa: F401
                                          write_archive, write_dir_transport)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)  # a Wednesday, noon UTC


def T(days_ago: float = 0, hh: int | None = None, mm: int = 0) -> datetime:
    """A time relative to NOW; with hh given, that wall-clock time `days_ago` days before NOW's date."""
    if hh is None:
        return NOW - timedelta(days=days_ago)
    return (NOW - timedelta(days=days_ago)).replace(hour=hh, minute=mm, second=0, microsecond=0)


def manifest(**auto_kwargs) -> dict:
    return manifest_dict({"auto-a": automation_entry(**auto_kwargs)})


def data_of(*artefacts) -> analytics.Data:
    runs = [a for a in artefacts if a["kind"] == "run"]
    hb = [a for a in artefacts if a["kind"] == "heartbeat"]
    lc = [a for a in artefacts if a["kind"] == "lifecycle"]
    return analytics.Data(runs, hb, lc)


def signals_of(data, mf, now=NOW, state=None, **cfg):
    report, st = analytics.compute(data, mf, now, cfg or None, state)
    return report["signals"], report, st


def by_type(signals, type_):
    return [s for s in signals if s["type"] == type_]


def sig(type_="FAILED", severity="high", automation="auto-a", host="VDI-1", sid=None, **extra) -> dict:
    from automation_logging.common import fingerprint
    s = {"id": sid or fingerprint(type_, automation, host, "x"), "type": type_, "severity": severity, "automation": automation,
         "host": host, "ring": "pilot", "detail": f"{type_} detail", "first_seen": "2026-10-14T09:00:00.000Z",
         "last_seen": "2026-10-14T09:00:00.000Z", "evidence": [], "diagnosis": None, "owner": "owner@example.com",
         "error_category": "NETWORK_ERROR", "synopsis": "It broke."}
    s.update(extra)
    return s
