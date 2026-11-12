"""Phase 1 (1.1): run the REAL bin\apply-pending.cmd against fake install folders (crash windows, failing self-test, run lock). Windows only."""
from __future__ import annotations

import subprocess
import sys

import pytest

from wrapper_core.paths import Paths
from wrapper_core.scheduler import Scheduler

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the launcher scripts are batch files")


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "amx"
    p = Paths(root).ensure()
    fake_py = tmp_path / "fakepy.cmd"
    # stands in for python.exe: the self-test "fails" when the freshly swapped copy contains a BAD marker file
    fake_py.write_text(r'@echo off' + '\r\n' + r'if exist "%AMX_ROOT%\wrapper-core\BAD" exit /b 1' + '\r\n' + r'exit /b 0' + '\r\n')
    Scheduler(p, python_exe=str(fake_py)).write_apply_pending()
    (p.root / "logs").mkdir(exist_ok=True)
    return p


def _core(p, name, version, bad=False):
    d = p.root / name
    d.mkdir(parents=True)
    (d / "VERSION").write_text(version + "\n")
    if bad:
        (d / "BAD").write_text("x")
    return d


def _run(p):
    r = subprocess.run(["cmd", "/c", str(p.bin / "apply-pending.cmd")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def _ver(d):
    return (d / "VERSION").read_text().strip()


def test_good_pending_is_swapped_and_the_old_copy_kept_until_verified(home):
    import shutil
    shutil.rmtree(home.wrapper_core)
    _core(home, "wrapper-core", "1.0.0")
    _core(home, "wrapper-core.pending", "2.0.0")
    _run(home)
    assert _ver(home.wrapper_core) == "2.0.0" and not (home.root / "wrapper-core.pending").exists()
    assert _ver(home.root / "wrapper-core.old") == "1.0.0"          # the rollback copy survives the successful swap


def test_failing_selftest_reverts_and_records_the_rejected_version(home):
    import shutil
    shutil.rmtree(home.wrapper_core)
    _core(home, "wrapper-core", "1.0.0")
    _core(home, "wrapper-core.pending", "2.0.0", bad=True)
    _run(home)
    assert _ver(home.wrapper_core) == "1.0.0" and not (home.root / "wrapper-core.pending").exists()
    assert (home.root / "wrapper-core.bad").is_dir()
    assert (home.state / "rejected-wrapper-core.txt").read_text().strip() == "2.0.0"
    from wrapper_core import selfheal
    assert "2.0.0" in selfheal.rejected_wrapper_versions(home)


def test_crash_between_the_two_renames_is_repaired_by_the_next_launcher(home):
    import shutil
    shutil.rmtree(home.wrapper_core)
    _core(home, "wrapper-core.old", "1.0.0")                          # the swap died after renaming current -> .old
    _run(home)
    assert _ver(home.wrapper_core) == "1.0.0" and not (home.root / "wrapper-core.old").exists()


def test_crash_with_pending_still_present_completes_the_update(home):
    import shutil
    shutil.rmtree(home.wrapper_core)
    _core(home, "wrapper-core.old", "1.0.0")
    _core(home, "wrapper-core.pending", "2.0.0")
    _run(home)
    assert _ver(home.wrapper_core) == "2.0.0"


def test_nothing_is_swapped_while_a_run_holds_a_lock(home):
    import shutil
    shutil.rmtree(home.wrapper_core)
    _core(home, "wrapper-core", "1.0.0")
    _core(home, "wrapper-core.pending", "2.0.0")
    (home.state / "locks").mkdir(parents=True, exist_ok=True)
    (home.state / "locks" / "auto-a.lock").write_text("1 0 0")
    _run(home)
    assert _ver(home.wrapper_core) == "1.0.0" and (home.root / "wrapper-core.pending").is_dir()


def test_no_pending_is_a_noop(home):
    before = _ver(home.wrapper_core) if (home.wrapper_core / "VERSION").exists() else None
    _run(home)
    assert (_ver(home.wrapper_core) if (home.wrapper_core / "VERSION").exists() else None) == before
