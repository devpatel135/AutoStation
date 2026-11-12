"""Phase 3: ingest that cannot silently lose the newest data, bundles, symlinks, verdict reporting."""
from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

import pytest

from automation_logging import analytics, ingest
from automation_logging.cli import main as cli_main

from factories import NOW, run_record


def _put(root, ring, kind, name, art_id, rec, dated=None):
    d = root / "ingest" / ring / kind / name
    if dated:
        d = d / f"{dated.year:04d}" / f"{dated.month:02d}" / f"{dated.day:02d}"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{art_id}.json").write_text(json.dumps(rec), encoding="utf-8")


def _run(day_offset, n=0):
    t = NOW - timedelta(days=day_offset, hours=n % 20)
    rec = run_record("auto-a", t)
    rec["run_id"] = f"r{day_offset}-{n}"
    rec["ring"] = "pilot"
    return rec, t


def test_cap_defers_the_oldest_files_never_the_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_INGEST_FILES", 3)
    src = tmp_path / "src"
    for off in (1, 2, 3, 4, 5):
        rec, t = _run(off)
        _put(src, "pilot", "run", "auto-a", rec["run_id"], rec, dated=t)
    st = ingest.ingest_dir(src, tmp_path / "arch", keep_source=True, now=NOW)
    assert st["deferred_files"] == 2 and st["archived"] == 3
    got = {p.stem for p in (tmp_path / "arch").rglob("r*.json")}
    assert got == {"r1-0", "r2-0", "r3-0"}


def test_files_older_than_the_window_are_not_read(tmp_path):
    src = tmp_path / "src"
    rec, t = _run(100)
    _put(src, "pilot", "run", "auto-a", rec["run_id"], rec, dated=t)
    st = ingest.ingest_dir(src, tmp_path / "arch", keep_source=True, now=NOW, window_days=60)
    assert st["out_of_window"] == 1 and st["archived"] == 0


def test_cli_ingest_keeps_the_source_unless_consume(tmp_path):
    src = tmp_path / "src"
    rec, t = _run(1)
    _put(src, "pilot", "run", "auto-a", rec["run_id"], rec, dated=t)
    f = next(src.rglob("*.json"))
    assert cli_main(["ingest", "--source", "dir", "--from", str(src), "--out", str(tmp_path / "a1")]) == 0
    assert f.exists()
    assert cli_main(["ingest", "--source", "dir", "--from", str(src), "--out", str(tmp_path / "a2"), "--consume"]) == 0
    assert not f.exists()


def test_cli_exit_3_when_capped_and_verdict_reaches_the_report(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_INGEST_FILES", 1)
    src = tmp_path / "src"
    for off in (1, 2):
        rec, t = _run(off)
        _put(src, "pilot", "run", "auto-a", rec["run_id"], rec, dated=t)
    arch = tmp_path / "arch"
    assert cli_main(["ingest", "--source", "dir", "--from", str(src), "--out", str(arch)]) == 3
    dq = analytics._data_quality(analytics.load_archive(arch, NOW), NOW, {})
    assert dq["ingest_incomplete"] is True and dq["ingest"]["deferred_files"] == 1


def test_bundle_is_unpacked_and_idempotent(tmp_path):
    src = tmp_path / "src"
    rec, t = _run(1)
    bundle = {"schema_version": 1, "kind": "bundle", "items": [
        {"kind": "run", "name": "auto-a", "id": rec["run_id"], "payload": rec}]}
    _put(src, "pilot", "bundle", "host1", "20260101T000000-abcd", bundle, dated=t)
    st = ingest.ingest_dir(src, tmp_path / "arch", keep_source=True, now=NOW)
    assert st["bundles"] == 1 and st["archived"] == 1 and st["rejected"] == 0
    st2 = ingest.ingest_dir(src, tmp_path / "arch", keep_source=True, now=NOW)
    assert st2["archived"] == 0 and st2["duplicates"] == 1


def test_malformed_bundle_items_are_rejected_one_by_one(tmp_path):
    src = tmp_path / "src"
    rec, t = _run(1)
    bundle = {"schema_version": 1, "kind": "bundle", "items": [
        {"kind": "run", "name": "auto-a", "id": rec["run_id"], "payload": rec},
        {"kind": "nonsense", "name": "x", "id": "y", "payload": {}}, "junk"]}
    _put(src, "pilot", "bundle", "host1", "b1", bundle, dated=t)
    st = ingest.ingest_dir(src, tmp_path / "arch", keep_source=True, now=NOW)
    assert st["archived"] == 1 and st["rejected"] == 2


def test_forged_ring_in_content_is_rejected(tmp_path):
    src = tmp_path / "src"
    rec, t = _run(1)
    rec["ring"] = "dev"
    _put(src, "pilot", "run", "auto-a", rec["run_id"], rec, dated=t)
    st = ingest.ingest_dir(src, tmp_path / "arch", keep_source=True, now=NOW)
    assert st["archived"] == 0 and st["rejected"] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_symlinks_are_skipped_not_followed_or_deleted(tmp_path):
    src = tmp_path / "src"
    secret = tmp_path / "secret.json"
    secret.write_text("{}", encoding="utf-8")
    d = src / "ingest" / "pilot" / "run" / "auto-a"
    d.mkdir(parents=True)
    os.symlink(secret, d / "evil.json")
    st = ingest.ingest_dir(src, tmp_path / "arch", now=NOW)      # default deletes processed files
    assert st["symlinks"] == 1 and st["archived"] == 0
    assert secret.exists()


def test_scale_smoke_30_days_of_bundles(tmp_path):
    """1,000 runs/day for 30 days = 30,000 runs; as one bundle per run that is 30k source files. Time-boxed sanity check on a sample."""
    import time
    src = tmp_path / "src"
    n_days, per_day = 5, 200
    for day in range(1, n_days + 1):
        for i in range(per_day):
            rec, t = _run(day, i)
            rec["run_id"] = f"r{day}-{i}"
            bundle = {"schema_version": 1, "kind": "bundle",
                      "items": [{"kind": "run", "name": "auto-a", "id": rec["run_id"], "payload": rec}]}
            _put(src, "pilot", "bundle", f"h{i % 50}", f"b{day}-{i}", bundle, dated=t)
    t0 = time.time()
    st = ingest.ingest_dir(src, tmp_path / "arch", keep_source=True, now=NOW)
    elapsed = time.time() - t0
    assert st["archived"] == n_days * per_day and st["deferred_files"] == 0
    print(f"scale: {st['archived']} runs ingested in {elapsed:.1f}s")
