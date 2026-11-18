"""Hermetic-test guard shared by every component's tests/conftest.py (wrapper-core keeps its own, older copy of the schtasks part).

A test run must never change the developer's machine. Three things have bitten us or could:

* ``schtasks``: a leaked ``/Create`` whose start time has passed makes Windows run the real automation and leaves a stray task behind.
  Any ``schtasks`` call through ``subprocess.run`` / ``subprocess.Popen`` / ``subprocess.call`` / ``check_call`` / ``check_output`` is answered
  by a harmless fake and recorded in ``guard.schtasks_calls``.
* the real ``%LOCALAPPDATA%\\AUTOMAGIX`` home: ``LOCALAPPDATA`` and ``APPDATA`` are redirected into the test's tmp folder, and after each test the
  real folder is compared with how it was before; a difference FAILS the test.
* the real Startup folder (``%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup``): same redirect and the same after-test comparison.

Usage in a conftest::

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools" / "tests"))
    from _hermetic import hermetic_fixture
    _hermetic = hermetic_fixture()          # an autouse fixture; the returned object is also usable as a normal fixture name
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterator

import warnings

import pytest

_SCHTASKS_NAMES = ("schtasks", "schtasks.exe")


def real_locations() -> dict:
    """The real, un-redirected folders to protect (empty entries when the variable does not exist, e.g. on Linux)."""
    out = {}
    local = os.environ.get("LOCALAPPDATA")
    roaming = os.environ.get("APPDATA")
    if local:
        out["automagix_home"] = Path(local) / "AUTOMAGIX"
    if roaming:
        out["startup"] = Path(roaming) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return out


_REAL = real_locations()          # captured at import time, before any test redirects the environment


def fingerprint(root: Path, depth: int = 3, limit: int = 4000) -> tuple:
    """A cheap, order-independent picture of a folder: (relative path, size, mtime_ns) for up to ``limit`` entries ``depth`` levels deep.
    A folder that does not exist fingerprints as ``()``; an unreadable one as ``("unreadable",)``."""
    if not root.exists():
        return ()
    entries = []

    def walk(d: Path, level: int) -> None:
        if len(entries) >= limit:
            return
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    entries.append((os.path.relpath(e.path, root), st.st_size, st.st_mtime_ns))
                    if level < depth and e.is_dir(follow_symlinks=False):
                        walk(Path(e.path), level + 1)
        except OSError:
            entries.append(("unreadable", 0, 0))

    walk(root, 1)
    return tuple(sorted(entries))


def _is_schtasks(args) -> bool:
    first = args[0] if isinstance(args, (list, tuple)) and args else args
    return isinstance(first, (str, os.PathLike)) and os.path.basename(os.fspath(first)).lower() in _SCHTASKS_NAMES


class _FakePopen:
    """Just enough of Popen for a code path that starts schtasks and waits for it."""

    def __init__(self, args, *a, **k):
        self.args = args
        self.returncode = 0
        self.pid = 0
        self.stdin = self.stdout = self.stderr = None

    def communicate(self, input=None, timeout=None):
        return ("", "")

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def kill(self):
        pass

    terminate = kill

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Guard:
    def __init__(self) -> None:
        self.schtasks_calls: list = []


def install(monkeypatch, tmp_path: Path) -> Guard:
    """Patch subprocess and redirect the per-user folders for ONE test. Returns the guard (it records the blocked schtasks calls)."""
    guard = Guard()
    real_run, real_popen = subprocess.run, subprocess.Popen
    real_call, real_check_call, real_check_output = subprocess.call, subprocess.check_call, subprocess.check_output

    def record(args) -> None:
        guard.schtasks_calls.append(list(args) if isinstance(args, (list, tuple)) else [args])

    def run(args, *a, **k):
        if _is_schtasks(args):
            record(args)
            return subprocess.CompletedProcess(args, 0, "" if k.get("text") or k.get("universal_newlines") else b"", "" if k.get("text") or k.get("universal_newlines") else b"")
        return real_run(args, *a, **k)

    def popen(args, *a, **k):
        if _is_schtasks(args):
            record(args)
            return _FakePopen(args)
        return real_popen(args, *a, **k)

    def call(args, *a, **k):
        if _is_schtasks(args):
            record(args)
            return 0
        return real_call(args, *a, **k)

    def check_call(args, *a, **k):
        if _is_schtasks(args):
            record(args)
            return 0
        return real_check_call(args, *a, **k)

    def check_output(args, *a, **k):
        if _is_schtasks(args):
            record(args)
            return "" if k.get("text") or k.get("universal_newlines") else b""
        return real_check_output(args, *a, **k)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "call", call)
    monkeypatch.setattr(subprocess, "check_call", check_call)
    monkeypatch.setattr(subprocess, "check_output", check_output)

    # per-user folders: anything that resolves through these lands in the test's own tmp folder
    fake_local = tmp_path / "_hermetic_localappdata"
    fake_roaming = tmp_path / "_hermetic_appdata"
    fake_local.mkdir(parents=True, exist_ok=True)
    fake_roaming.mkdir(parents=True, exist_ok=True)
    if "LOCALAPPDATA" in os.environ:
        monkeypatch.setenv("LOCALAPPDATA", str(fake_local))
    if "APPDATA" in os.environ:
        monkeypatch.setenv("APPDATA", str(fake_roaming))
    if "AUTOMAGIX_HOME" not in os.environ:
        monkeypatch.setenv("AUTOMAGIX_HOME", str(tmp_path / "_hermetic_home"))
    if "AUTOMAGIX_STARTUP_DIR" not in os.environ:
        monkeypatch.setenv("AUTOMAGIX_STARTUP_DIR", str(tmp_path / "_hermetic_startup"))
    return guard


class RealFolderTouched(AssertionError):
    pass


def guard_body(tmp_path, monkeypatch, check_real_folders: bool = True) -> Iterator[Guard]:
    """The fixture body as a plain generator (the pytest fixture below wraps it, and the guard's own tests drive it by hand)."""
    # AMX_TEST_SKIP_REAL_FOLDER_CHECK=1 turns the after-test comparison off (a real AutoStation install on the same machine writes to
    # its own home while the suite runs); the schtasks fake and the environment redirect stay on
    check = check_real_folders and not os.environ.get("AMX_TEST_SKIP_REAL_FOLDER_CHECK")
    before = {k: fingerprint(v) for k, v in _REAL.items()} if check else {}
    guard = install(monkeypatch, tmp_path)
    yield guard
    if check:
        changed = [k for k, v in _REAL.items() if fingerprint(v) != before[k]]
        if changed:
            message = ("this test changed the machine's real " + " and ".join(f"{k} ({_REAL[k]})" for k in changed)
                       + " - tests must write only under tmp_path (set AUTOMAGIX_HOME / AUTOMAGIX_STARTUP_DIR, or pass paths in)")
            # CI has no real AutoStation install, so a change there IS a leaking test: fail. On a developer machine the real automation (or
            # its scheduled task or self-heal) legitimately writes to the same folder while the suite runs, so there it is only a warning
            # (AMX_TEST_STRICT_REAL_FOLDER=1 forces the failure).
            if os.environ.get("CI") or os.environ.get("AMX_TEST_STRICT_REAL_FOLDER"):
                raise RealFolderTouched(message)
            warnings.warn(message + " (warning only: a real AutoStation install on this machine may have written there; set AMX_TEST_STRICT_REAL_FOLDER=1 to fail)")


def hermetic_fixture(*, check_real_folders: bool = True):
    """Build the autouse fixture for a conftest. Fails the test that changed the real AUTOMAGIX home or Startup folder."""

    @pytest.fixture(autouse=True)
    def _hermetic(tmp_path, monkeypatch) -> Iterator[Guard]:
        yield from guard_body(tmp_path, monkeypatch, check_real_folders)

    return _hermetic
