from __future__ import annotations

from datetime import timedelta

from automation_logging import analytics
from automation_logging.common import iso, percentile

from factories import (NOW, T, automation_entry, by_type, data_of, heartbeat_record, lifecycle_record, manifest, manifest_dict,
                       run_record, signals_of)


def daily_runs(days, hh=9, **kw):
    return [run_record("auto-a", T(d, hh, 1), **kw) for d in days]


# ---------------------------------------------------------------------------------------------- MISSED
def test_missed_is_one_stable_signal_per_slot_and_escalates():
    runs = daily_runs([4, 3, 2])                       # ran through 10-12; nothing on 10-13 or this morning
    d = data_of(*runs, lifecycle_record("installed", "auto-a", "VDI-1", T(5)))
    sigs, _, _ = signals_of(d, manifest())
    missed = by_type(sigs, "MISSED")
    assert len(missed) == 1 and missed[0]["missed_count"] == 2           # one open problem, not one per expected run
    assert missed[0]["first_seen"] == "2026-10-13T09:00:00.000Z" and missed[0]["expected_at"] == "2026-10-14T09:00:00.000Z"
    assert all(s["severity"] == "critical" and s["owner"] == "owner@example.com" and s["host"] is None for s in missed)


def test_missed_not_raised_when_run_present_within_grace_or_exempt():
    d = data_of(*daily_runs([4, 3, 2, 1, 0]))
    assert not by_type(signals_of(d, manifest())[0], "MISSED")
    # still inside the grace period at 09:10 (grace = max(15min, 0.25*baseline))
    d2 = data_of(*daily_runs([4, 3, 2, 1]), lifecycle_record("installed", "auto-a", "VDI-1", T(5)))
    sigs, _, _ = signals_of(d2, manifest(), now=T(0, 9, 10))
    assert not by_type(sigs, "MISSED")
    sigs, _, _ = signals_of(d2, manifest(), now=T(0, 9, 20))
    assert len(by_type(sigs, "MISSED")) == 1


