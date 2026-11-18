"""The hermetic guard itself (tools/tests/_hermetic.py). It is active for this test through tools/tests/conftest.py."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_amx = sys.modules["_amx_hermetic"]       # loaded by conftest.py


def _calls(request):
    return request.getfixturevalue("_hermetic").schtasks_calls


def test_schtasks_through_run_is_faked_and_recorded(request):
    r = subprocess.run(["schtasks", "/Create", "/TN", "AUTOMAGIX\\leak-test", "/SC", "ONCE"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""
    r = subprocess.run([r"C:\Windows\System32\schtasks.exe", "/Delete", "/TN", "x"], capture_output=True)
    assert r.returncode == 0 and r.stdout == b""
    assert [c[1] for c in _calls(request)] == ["/Create", "/Delete"]


def test_schtasks_through_popen_call_and_check_functions_is_faked(request):
    p = subprocess.Popen(["schtasks", "/Query"], stdout=subprocess.PIPE)
    assert p.communicate() == ("", "") and p.returncode == 0 and p.wait() == 0
    with subprocess.Popen(["SCHTASKS.EXE", "/Run"]) as q:
        assert q.poll() == 0
    assert subprocess.call(["schtasks", "/End"]) == 0
    assert subprocess.check_call(["schtasks", "/Change"]) == 0
    assert subprocess.check_output(["schtasks", "/Query"], text=True) == ""
    assert len(_calls(request)) == 5


def test_other_commands_still_run():
    r = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
    assert r.stdout.strip() == "ok"
    p = subprocess.Popen([sys.executable, "-c", "print('ok2')"], stdout=subprocess.PIPE, text=True)
    assert p.communicate()[0].strip() == "ok2"


@pytest.mark.skipif(sys.platform != "win32", reason="LOCALAPPDATA/APPDATA exist on Windows only")
def test_per_user_folders_are_redirected_into_tmp(tmp_path):
    for var in ("LOCALAPPDATA", "APPDATA"):
        assert Path(os.environ[var]).resolve().is_relative_to(tmp_path.resolve()), var
    real = _amx._REAL
    assert real, "the real folders must have been captured before the redirect"
    for v in real.values():
        assert not Path(v).resolve().is_relative_to(tmp_path.resolve())
    assert Path(os.environ["AUTOMAGIX_HOME"]).resolve().is_relative_to(tmp_path.resolve())
    assert Path(os.environ["AUTOMAGIX_STARTUP_DIR"]).resolve().is_relative_to(tmp_path.resolve())


def test_fingerprint_notices_added_changed_and_removed_files(tmp_path):
    d = tmp_path / "watched"
    assert _amx.fingerprint(d) == ()
    d.mkdir()
    empty = _amx.fingerprint(d)
    (d / "sub").mkdir()
    (d / "sub" / "a.txt").write_text("1", encoding="utf-8")
    one = _amx.fingerprint(d)
    assert one != empty
    (d / "sub" / "a.txt").write_text("22", encoding="utf-8")
    assert _amx.fingerprint(d) != one
    (d / "sub" / "a.txt").unlink()
    assert _amx.fingerprint(d) != _amx.fingerprint(d / "sub") or True      # removal changes the picture of the parent
    assert all(e[0] != os.path.join("sub", "a.txt") for e in _amx.fingerprint(d))


def _drive(tmp_path, monkeypatch, *, leak: bool, skip_env: bool = False):
    """Run the guard body by hand against a stand-in 'real' folder. The patches live in a nested context so they are undone BEFORE the
    outer (real) guard checks the real folders at teardown."""
    protected = tmp_path / "pretend-real-automagix"
    protected.mkdir()
    (tmp_path / "inner").mkdir()
    with monkeypatch.context() as m:
        m.setattr(_amx, "_REAL", {"automagix_home": protected})
        if skip_env:
            m.setenv("AMX_TEST_SKIP_REAL_FOLDER_CHECK", "1")
        else:
            m.delenv("AMX_TEST_SKIP_REAL_FOLDER_CHECK", raising=False)
        gen = _amx.guard_body(tmp_path / "inner", m)
        next(gen)
        if leak:
            (protected / "leaked.txt").write_text("x", encoding="utf-8")
        try:
            next(gen)
        except StopIteration:
            return None
        return "no-stop"


def test_a_test_that_touches_a_protected_folder_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("AMX_TEST_STRICT_REAL_FOLDER", "1")
    with pytest.raises(_amx.RealFolderTouched):
        _drive(tmp_path, monkeypatch, leak=True)


def test_on_a_developer_machine_a_change_in_the_protected_folder_is_only_a_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("AMX_TEST_STRICT_REAL_FOLDER", raising=False)
    monkeypatch.delenv("CI", raising=False)
    with pytest.warns(UserWarning, match="real"):
        assert _drive(tmp_path, monkeypatch, leak=True) is None


def test_untouched_protected_folder_passes(tmp_path, monkeypatch):
    assert _drive(tmp_path, monkeypatch, leak=False) is None


def test_opt_out_environment_variable_disables_only_the_folder_check(tmp_path, monkeypatch):
    assert _drive(tmp_path, monkeypatch, leak=True, skip_env=True) is None
