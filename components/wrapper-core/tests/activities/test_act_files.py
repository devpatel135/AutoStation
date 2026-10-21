from __future__ import annotations

import os
import threading
import time
import zipfile
from pathlib import Path

import pytest

from wrapper_core import retry
from wrapper_core.activities import files
from wrapper_core.retry import DataError


def touch(p: Path, content: str = "x", age_s: float = 10) -> Path:
    p.write_text(content, encoding="utf-8")
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def test_new_files_have_watcher_semantics(tmp_path):
    inbox = tmp_path / "in"
    inbox.mkdir()
    a, b = touch(inbox / "a.csv", age_s=30), touch(inbox / "b.csv", age_s=20)
    touch(inbox / "skip.txt")
    assert files.new_files(inbox, "*.csv", "w") == [a, b]            # oldest first, pattern respected
    assert files.new_files(inbox, "*.csv", "w") == [a, b]            # not marked yet: still new
    files.mark_processed("w", a)
    assert files.new_files(inbox, "*.csv", "w") == [b]
    files.mark_processed("w", b)
    assert files.new_files(inbox, "*.csv", "w") == []                # quiet poll run
    touch(a, "changed content")                                       # same name, new size+mtime => new again
    assert files.new_files(inbox, "*.csv", "w") == [a]
    assert files.new_files(inbox, "*.csv", "other-state") == [b, a]   # states are independent (a was just touched: now the newest)
    files.forget_processed("w")
    assert len(files.new_files(inbox, "*.csv", "w")) == 2
    assert files.new_files(inbox, "*.csv", "m", mark=True) and files.new_files(inbox, "*.csv", "m") == []


def test_new_files_skips_files_still_being_written(tmp_path):
    f = touch(tmp_path / "fresh.csv", age_s=0)
    assert files.new_files(tmp_path, "*.csv", "fresh", min_age_s=5) == []
    assert files.new_files(tmp_path, "*.csv", "fresh", min_age_s=0) == [f]
    assert files.new_files(tmp_path / "does-not-exist") == []


def test_new_files_recursive(tmp_path):
    (tmp_path / "sub").mkdir()
    f = touch(tmp_path / "sub" / "deep.csv")
    assert files.new_files(tmp_path, "*.csv", "r") == []
    assert files.new_files(tmp_path, "*.csv", "r", recursive=True) == [f]


def test_wait_for_file_waits_for_growth_to_finish(tmp_path):
    target = tmp_path / "late.bin"

    def writer():
        time.sleep(0.3)
        with open(target, "wb") as f:
            for _ in range(3):
                f.write(b"chunk")
                f.flush()
                time.sleep(0.2)

    t = threading.Thread(target=writer)
    t.start()
    got = files.wait_for_file(target, timeout=10, stable_for_s=0.5, poll=0.05)
    t.join()
    assert got == target and target.stat().st_size == 15


def test_wait_for_file_timeout_is_classified_as_system_unavailable(tmp_path):
    with pytest.raises(files.FileWaitTimeout) as ei:
        files.wait_for_file(tmp_path / "never", timeout=0.3, poll=0.05)
    assert retry.classify_exception(ei.value) is retry.Category.SYSTEM_UNAVAILABLE
    assert isinstance(ei.value, TimeoutError)


def test_safe_copy_never_clobbers_and_is_atomic(tmp_path):
    src = touch(tmp_path / "doc.txt", "payload")
    dest = tmp_path / "out"
    first = files.safe_copy(src, dest / "doc.txt")
    second = files.safe_copy(src, dest)                               # directory target keeps the name
    third = files.safe_copy(src, dest / "doc.txt")
    assert [p.name for p in (first, second, third)] == ["doc.txt", "doc (1).txt", "doc (2).txt"]
    assert all(p.read_text() == "payload" for p in (first, second, third))
    touch(src, "new")
    files.safe_copy(src, dest / "doc.txt", overwrite=True)
    assert first.read_text() == "new"
    assert not list(dest.glob(".amx-*")), "no temp files left behind"
    with pytest.raises(DataError):
        files.safe_copy(tmp_path / "missing.txt", dest)


def test_safe_copy_verification_failure_cleans_up(tmp_path, monkeypatch):
    src = touch(tmp_path / "a.txt", "payload")
    calls = iter(["aaa", "bbb"])
    monkeypatch.setattr(files, "sha256_of", lambda p: next(calls))
    with pytest.raises(DataError):
        files.safe_copy(src, tmp_path / "out" / "a.txt")
    assert not list((tmp_path / "out").iterdir())


def test_safe_move_removes_source_only_after_success(tmp_path):
    src = touch(tmp_path / "m.txt", "data")
    dst = files.safe_move(src, tmp_path / "moved")
    assert not src.exists() and dst.read_text() == "data"


def test_zip_roundtrip_and_zip_slip_defences(tmp_path):
    root = tmp_path / "tree"
    (root / "d").mkdir(parents=True)
    touch(root / "a.txt", "A")
    touch(root / "d" / "b.txt", "B")
    z = files.zip_dir(root, tmp_path / "t.zip")
    out = tmp_path / "x"
    got = files.unzip_safe(z, out)
    assert (out / "a.txt").read_text() == "A" and (out / "d" / "b.txt").read_text() == "B" and len(got) == 2
    z2 = files.zip_dir(root, tmp_path / "t2.zip", include_root=True)
    files.unzip_safe(z2, tmp_path / "y")
    assert (tmp_path / "y" / "tree" / "a.txt").exists()

    for evil in ("../escape.txt", "/abs.txt", "C:/drive.txt", "a/../../up.txt", "..\\win.txt"):
        bad = tmp_path / f"bad-{abs(hash(evil))}.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr(evil, "x")
        with pytest.raises(DataError):
            files.unzip_safe(bad, tmp_path / "safe")
    assert not (tmp_path / "escape.txt").exists()
    many = tmp_path / "many.zip"
    with zipfile.ZipFile(many, "w") as zf:
        for i in range(5):
            zf.writestr(f"f{i}", "x")
    with pytest.raises(DataError):
        files.unzip_safe(many, tmp_path / "m", max_files=3)
    with pytest.raises(DataError):
        files.unzip_safe(many, tmp_path / "m2", max_total_bytes=2)
    notzip = touch(tmp_path / "n.zip", "not a zip")
    with pytest.raises(DataError):
        files.unzip_safe(notzip, tmp_path / "n")


def test_cleanup_older_than(tmp_path):
    old = touch(tmp_path / "old.log", age_s=3 * 86400)
    new = touch(tmp_path / "new.log", age_s=60)
    keep = touch(tmp_path / "old.txt", age_s=3 * 86400)
    assert files.cleanup_older_than(tmp_path, days=1, pattern="*.log", dry_run=True) == [old] and old.exists()
    assert files.cleanup_older_than(tmp_path, days=1, pattern="*.log") == [old]
    assert not old.exists() and new.exists() and keep.exists()
    with pytest.raises(ValueError):
        files.cleanup_older_than(tmp_path)


def test_workdir_is_cleaned_up_and_lives_in_the_run_dir(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    monkeypatch.setenv("AUTOMAGIX_RUN_DIR", str(run_dir))
    with files.workdir("job") as d:
        assert d.parent == run_dir and d.name.startswith("job-")
        (d / "scratch.txt").write_text("x")
        assert d.exists()
    assert not d.exists()
    with pytest.raises(RuntimeError):
        with files.workdir() as d2:
            raise RuntimeError("boom")
    assert not d2.exists()