def test_missed_exemptions_poll_paused_maintenance_unknown_and_fleet_pause():
    d = data_of(*daily_runs([4, 3]), lifecycle_record("installed", "auto-a", "VDI-1", T(5)))
    assert not by_type(signals_of(d, manifest(slots=[{"id": "poll", "type": "poll", "every_minutes": 5}]))[0], "MISSED")
    assert not by_type(signals_of(d, manifest(status="paused"))[0], "MISSED")
    assert not by_type(signals_of(d, manifest(maintenance=[{"days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}]))[0], "MISSED")
    fleet_paused = manifest_dict({"auto-a": automation_entry()}, fleet_paused=True)
    assert not by_type(signals_of(d, fleet_paused)[0], "MISSED")
    never_seen = data_of()
    assert not by_type(signals_of(never_seen, manifest())[0], "MISSED")   # unknown automation: nothing to compare against


def test_missed_weekly_and_hourly_slots():
    weekly = [{"id": "weekly", "type": "weekly", "days": ["Monday"], "time": "08:00"}]
    d = data_of(run_record("auto-a", T(9, 8, 1), slot="weekly"), lifecycle_record("installed", "auto-a", "VDI-1", T(12)))
    sigs, _, _ = signals_of(d, manifest(slots=weekly), now=T(0, 12), lookback_h=240)
    assert {s["expected_at"][:10] for s in by_type(sigs, "MISSED")} == {"2026-10-12"}   # Monday 10-05 was served; 10-12 was not
    hourly = [{"id": "h", "type": "hourly", "every": 6}]
    d = data_of(*[run_record("auto-a", T(1, h, 2), slot="h") for h in (0, 6, 12)], lifecycle_record("installed", "auto-a", "VDI-1", T(2)))
    sigs, _, _ = signals_of(d, manifest(slots=hourly), now=T(0, 7), lookback_h=24)
    missed = by_type(sigs, "MISSED")
    assert len(missed) == 1 and missed[0]["missed_count"] == 3                       # three overdue 6-hourly runs = one signal
    assert missed[0]["first_seen"] == "2026-10-13T18:00:00.000Z" and missed[0]["expected_at"] == "2026-10-14T06:00:00.000Z"


def test_missed_respects_schedule_timezone():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
    except Exception:  # stock Windows Python ships no tz database
        import pytest
        pytest.skip("no tz database available")
    # 09:00 America/New_York == 13:00 UTC in October (EDT)
    d = data_of(*[run_record("auto-a", T(x, 13, 1)) for x in (3, 2)], lifecycle_record("installed", "auto-a", "VDI-1", T(4)))
    sigs, _, _ = signals_of(d, manifest(tz="America/New_York"))
    assert [s["expected_at"] for s in by_type(sigs, "MISSED")] == ["2026-10-13T13:00:00.000Z"]   # today's 13:00 UTC is still in the future


# ---------------------------------------------------------------------------------------------- SILENT
def test_silent_host_and_decommission_exemption():
    hb = heartbeat_record("VDI-9", NOW - timedelta(hours=40), automations={"auto-a": "1.0.0"})
    sigs, _, _ = signals_of(data_of(hb), manifest())
    s = by_type(sigs, "SILENT")
    assert len(s) == 1 and s[0]["host"] == "VDI-9" and s[0]["automation"] == "auto-a"
    fresh = heartbeat_record("VDI-9", NOW - timedelta(hours=10), automations={"auto-a": "1.0.0"})
    assert not by_type(signals_of(data_of(fresh), manifest())[0], "SILENT")
    decom = lifecycle_record("decommissioned", "auto-a", "VDI-9", NOW - timedelta(hours=39))
    assert not by_type(signals_of(data_of(hb, decom), manifest())[0], "SILENT")
    empty = heartbeat_record("VDI-9", NOW - timedelta(hours=90), automations={})
    assert not by_type(signals_of(data_of(empty), manifest())[0], "SILENT")
    # recent run activity on the host counts as life even if the heartbeat is old
    alive = run_record("auto-a", NOW - timedelta(hours=2), host="VDI-9")
    assert not by_type(signals_of(data_of(hb, alive), manifest())[0], "SILENT")


# ---------------------------------------------------------------------------------------------- SLOW / HUNG
def test_slow():
    sigs, _, _ = signals_of(data_of(run_record("auto-a", T(0, 9), duration_ms=300000)), manifest())
    assert by_type(sigs, "SLOW") and "baseline" in by_type(sigs, "SLOW")[0]["detail"]
    assert not by_type(signals_of(data_of(run_record("auto-a", T(0, 9), duration_ms=200000)), manifest())[0], "SLOW")


def test_hung_killed_and_hung_detected():
    killed = run_record("auto-a", T(0, 9), status="hung", duration_ms=3_600_000, hung_detected=True,
                        error={"category": "SYSTEM_UNAVAILABLE", "message": "killed", "type": "Escalation"})
    s = by_type(signals_of(data_of(killed), manifest())[0], "HUNG")
    assert len(s) == 1 and s[0]["severity"] == "critical" and "killed" in s[0]["detail"]
    late = run_record("auto-a", T(0, 9), status="success", duration_ms=500000, hung_detected=True)
    sigs, _, _ = signals_of(data_of(late), manifest())
    assert by_type(sigs, "HUNG") and not by_type(sigs, "SLOW")  # one signal per cause


# ---------------------------------------------------------------------------------------------- FAILED / ITEM_FAILURES
ERR = {"category": "LOGIC_ERROR", "type": "KeyError", "message": "KeyError: 'vendor_id'", "attempts": 1}


def test_failed_streak_severity_and_resolution():
    one = [run_record("auto-a", T(0, 9), status="failed", error=ERR)]
    s = by_type(signals_of(data_of(*one), manifest())[0], "FAILED")
    assert len(s) == 1 and s[0]["severity"] == "high" and s[0]["error_category"] == "LOGIC_ERROR"
    three = [run_record("auto-a", T(d, 9), status="failed", error=ERR) for d in (2, 1, 0)]
    s3 = by_type(signals_of(data_of(*three), manifest())[0], "FAILED")[0]
    assert s3["severity"] == "critical" and s3["consecutive"] == 3 and len(s3["evidence"]) == 3
    assert s3["first_seen"] == iso(T(2, 9))
    recovered = three + [run_record("auto-a", T(0, 10), status="success")]
    assert not by_type(signals_of(data_of(*recovered), manifest())[0], "FAILED")


def test_failed_signal_ids_are_stable_and_error_specific():
    a = [run_record("auto-a", T(1, 9), status="failed", error=ERR)]
    b = [run_record("auto-a", T(0, 9), status="failed", error=dict(ERR, message="KeyError: 'vendor_id'"))]
    sa = by_type(signals_of(data_of(*a), manifest())[0], "FAILED")[0]
    sb = by_type(signals_of(data_of(*b), manifest(), now=NOW)[0], "FAILED")[0]
    assert sa["id"] == sb["id"]                                    # same error on a later day => same alert fingerprint
    other = [run_record("auto-a", T(0, 9), status="failed", error=dict(ERR, category="NETWORK_ERROR", message="timed out after 30s"))]
    assert by_type(signals_of(data_of(*other), manifest())[0], "FAILED")[0]["id"] != sa["id"]
    n1 = [run_record("auto-a", T(0, 9), status="failed", error=dict(ERR, message="HTTP 503 after 12 retries"))]
    n2 = [run_record("auto-a", T(0, 9), status="failed", error=dict(ERR, message="HTTP 503 after 7 retries"))]
    assert (by_type(signals_of(data_of(*n1), manifest())[0], "FAILED")[0]["id"]
            == by_type(signals_of(data_of(*n2), manifest())[0], "FAILED")[0]["id"])   # digits are normalised away


def test_paused_and_skipped_runs_do_not_count_as_failures_or_successes():
    runs = [run_record("auto-a", T(1, 9), status="failed", error=ERR), run_record("auto-a", T(0, 9), status="halted_paused")]
    assert by_type(signals_of(data_of(*runs), manifest())[0], "FAILED")        # the pause did not "fix" it


def test_item_failures_hidden_in_a_successful_run():
    items = [(f"i{k}", "success", 500, None) for k in range(7)] + [(f"f{k}", "failed", 900, "data_error") for k in range(3)]
    r = run_record("auto-a", T(0, 9), status="partial", items=items)
    s = by_type(signals_of(data_of(r), manifest())[0], "ITEM_FAILURES")
    assert len(s) == 1 and s[0]["severity"] == "high" and s[0]["failure_rate"] == 0.3
    r2 = run_record("auto-a", T(0, 9), status="partial", items=[(f"i{k}", "success", 5, None) for k in range(19)] + [("f", "failed", 5, "data_error")])
    assert by_type(signals_of(data_of(r2), manifest())[0], "ITEM_FAILURES")[0]["severity"] == "medium"


# ---------------------------------------------------------------------------------------------- VERSION_DRIFT / UNEXPECTED
def test_version_drift_needs_state_or_selfheal_errors():
    mf = manifest(version="1.1.0")
    old_run = run_record("auto-a", T(0, 10), version="1.0.0")
    assert not by_type(signals_of(data_of(old_run), mf)[0], "VERSION_DRIFT")                   # first sighting: no basis yet
    state = {"targets": {"auto-a|pilot": {"version": "1.1.0", "since": iso(T(1))}}}
    sigs, _, st = signals_of(data_of(old_run), mf, state=state)
    d = by_type(sigs, "VERSION_DRIFT")
    assert len(d) == 1 and "1.0.0" in d[0]["detail"] and "1.1.0" in d[0]["detail"]
    assert st["targets"]["auto-a|pilot"]["since"] == iso(T(1))                                  # unchanged target keeps its timestamp
    before_change = run_record("auto-a", T(1, 0, 0) - timedelta(hours=3), version="1.0.0")      # ran before the change: not drift
    assert not by_type(signals_of(data_of(before_change), mf, state=state)[0], "VERSION_DRIFT")
    errored = run_record("auto-a", T(0, 10), version="1.0.0", self_heal_errors=["sha256 mismatch"])
    assert by_type(signals_of(data_of(errored), mf)[0], "VERSION_DRIFT")[0]["error_message"] == "sha256 mismatch"


def test_version_drift_state_tracks_target_changes():
    _, _, st = signals_of(data_of(), manifest(version="1.0.0"))
    _, _, st2 = signals_of(data_of(), manifest(version="1.1.0"), now=NOW + timedelta(days=1), state=st)
    assert st["targets"]["auto-a|pilot"]["version"] == "1.0.0" and st2["targets"]["auto-a|pilot"]["version"] == "1.1.0"
    assert st2["targets"]["auto-a|pilot"]["since"] == iso(NOW + timedelta(days=1))


def test_component_version_drift():
    mf = manifest()
    state = {"targets": {"component:wrapper-core|pilot": {"version": "0.1.0", "since": iso(T(2))}}}
    r = run_record("auto-a", T(0, 9), wrapper="0.0.9")
    s = by_type(signals_of(data_of(r), mf, state=state)[0], "VERSION_DRIFT")
    assert s and "wrapper-core 0.0.9" in s[0]["detail"]


def test_unexpected_run_inside_maintenance_window():
    mf = manifest(maintenance=[{"daily": "08:00-10:00"}])
    inside = run_record("auto-a", T(0, 9, 5))
    s = by_type(signals_of(data_of(inside), mf)[0], "UNEXPECTED")
    assert len(s) == 1 and "maintenance window" in s[0]["detail"]
    skipped = run_record("auto-a", T(0, 9, 5), status="skipped_maintenance")
    assert not by_type(signals_of(data_of(skipped), mf)[0], "UNEXPECTED")
    outside = run_record("auto-a", T(0, 11, 0))
    assert not by_type(signals_of(data_of(outside), mf)[0], "UNEXPECTED")


# ---------------------------------------------------------------------------------------------- metrics
def test_item_level_metrics():
    items = ([(f"s{i}", "success", 100 * (i + 1), None) for i in range(8)] + [("k1", "skipped", 50, "duplicate"), ("k2", "skipped", 50, "duplicate"),
             ("k3", "skipped", 50, "stale"), ("f1", "failed", 900, "data_error")])
    runs = [run_record("auto-a", T(0, 9), status="partial", items=items, duration_ms=60000),
            run_record("auto-a", T(1, 9), status="failed", error=ERR, duration_ms=1000)]
    m = analytics.item_metrics(runs)
    assert m["total"] == 12 and m["skipped"] == 3 and m["failed"] == 1
    assert m["skip_reasons"] == {"duplicate": 2, "stale": 1} and m["skip_rate"] == 0.25
    assert m["throughput_per_min"] == 12.0 and m["item_failure_rate"] == round(1 / 12, 4)
    assert m["run_failure_rate"] == 0.5 and m["duration_ms"]["p50"] == 300 and m["duration_ms"]["p99"] == 900
    assert m["runs_with_item_failures"] == 1 and m["failure_categories"] == {"data_error": 1}


def test_percentile_nearest_rank():
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10 and percentile([1, 2, 3, 4], 50) == 2 and percentile([], 50) is None


def test_retry_and_llm_stats_and_report_shape():
    retries = [{"attempt": 1, "category": "NETWORK_ERROR", "decision": "retry", "source": "deterministic", "outcome": "failed_again"},
               {"attempt": 2, "category": "NETWORK_ERROR", "decision": "retry", "source": "deterministic", "outcome": "recovered"},
               {"attempt": 1, "category": "UNKNOWN", "decision": "escalate", "source": "llm", "outcome": "escalated"}]
    r = run_record("auto-a", T(0, 9), retries=retries, llm_calls=3)
    r["synopsis_source"] = "llm"
    sigs, report, _ = signals_of(data_of(r, run_record("auto-a", T(1, 9))), manifest(catalog={"minutes_per_item": 6}))
    rt = report["retries"]
    assert rt["total_events"] == 3 and rt["runs_with_retries"] == 1 and rt["by_source"] == {"deterministic": 2, "llm": 1}
    assert rt["by_category"]["NETWORK_ERROR"]["recovered"] == 1 and rt["by_category"]["NETWORK_ERROR"]["recovery_rate"] == 0.5
    assert report["llm"]["calls"] == 4 and report["llm"]["synopsis_from_llm"] == 1
    a = report["automations"]["auto-a"]
    assert a["health"] in ("healthy", "degraded") and a["last_run"]["run_id"] == r["run_id"] and len(a["daily"]) == 14
    assert report["fleet"]["total"] == 2 and report["fleet"]["success_rate"] == 1.0


def test_business_impact_and_trend_and_versions_table():
    items = [(f"i{k}", "success", 100, None) for k in range(30)]
    runs = [run_record("auto-a", T(d, 9), items=items) for d in range(0, 3)] + [run_record("auto-a", T(20, 9), items=items)]
    hb = heartbeat_record("VDI-1", T(0, 6), automations={"auto-a": "0.9.0"}, wrapper="0.1.0")
    sigs, report, _ = signals_of(data_of(*runs, hb), manifest(version="1.0.0", catalog={"minutes_per_item": 6}))
    assert report["impact"]["items_7d"] == 90 and report["impact"]["items_30d"] == 120
    assert report["impact"]["hours_saved_30d"] == 12.0                       # 120 items * 6 min
    assert [d["items"] for d in report["trend"][-3:]] == [30, 30, 30]
    host = report["versions"]["hosts"][0]
    assert host["automations"]["auto-a"] == {"version": "0.9.0", "target": "1.0.0", "ok": False} and host["components"]["wrapper-core"]["ok"]
    no_catalog = signals_of(data_of(*runs), manifest())[1]
    assert no_catalog["impact"]["hours_saved_30d"] is None


def test_load_archive_respects_window_and_attaches_path(tmp_path):
    from automation_logging.synthetic import write_archive
    write_archive(tmp_path, [run_record("auto-a", T(1, 9)), run_record("auto-a", T(60, 9))])
    d = analytics.load_archive(tmp_path, NOW, window_days=30)
    assert len(d.runs) == 1 and d.runs[0]["_path"].startswith("run/auto-a/2026/10/13/")


def test_missed_id_is_stable_as_more_runs_are_missed():
    d = data_of(*daily_runs([4, 3, 2]), lifecycle_record("installed", "auto-a", "VDI-1", T(5)))
    first = by_type(signals_of(d, manifest(), now=T(0, 10))[0], "MISSED")
    later = by_type(signals_of(d, manifest(), now=T(-1, 10))[0], "MISSED")
    assert len(first) == len(later) == 1 and first[0]["id"] == later[0]["id"]
