from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "wrapper-core"))

from automation_logging import analytics  # noqa: E402


def _entry(slot, blackouts=None):
    sched = {"timezone": "UTC", "slots": [dict(slot, id="s")]}
    if blackouts:
        sched["blackouts"] = blackouts
    return {"schedule": sched}


START = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)   # a Monday
END = datetime(2026, 3, 8, 23, 59, tzinfo=timezone.utc)


def test_weekdays_slot_expects_five_runs_a_week():
    runs = analytics.expected_runs(_entry({"type": "weekdays", "time": "09:00"}), START, END)
    assert len(runs) == 5 and all(t.weekday() < 5 for _, t in runs)


def test_interval_slot_respects_window():
    runs = analytics.expected_runs(_entry({"type": "interval", "every_minutes": 60, "window": "08:00-10:00"}), START, END)
    assert runs and all(8 <= t.hour <= 10 for _, t in runs)


def test_monthly_last_day_and_cron():
    m = analytics.expected_runs(_entry({"type": "monthly", "day": "last", "time": "06:00"}),
                                datetime(2026, 2, 1, tzinfo=timezone.utc), datetime(2026, 2, 28, 23, tzinfo=timezone.utc))
    assert [t.day for _, t in m] == [28]
    c = analytics.expected_runs(_entry({"type": "cron", "expr": "30 7 * * 1"}), START, END)
    assert len(c) == 1 and (c[0][1].hour, c[0][1].minute) == (7, 30)


def test_unknown_or_invalid_slot_contributes_nothing():
    assert analytics.expected_runs(_entry({"type": "cron", "expr": "bogus"}), START, END) == []
