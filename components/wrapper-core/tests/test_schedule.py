from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

import pytest

from wrapper_core import manifest as mf
from wrapper_core import schedule as sc
from wrapper_core.paths import Paths
from wrapper_core.scheduler import task_xml

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
UTC = timezone.utc
AFTER = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)          # a Wednesday


def runs(slot, n=5, after=AFTER, **kw):
    return [r.strftime("%Y-%m-%d %H:%M") for r in sc.next_runs(slot, after, n, tz="UTC", **kw)]


# --- cron -------------------------------------------------------------------------------------------------
def test_cron_parses_lists_ranges_steps_and_names():
    s = sc.parse_cron("*/20 8-10 1,15 jan-mar mon-fri")
    assert s.minutes == {0, 20, 40} and s.hours == {8, 9, 10} and s.doms == {1, 15} and s.months == {1, 2, 3} and s.dows == {1, 2, 3, 4, 5}
    assert sc.parse_cron("0 0 * * 7").dows == {0}                       # 7 is Sunday like 0
    assert sc.parse_cron("0 0 * * 5-7").dows == {5, 6, 0}


@pytest.mark.parametrize("expr", ["", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8", "5-1 * * * *",
                                  "*/0 * * * *", "a * * * *", "1,,2 * * * *", "x" * 200, "* * * foo *"])
def test_cron_rejects_bad_expressions(expr):
    with pytest.raises(ValueError):
        sc.parse_cron(expr)


def test_cron_day_of_month_or_day_of_week_like_classic_cron():
    spec = sc.parse_cron("0 6 13 * 5")                                   # the 13th OR any Friday
    assert spec.day_matches(date(2026, 11, 13)) and spec.day_matches(date(2026, 11, 6)) and not spec.day_matches(date(2026, 11, 12))


# --- next_runs --------------------------------------------------------------------------------------------
def test_monthly_last_day_handles_short_months_and_leap_years():
    slot = {"id": "m", "type": "monthly", "time": "18:00", "day": "last"}
    assert runs(slot, 4, after=datetime(2027, 12, 31, 19, 0, tzinfo=UTC)) == ["2028-01-31 18:00", "2028-02-29 18:00", "2028-03-31 18:00", "2028-04-30 18:00"]


def test_monthly_fixed_days_and_month_filter():
    slot = {"id": "q", "type": "monthly", "time": "06:00", "day": [1, 15], "months": [1, 7]}
    assert runs(slot, 5) == ["2027-01-01 06:00", "2027-01-15 06:00", "2027-07-01 06:00", "2027-07-15 06:00", "2028-01-01 06:00"]


def test_weekdays_skip_weekends():
    assert runs({"id": "w", "type": "weekdays", "time": "07:30"}, 4, after=datetime(2026, 10, 23, 12, 0, tzinfo=UTC)) == \
        ["2026-10-26 07:30", "2026-10-27 07:30", "2026-10-28 07:30", "2026-10-29 07:30"]


def test_interval_window_and_days():
    slot = {"id": "i", "type": "interval", "every_minutes": 90, "window": "08:00-12:00", "days": ["Thursday"]}
    assert runs(slot, 4) == ["2026-10-22 08:00", "2026-10-22 09:30", "2026-10-22 11:00", "2026-10-29 08:00"]


def test_interval_window_across_midnight_spills_into_the_next_day():
    slot = {"id": "n", "type": "interval", "every_minutes": 60, "window": "22:00-02:00"}
    assert runs(slot, 5, after=datetime(2026, 10, 21, 21, 0, tzinfo=UTC)) == \
        ["2026-10-21 22:00", "2026-10-21 23:00", "2026-10-22 00:00", "2026-10-22 01:00", "2026-10-22 22:00"]


def test_cron_next_runs_and_old_slot_types_still_work():
    assert runs({"id": "c", "type": "cron", "expr": "30 9 * * 1-5"}, 3) == ["2026-10-22 09:30", "2026-10-23 09:30", "2026-10-26 09:30"]
    assert runs({"id": "d", "type": "daily", "time": "09:00"}, 2) == ["2026-10-22 09:00", "2026-10-23 09:00"]
    assert runs({"id": "w", "type": "weekly", "time": "08:30", "days": ["Friday"]}, 2) == ["2026-10-23 08:30", "2026-10-30 08:30"]
    assert runs({"id": "h", "type": "hourly", "every": 6}, 3) == ["2026-10-21 18:00", "2026-10-22 00:00", "2026-10-22 06:00"]


def test_blackouts_skip_dates_and_ranges():
    slot = {"id": "d", "type": "daily", "time": "09:00"}
    blk = ["2026-10-22", {"date": "2026-10-24", "reason": "freeze"}, {"from": "2026-10-26", "to": "2026-10-27", "reason": "audit"}]
    assert runs(slot, 3, blackouts=blk) == ["2026-10-23 09:00", "2026-10-25 09:00", "2026-10-28 09:00"]


