"""Analytics correctness: calendar-aware SILENT, exact MISSED slots, MTTR with open episodes, SLO window, fast burn, FAILED hold, caps."""
from __future__ import annotations

from automation_logging import analytics, reliability
from factories import NOW, T, automation_entry, by_type, data_of, heartbeat_record, lifecycle_record, manifest, manifest_dict, run_record, signals_of

WEEKEND = [{"days": ["Saturday", "Sunday"]}]


def hb_hours_ago(h, host="H1"):
    return heartbeat_record(host, NOW.__class__.fromtimestamp(NOW.timestamp() - h * 3600, tz=NOW.tzinfo), automations={"auto-a": "1.0.0"})


# ---- 5.1 SILENT --------------------------------------------------------------------------------------------------------------
def test_silent_default_is_36h_medium():
    sigs, _, _ = signals_of(data_of(hb_hours_ago(50)), manifest())
    s = by_type(sigs, "SILENT")
    assert len(s) == 1 and s[0]["severity"] == "medium"
    assert not by_type(signals_of(data_of(hb_hours_ago(30)), manifest())[0], "SILENT")


def test_weekday_only_host_gets_72h():
    mf = manifest(maintenance=WEEKEND)
    assert not by_type(signals_of(data_of(hb_hours_ago(60)), mf)[0], "SILENT")
    assert by_type(signals_of(data_of(hb_hours_ago(80)), mf)[0], "SILENT")


def test_manifest_silent_after_h_is_honoured():
    mf = manifest_dict({"auto-a": dict(automation_entry(), silent_after_h=10)})
    assert by_type(signals_of(data_of(hb_hours_ago(12)), mf)[0], "SILENT")
    mf = manifest_dict({"auto-a": dict(automation_entry(), silent_after_h=100)})
    assert not by_type(signals_of(data_of(hb_hours_ago(80)), mf)[0], "SILENT")


def test_decommissioned_host_is_not_silent():
    lc = lifecycle_record("decommissioned", "auto-a", "H1", T(0, 10))
    assert not by_type(signals_of(data_of(hb_hours_ago(80), lc), manifest())[0], "SILENT")


# ---- 5.2 MISSED --------------------------------------------------------------------------------------------------------------
def _two_slot_runs(slot_today):
    runs = [run_record("auto-a", T(d, h, 1), slot=s) for d in (2, 1) for h, s in ((9, "am"), (10, "pm"))]
    runs.append(run_record("auto-a", T(0, 9, 1), slot=slot_today))
    return runs


def test_unknown_slot_run_does_not_hide_a_missed_slot_when_there_are_two_slots():
    slots = [{"id": "am", "type": "daily", "time": "09:00"}, {"id": "pm", "type": "daily", "time": "10:00"}]
    sigs, _, _ = signals_of(data_of(*_two_slot_runs("mystery")), manifest(slots=slots))
    assert {s["slot"] for s in by_type(sigs, "MISSED")} == {"am", "pm"}


def test_unknown_slot_run_counts_when_there_is_exactly_one_slot():
    runs = [run_record("auto-a", T(d, 9, 1)) for d in (2, 1)] + [run_record("auto-a", T(0, 9, 1), slot="mystery")]
    assert not by_type(signals_of(data_of(*runs), manifest())[0], "MISSED")


# ---- 5.3 MTTR / MTBF -----------------------------------------------------------------------------------------------------------
def test_mttr_reports_open_episodes_and_n():
    runs = [run_record("auto-a", T(5, 9), status="failed"), run_record("auto-a", T(4, 9)), run_record("auto-a", T(2, 9), status="failed")]
    out = reliability.mttr_mtbf(reliability.episodes(runs), NOW)
    assert out["episodes"] == 2 and out["mttr_n"] == 1 and out["open"] == 1
    assert out["mttr_s"] == 86400.0 and out["open_for_s"] > 2 * 86400 - 1


# ---- 5.4 SLO window / fast burn ----------------------------------------------------------------------------------------------------
def test_slo_window_is_clamped_to_the_loaded_window():
    slo = reliability.slo_for({"slo": {"target": 0.9, "window_days": 90}}, {"window_days": 30})
    assert slo["window_days"] == 30 and slo["requested_window_days"] == 90
    assert "requested_window_days" not in reliability.slo_for({"slo": {"window_days": 7}}, {"window_days": 30})


def test_daily_automation_can_fast_burn():
    c = dict(reliability.DEFAULTS)
    assert reliability.fast_window_h(automation_entry(), [], c) == 72.0
    runs = [run_record("auto-a", T(2, 9), status="failed"), run_record("auto-a", T(1, 9), status="failed"), run_record("auto-a", T(0, 9))]
    sigs, _, _ = signals_of(data_of(*runs), manifest())
    assert any(s["window"] if "window" in s else True for s in by_type(sigs, "ERROR_BUDGET_BURN")) and by_type(sigs, "ERROR_BUDGET_BURN")


# ---- 5.5 FAILED hold, caps ------------------------------------------------------------------------------------------------------------
def test_failed_stays_open_past_the_lookback_until_success_or_decommission():
    old = run_record("auto-a", T(5, 9), status="failed", error={"category": "TIMEOUT", "message": "x"})
    assert by_type(signals_of(data_of(old), manifest())[0], "FAILED")
    assert not by_type(signals_of(data_of(old, run_record("auto-a", T(4, 9))), manifest())[0], "FAILED")
    assert not by_type(signals_of(data_of(old, lifecycle_record("decommissioned", "auto-a", old["host"], T(4, 9))), manifest())[0], "FAILED")
    assert not by_type(signals_of(data_of(old), manifest(status="paused"))[0], "FAILED")


def test_type_cap_summarises_the_overflow():
    runs = [run_record("auto-a", T(0, 9), status="failed", host=f"H{i}", error={"category": "TIMEOUT", "message": "x"}) for i in range(5)]
    sigs, _, _ = signals_of(data_of(*runs), manifest(), type_cap=2)
    failed = by_type(sigs, "FAILED")
    assert len(failed) == 3 and sum(1 for s in failed if s.get("overflow")) == 1 and [s for s in failed if s.get("overflow")][0]["suppressed"] == 3
