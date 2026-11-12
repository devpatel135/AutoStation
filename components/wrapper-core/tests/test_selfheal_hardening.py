"""Phase 1 (1.3): self-heal lock, orphan recovery, marker semantics, secret_refresh opt-in, pairing, deadline, prechecks."""
from __future__ import annotations

import os
import time
import zipfile

import pytest

from wrapper_core import selfheal
from wrapper_core.locking import FileLock
from wrapper_core.paths import Paths


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)


def test_recover_orphan_restores_missing_target_from_old(tmp_path):
    p = Paths(tmp_path).ensure()
    import shutil
    shutil.rmtree(p.wrapper_core, ignore_errors=True)
    old = p.wrapper_core.with_name("wrapper-core.old")
    old.mkdir(parents=True)
    (old / "VERSION").write_text("1.0.0\n")
    notes = selfheal.recover_orphans(p)
    assert (p.wrapper_core / "VERSION").read_text().strip() == "1.0.0" and not old.exists()
    assert any("restored wrapper-core" in n for n in notes)


def test_recover_removes_only_stale_staging(tmp_path):
    p = Paths(tmp_path).ensure()
    stale, fresh = p.root / "execution-framework.staging-1", p.root / "execution-framework.staging-2"
    for d in (stale, fresh):
        d.mkdir(parents=True)
    old_t = time.time() - 3600
    os.utime(stale, (old_t, old_t))
    selfheal.recover_orphans(p)
    assert not stale.exists() and fresh.exists()


def test_missing_version_marker_means_not_installed(tmp_path):
    p = Paths(tmp_path).ensure()
    p.framework.mkdir(parents=True, exist_ok=True)
    selfheal.write_installed(p, {"components": {"execution-framework": "9.9.9"}, "automations": {}})
    assert selfheal.installed_component_version(p, "execution-framework") is None
    (p.framework / "VERSION").write_text("1.2.3\n")
    assert selfheal.installed_component_version(p, "execution-framework") == "1.2.3"
    adir = p.automation_dir("auto-a")
    adir.mkdir(parents=True, exist_ok=True)
    assert selfheal.installed_automation_version(p, "auto-a") is None


def test_staging_dir_is_unique_per_process(tmp_path):
    t = tmp_path / "execution-framework"
    assert selfheal.staging_dir(t).name == f"execution-framework.staging-{os.getpid()}"
    assert selfheal.is_leftover(selfheal.staging_dir(t).name) and selfheal.is_leftover("x.old")


def test_second_pass_is_skipped_while_the_lock_is_held(tmp_path):
    p = Paths(tmp_path).ensure()
    other = FileLock(p.state / "selfheal.lock", ttl_s=900)
    import subprocess, sys
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (p.state / "selfheal.lock").write_text(f"{child.pid} {time.time()} 0")
        res = selfheal.run(p, {"ring": "dev", "selfheal_lock_wait_s": 0.2}, None)
        assert any("another self-heal pass" in e for e in res.errors)
    finally:
        child.kill(); child.wait()


def test_path_length_precheck(tmp_path):
    z = tmp_path / "p.zip"
    _zip(z, {"a/" + "b" * 100 + ".txt": "x"})
    with pytest.raises(selfheal.HealError, match="exceed"):
        selfheal.safe_extract(z, tmp_path / "out", max_path=len(str(tmp_path)) + 50)
    selfheal.safe_extract(z, tmp_path / "ok", max_path=0)


def test_free_space_precheck(tmp_path):
    with pytest.raises(selfheal.HealError, match="free disk space"):
        selfheal.precheck_space(tmp_path, need=1 << 60)


def test_deadline_aborts_downloads(tmp_path):
    d = selfheal.Deadline(0)
    with pytest.raises(selfheal.HealError, match="deadline"):
        selfheal.fetch_file("http://127.0.0.1:1/x", tmp_path / "x", deadline=d)


def test_download_streams_to_disk_and_verifies(tmp_path):
    import hashlib
    src = tmp_path / "pkg.zip"
    _zip(src, {"f.txt": "hello"})
    stage = tmp_path / "home" / "x.staging-1"
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    selfheal.download_and_stage(str(src), sha, stage, None)
    assert (stage / "f.txt").read_text() == "hello"
    with pytest.raises(selfheal.HealError, match="sha256"):
        selfheal.download_and_stage(str(src), "0" * 64, tmp_path / "home" / "y.staging-1", None)