def test_time_zones_are_honoured():
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo("America/New_York")
    except Exception:
        pytest.skip("no tz database")
    got = sc.next_runs({"id": "d", "type": "daily", "time": "09:00"}, AFTER, 1, tz="America/New_York")
    assert got[0].hour in (13, 14)                                       # 09:00 Eastern is 13:00/14:00 UTC


def test_next_runs_is_bounded_and_safe():
    assert sc.next_runs({"id": "x", "type": "daily", "time": "09:00"}, AFTER, 0) == []
    assert len(sc.next_runs({"id": "p", "type": "poll", "every_minutes": 1}, AFTER, 5000, tz="UTC")) == 1000
    never = {"id": "n", "type": "cron", "expr": "0 0 31 2 *"}            # 31 February can never fire: refused at validation, empty when asked anyway
    assert any("no calendar day" in e for e in sc.validate_slot(never))
    with pytest.raises(ValueError):
        sc.next_runs(never, AFTER, 3, tz="UTC", horizon_days=200)
    assert not sc.validate_slot({"id": "n", "type": "cron", "expr": "0 0 29 2 *"})   # leap day is a real day


def test_stepped_day_fields_combined_with_a_restricted_other_field_are_refused():
    amb = {"id": "a", "type": "cron", "expr": "0 6 */2 * 1"}
    assert any("ambiguous" in e for e in sc.validate_slot(amb))
    assert not sc.validate_slot({"id": "a", "type": "cron", "expr": "0 6 */2 * *"})   # a stepped field next to a bare * is unambiguous


def test_trigger_xml_validates_start_day_and_blackouts_are_parsed_once():
    slot = {"id": "w", "type": "weekdays", "time": "09:00"}
    for bad in ("2026-13-45", "tomorrow", "2026-01-01</StartBoundary><x/>"):
        with pytest.raises(ValueError):
            sc.trigger_xml(slot, "UTC", bad)
    assert "<StartBoundary>2026-01-05T" in sc.trigger_xml(slot, "UTC", "2026-01-05")
    many = [{"from": f"2027-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}", "to": f"2027-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}"} for i in range(500)]
    import time as _t
    t0 = _t.monotonic()
    runs = sc.next_runs({"id": "m", "type": "interval", "every_minutes": 1}, AFTER, 5, tz="UTC", blackouts=many)
    assert len(runs) == 5 and _t.monotonic() - t0 < 5


# --- validation -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("slot,expect", [
    ({"id": "a", "type": "monthly", "time": "9:00", "day": 31}, []),
    ({"id": "a", "type": "monthly", "time": "09:00", "day": 32}, "monthly day"),
    ({"id": "a", "type": "monthly", "time": "09:00"}, "needs day"),
    ({"id": "a", "type": "monthly", "time": "09:00", "day": 1, "months": [13]}, "months"),
    ({"id": "a", "type": "monthly", "time": "9am", "day": 1}, "HH:MM"),
    ({"id": "a", "type": "weekdays", "time": "25:00"}, "HH:MM"),
    ({"id": "a", "type": "interval", "every_minutes": 0}, "every_minutes"),
    ({"id": "a", "type": "interval", "every_minutes": 5, "window": "8-18"}, "window"),
    ({"id": "a", "type": "interval", "every_minutes": 5, "days": ["Funday"]}, "bad day"),
    ({"id": "a", "type": "cron", "expr": "* * * *"}, "5 fields"),
    ({"id": "a", "type": "cron", "expr": "*/5 * * * *"}, "distinct times"),
    ({"id": "a", "type": "cron", "expr": "0 6 1 1,4,7,10 *"}, []),
    ({"id": "a", "type": "interval", "every_minutes": 15, "window": "08:00-18:00", "days": ["Monday"]}, []),
])
def test_validate_slot(slot, expect):
    errs = sc.validate_slot(slot)
    if not expect:
        assert errs == []
    else:
        assert any(expect in e for e in errs), errs


def test_validate_blackouts():
    assert sc.validate_blackouts(["2026-12-25", {"from": "2026-12-28", "to": "2026-12-31"}]) == []
    assert sc.validate_blackouts("2026-12-25")
    assert sc.validate_blackouts(["25/12/2026"])
    assert sc.validate_blackouts([{"from": "2026-12-31", "to": "2026-12-28"}])
    assert sc.validate_blackouts(["2026-01-01"] * 501)
    assert sc.validate_blackouts([123])                                  # hostile type: reported, not raised


def entry(slots, **sched):
    return {"schedule": {"timezone": "UTC", "slots": slots, **sched}, "description": "d", "owner": "o@example.com", "status": "active",
            "versions": {"1.0.0": {"url": "a.zip", "sha256": "a" * 64}}, "rings": {"dev": "1.0.0", "pilot": "1.0.0", "broad": "1.0.0"}}


