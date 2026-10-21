"""Second hardening round: forged timestamps, hostile hosts, ingest floods and symlinks, signal isolation."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

from automation_logging import analytics, ingest, reliability, validate
from automation_logging.common import iso

from factories import NOW, T, data_of, lifecycle_record, manifest, run_record, signals_of
from automation_logging.synthetic import heartbeat_record


def _run(**over):
    rec = run_record("auto-a", T(1, 9, 0))
    rec.update(over)
    return rec


# ------------------------------------------------------------------------------------------------ timestamps
@pytest.mark.parametrize("ts", ["9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+14:00", "2099-01-01T00:00:00Z",
                                "2019-12-31T23:59:59Z", "not a time", "", "2026-13-45T00:00:00Z"])
def test_forged_or_absurd_timestamps_are_rejected(ts):
    assert validate.validate_record(_run(started_at=ts), "run")


def test_a_heartbeat_dated_in_the_future_is_rejected_so_it_cannot_mask_silence():
    hb = heartbeat_record("VDI-1", datetime.now(timezone.utc) + timedelta(days=30))
    assert "bad ts" in validate.validate_record(hb, "heartbeat")


def test_small_clock_skew_is_tolerated():
    rec = _run(started_at=iso(datetime.now(timezone.utc) + timedelta(minutes=2)))
    assert validate.validate_record(rec, "run") is None


def test_timestamps_are_normalised_to_utc_so_string_order_is_time_order():
    rec = _run(started_at="2026-01-01T10:00:00+05:00", ended_at="2026-01-01T10:05:00+05:00")
    assert validate.validate_record(rec, "run") is None
    validate.normalize_timestamps(rec)
    assert rec["started_at"] == "2026-01-01T05:00:00.000Z" and rec["ended_at"] == "2026-01-01T05:05:00.000Z"


def test_mttr_is_never_negative_with_mixed_offsets():
    a = _run(run_id="r1", status="failed", started_at="2026-01-01T10:00:00+05:00")   # = 05:00Z
    b = _run(run_id="r2", status="success", started_at="2026-01-01T07:00:00Z")       # 07:00Z, later although the string sorts earlier
    for r in (a, b):
        validate.normalize_timestamps(r)
    eps = reliability.episodes(reliability.series([a, b]))
    assert len(eps) == 1 and eps[0]["end"] is not None and eps[0]["end"] - eps[0]["start"] == timedelta(hours=2)


def test_hostile_run_host_names_are_rejected():
    assert validate.validate_record(_run(host="evil host\n# injected"), "run")
    assert validate.validate_record(_run(host="x" * 500), "run")
    assert validate.validate_record(_run(host="VDI-01.corp"), "run") is None


# ------------------------------------------------------------------------------------------------ ingest floods
def _drop(root, name, content, kind="run"):
    d = root / "ingest" / "pilot" / kind / "auto-a"
    d.mkdir(parents=True, exist_ok=True)
    f = d / (name + ".json")
    f.write_text(content, encoding="utf-8")
    return f


def test_rejected_quarantine_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_REJECTED_FILES", 5)
    src = tmp_path / "logs"
    for i in range(20):
        _drop(src, f"bad{i}", "{ not json " + str(i))
    stats = ingest.ingest_dir(src, tmp_path / "archive")
    assert stats["rejected"] == 20 and stats["rejected_unstored"] == 15
    assert len(list((tmp_path / "archive" / "_rejected").glob("*.txt"))) == 5


def test_file_count_cap_defers_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_INGEST_FILES", 3)
    src = tmp_path / "logs"
    for i in range(7):
        _drop(src, f"bad{i}", "nope")
    assert ingest.ingest_dir(src, tmp_path / "archive")["deferred_files"] == 4


@pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs privileges on Windows")
def test_symlinked_artefacts_are_never_followed(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-VALUE")
    src = tmp_path / "logs"
    d = src / "ingest" / "pilot" / "run" / "auto-a"
    d.mkdir(parents=True)
    os.symlink(secret, d / "evil.json")
    stats = ingest.ingest_dir(src, tmp_path / "archive")
    assert stats["rejected"] == 1
    assert not any("TOP-SECRET" in p.read_text(errors="ignore") for p in (tmp_path / "archive").rglob("*") if p.is_file())


def test_forged_record_cannot_crash_compute_signals(monkeypatch):
    """One signal builder raising must be isolated: the other signals still compute and the error is reported in state."""
    runs = [run_record("auto-a", T(0, 8, 0), status="failed")]
    d = data_of(*runs, lifecycle_record("installed", "auto-a", "VDI-1", T(5)))
    real = analytics.wc_manifest.in_maintenance

    def boom(*a, **k):
        raise OverflowError("date value out of range")

    monkeypatch.setattr(analytics.wc_manifest, "in_maintenance", boom)
    sigs, _, state = signals_of(d, manifest())
    monkeypatch.setattr(analytics.wc_manifest, "in_maintenance", real)
    assert any(s["type"] == "FAILED" for s in sigs)
    assert state.get("signal_errors")


def test_signals_for_unregistered_automations_are_marked():
    runs = [run_record("ghost-auto", T(0, 8, 0), status="failed")]
    sigs, _, _ = signals_of(data_of(*runs), manifest())
    ghost = [s for s in sigs if s["automation"] == "ghost-auto"]
    assert ghost and all(s["registered"] is False for s in ghost)


def test_forged_uninstall_for_unknown_automation_cannot_hide_a_silent_host():
    hb = heartbeat_record("VDI-1", T(5))
    hb["versions"] = {"automations": {"auto-a": "1.0.0"}}
    forged = lifecycle_record("uninstalled", "auto-a", "VDI-1", T(4))
    sigs, _, _ = signals_of(data_of(hb, forged), {"automations": {}})      # auto-a is not in this manifest: the event is ignored
    assert any(s["type"] == "SILENT" for s in sigs)


def test_a_row_that_cannot_render_is_shown_as_omitted_not_silently_dropped():
    from automation_logging import dashboard
    d = data_of(run_record("auto-a", T(0, 8, 0), status="failed"), lifecycle_record("installed", "auto-a", "VDI-1", T(5)))
    sigs, report, _ = signals_of(d, manifest())
    assert report["signals"]
    report["signals"][0].pop("severity")                      # a record the renderer cannot handle
    html = dashboard.render(report, [])
    assert "row omitted" in html and "could not be rendered and were omitted" in html


def test_report_carries_data_quality_that_consumers_can_trust_or_refuse():
    d = data_of(run_record("auto-a", T(0, 8, 0)), heartbeat_record("VDI-1", T(0, 8, 5)), lifecycle_record("installed", "auto-a", "VDI-1", T(5)))
    _, report, _ = signals_of(d, manifest())
    dq = report["data_quality"]
    assert dq["records_loaded"] == 3 and dq["signal_errors"] == [] and dq["newest_record_age_h"] is not None


def test_on_time_scales_to_a_hostile_archive():
    import time
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    expected = [("s", base + timedelta(minutes=5 * i)) for i in range(2000)]
    runs = [dict(run_record("auto-a", base + timedelta(seconds=i * 7)), slot="s") for i in range(10000)]
    t0 = time.time()
    res = reliability.on_time(expected, runs, timedelta(minutes=15), base + timedelta(days=30))
    assert time.time() - t0 < 5 and res["expected"] == 2000 and res["on_time"] + res["late"] + res["missed"] == 2000 and res["on_time"] > 100