def test_manifest_validate_covers_new_types_and_blackouts():
    m = {"schema_version": 1, "components": {}, "automations": {"x": entry([{"id": "ok", "type": "monthly", "time": "06:00", "day": "last"}],
                                                                             blackouts=["2026-12-25"])}}
    assert mf.validate(m) == []
    m["automations"]["x"] = entry([{"id": "bad", "type": "cron", "expr": "nope"}], blackouts=["xmas"])
    errs = mf.validate(m)
    assert any("cron" in e for e in errs) and any("blackout" in e for e in errs)


def test_blackout_date_skips_the_run_via_maintenance_check():
    sched = {"timezone": "UTC", "blackouts": [{"date": "2026-12-25", "reason": "holiday"}]}
    hit = mf.in_maintenance(sched, datetime(2026, 12, 25, 10, 0, tzinfo=UTC))
    assert hit and hit["reason"] == "holiday"
    assert mf.in_maintenance(sched, datetime(2026, 12, 26, 10, 0, tzinfo=UTC)) is None
    assert mf.in_maintenance({"timezone": "UTC", "blackouts": "garbage"}, AFTER) is None      # malformed input never raises


# --- Task Scheduler XML -------------------------------------------------------------------------------------
@pytest.mark.parametrize("slot", [
    {"id": "monthly", "type": "monthly", "time": "18:00", "day": [1, "last"], "months": [3, 6, 9, 12]},
    {"id": "bizdays", "type": "weekdays", "time": "07:30"},
    {"id": "office", "type": "interval", "every_minutes": 15, "window": "08:00-18:00", "days": ["Monday", "Friday"]},
    {"id": "always", "type": "interval", "every_minutes": 10},
    {"id": "dom", "type": "cron", "expr": "0 6 1,15 * *"},
    {"id": "dow", "type": "cron", "expr": "30 8,16 * * 1-5"},
    {"id": "both", "type": "cron", "expr": "0 6 1 jan,jul mon"},
    {"id": "months", "type": "cron", "expr": "0 5 * 6-8 *"},
], ids=lambda s: s["id"])
def test_new_slot_types_render_wellformed_least_privilege_xml(slot):
    xml = task_xml(Paths(), "auto-a", slot, "UTC")
    root = ET.fromstring(xml.split("?>", 1)[1])
    assert root.find("t:Principals/t:Principal/t:RunLevel", NS).text == "LeastPrivilege"
    assert len(root.findall("t:Triggers/t:CalendarTrigger", NS)) >= 1
    assert slot["id"] in root.find("t:Actions/t:Exec/t:Arguments", NS).text


def test_xml_content_for_each_new_type():
    x = task_xml(Paths(), "a", {"id": "m", "type": "monthly", "time": "18:00", "day": [1, "last"], "months": [1]}, "UTC")
    assert "<Day>1</Day>" in x and "<Day>Last</Day>" in x and "<January />" in x and "<February />" not in x
    x = task_xml(Paths(), "a", {"id": "w", "type": "weekdays", "time": "07:30"}, "UTC")
    assert "<Monday />" in x and "<Friday />" in x and "<Saturday />" not in x and "<Sunday />" not in x
    x = task_xml(Paths(), "a", {"id": "o", "type": "interval", "every_minutes": 90, "window": "08:00-18:00"}, "UTC")
    assert "<Interval>PT1H30M</Interval>" in x and "<Duration>PT10H</Duration>" in x
    x = task_xml(Paths(), "a", {"id": "c", "type": "cron", "expr": "0 8,16 * * 1-5"}, "UTC")
    assert x.count("<CalendarTrigger>") == 2 and "<ScheduleByWeek>" in x
    x = task_xml(Paths(), "a", {"id": "c", "type": "cron", "expr": "0 5 * 6-8 mon"}, "UTC")
    assert "<ScheduleByMonthDayOfWeek>" in x and "<Last>" not in x and "<Week>Last</Week>" in x


def test_slot_that_cannot_be_expressed_is_refused_not_misrendered():
    with pytest.raises(ValueError):
        task_xml(Paths(), "a", {"id": "c", "type": "cron", "expr": "*/5 * * * *"}, "UTC")      # 288 triggers
    with pytest.raises(ValueError):
        task_xml(Paths(), "a", {"id": "m", "type": "monthly", "time": "09:00", "day": 40}, "UTC")


def test_register_skips_an_invalid_new_slot_but_keeps_the_good_ones(monkeypatch):
    from wrapper_core.scheduler import Scheduler

    class Fake:
        existing = set()

        def __call__(self, args, input_text=None):
            import subprocess
            if args[1] == "/Create":
                self.existing.add(args[3])
            return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("wrapper_core.scheduler.IS_WINDOWS", True)
    s = Scheduler(Paths().ensure(), runner=Fake())
    touched = s.register("auto-a", {"timezone": "UTC", "slots": [{"id": "good", "type": "weekdays", "time": "07:30"},
                                                               {"id": "bad", "type": "cron", "expr": "*/5 * * * *"}]})
    assert touched == ["AUTOMAGIX\\auto-a-good"]
