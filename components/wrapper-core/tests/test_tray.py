from __future__ import annotations

import ctypes
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from wrapper_core import _trayicon, tray
from wrapper_core import runner as runner_mod
from wrapper_core import runctx
from wrapper_core.paths import Paths

from conftest import HELLO
from test_runner import setup_reg

WIN_ENV = {}      # an environment with none of the off-switches (the autouse fixture sets AUTOMAGIX_NO_TOAST for the real env)


# ---- pure text helpers -----------------------------------------------------------------------------------------------------
def test_sanitize_name_strips_control_bidi_and_bounds_length():
    assert tray.sanitize_name("a\r\nb\tc") == "a b c"
    assert tray.sanitize_name("evil\u202etxt.exe") == "evil txt.exe"
    assert tray.sanitize_name("x" * 500) == "x" * tray.MAX_NAME
    assert tray.sanitize_name("") == "automation" and tray.sanitize_name(None) == "automation"
    assert "\x00" not in tray.sanitize_name("a\x00b")


def test_tooltip_fits_the_windows_limit_and_shows_elapsed():
    tip = tray.tooltip("invoice-router", 125)
    assert tip == "AUTOSTATION - invoice-router running - 02:05"
    assert len(tray.tooltip("n" * 1000, 99999)) <= tray.MAX_TIP
    assert tray.format_elapsed(3725) == "1:02:05" and tray.format_elapsed(-5) == "00:00"


def test_site_url_is_derived_from_registry_url_and_https_only():
    assert tray.site_url({"registry_url": "https://o.github.io/AutoStation/registry/"}) == "https://o.github.io/AutoStation/"
    assert tray.site_url({"registry_url": "https://o.github.io/AutoStation/registry"}) == "https://o.github.io/AutoStation/"
    for bad in ("http://o.github.io/x/registry/", "https://u:p@o.github.io/registry/", "https://o.github.io:8443/registry/",
                "file:///c:/x", "javascript:alert(1)", "", "https://exa mple.com/registry/", "https://o.github.io/a b/registry/"):
        assert tray.site_url({"registry_url": bad}) is None, bad
    assert tray.site_url({}) is None


# ---- switches --------------------------------------------------------------------------------------------------------------
def test_disabled_reason_switches():
    on = dict(cfg={}, descriptor={}, env=WIN_ENV, platform="win32")
    assert tray.disabled_reason(**on) is None
    assert "not Windows" in tray.disabled_reason({}, {}, WIN_ENV, "linux")
    assert "NO_TRAY" in tray.disabled_reason({}, {}, {"AUTOMAGIX_NO_TRAY": "1"}, "win32")
    assert "NO_TOAST" in tray.disabled_reason({}, {}, {"AUTOMAGIX_NO_TOAST": "1"}, "win32")
    assert "pytest" in tray.disabled_reason({}, {}, {"PYTEST_CURRENT_TEST": "x"}, "win32")
    assert "dry run" in tray.disabled_reason({}, {}, WIN_ENV, "win32", dry_run=True)
    assert "platform.json" in tray.disabled_reason({"tray": False}, {}, WIN_ENV, "win32")
    assert "automation.yaml" in tray.disabled_reason({}, {"tray": False}, WIN_ENV, "win32")
    assert tray.disabled_reason({"tray": True}, {"tray": True}, WIN_ENV, "win32") is None


def test_automation_descriptor_reads_tray_flag(paths):
    adir = paths.automation_dir("auto-t")
    adir.mkdir(parents=True)
    (adir / "automation.yaml").write_text("name: auto-t\nentrypoint: main.py\ntray: false\n", encoding="utf-8")
    assert tray.automation_descriptor(paths, "auto-t").get("tray") is False
    assert tray.automation_descriptor(paths, "missing") == {}


# ---- liveness --------------------------------------------------------------------------------------------------------------
def test_pid_alive_for_self_dead_and_bogus():
    assert tray.pid_alive(os.getpid()) is True
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert tray.pid_alive(child.pid) is False
    for bogus in (0, -1, None, "abc"):
        assert tray.pid_alive(bogus) is False


# ---- helper command / start / stop (fake Popen) -----------------------------------------------------------------------------
class FakeProc:
    def __init__(self, exits_after_polls=None):
        self.polls, self.exits_after, self.terminated, self.waited = 0, exits_after_polls, False, False

    def poll(self):
        self.polls += 1
        return 0 if self.exits_after is not None and self.polls > self.exits_after else None

    def terminate(self):
        self.terminated = True
        self.exits_after = -1

    def wait(self, timeout=None):
        self.waited = True
        return 0


def test_helper_command_is_an_argument_list_without_shell_metacharacter_risk(tmp_path):
    cmd = tray.helper_command("a; calc & b", 4242, tmp_path / "s.stop", tmp_path / "runs", "https://o.github.io/r/")
    assert isinstance(cmd, list) and cmd[1:4] == ["-m", "wrapper_core", "tray"]
    assert cmd[cmd.index("--pid") + 1] == "4242" and cmd[cmd.index("--url") + 1] == "https://o.github.io/r/"
    assert cmd[cmd.index("--name") + 1] == "a; calc & b"       # one argv element: never parsed by a shell
    assert "--url" not in tray.helper_command("x", 1, tmp_path / "s", tmp_path, None)


def test_start_for_run_launches_a_detached_helper(paths):
    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return FakeProc()

    h = tray.start_for_run(paths, "auto-a", {"registry_url": "https://o.github.io/r/registry/"}, popen=fake_popen, env=WIN_ENV, platform="win32")
    assert h is not None and h.stop_file.parent == paths.state / "tray"
    assert seen["cmd"][seen["cmd"].index("--name") + 1] == "auto-a"
    assert seen["cmd"][seen["cmd"].index("--url") + 1] == "https://o.github.io/r/"
    assert seen["kw"].get("shell") is not True and seen["kw"]["env"]["AUTOMAGIX_HOME"] == str(paths.root)
    if os.name == "nt":
        assert seen["kw"]["creationflags"] & 0x08000000        # CREATE_NO_WINDOW


def test_start_for_run_is_silent_when_disabled_or_broken(paths):
    calls = []
    boom = lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError("no"))   # noqa: E731
    assert tray.start_for_run(paths, "auto-a", {}, popen=boom, env={"AUTOMAGIX_NO_TRAY": "1"}, platform="win32") is None
    assert tray.start_for_run(paths, "auto-a", {"tray": False}, popen=boom, env=WIN_ENV, platform="win32") is None
    assert tray.start_for_run(paths, "auto-a", {}, popen=boom, env=WIN_ENV, platform="linux") is None
    assert calls == []
    assert tray.start_for_run(paths, "auto-a", {}, popen=boom, env=WIN_ENV, platform="win32") is None     # Popen raised: swallowed
    assert tray.start_for_run(paths, "../evil", {}, popen=boom, env=WIN_ENV, platform="win32") is None     # invalid name refused


def test_stop_writes_the_stop_file_and_does_not_kill_a_helper_that_exits(paths):
    stop_file = paths.state / "tray" / "x.stop"
    proc = FakeProc(exits_after_polls=2)
    tray.stop(tray.TrayHandle(proc, stop_file))
    assert proc.terminated is False and not stop_file.exists()


def test_stop_terminates_a_stuck_helper_after_the_grace_period(paths):
    proc = FakeProc()
    tray.stop(tray.TrayHandle(proc, paths.state / "tray" / "y.stop"), grace_s=0.1)
    assert proc.terminated is True
    tray.stop(None)       # no handle: no-op


# ---- runner integration ----------------------------------------------------------------------------------------------------
def test_runner_starts_the_tray_before_the_payload_and_stops_it_after(registry, monkeypatch):
    events = []
    monkeypatch.setattr(runner_mod.tray_mod, "start_for_run", lambda paths, name, cfg, dry_run=False: events.append(("start", name, dry_run)) or "H")
    monkeypatch.setattr(runner_mod.tray_mod, "stop", lambda h: events.append(("stop", h)))
    monkeypatch.setattr(runner_mod, "_sleep", lambda s: None)
    setup_reg(registry)
    runctx.set_current(None)
    out = runner_mod.run_automation("auto-a", "daily")
    assert out.exit_code == 0
    assert events == [("start", "auto-a", False), ("stop", "H")]


def test_runner_stops_the_tray_even_when_the_run_blows_up(registry, monkeypatch):
    events = []
    monkeypatch.setattr(runner_mod.tray_mod, "start_for_run", lambda *a, **k: "H")
    monkeypatch.setattr(runner_mod.tray_mod, "stop", lambda h: events.append("stop"))
    monkeypatch.setattr(runner_mod, "_run_once", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(runner_mod, "_sleep", lambda s: None)
    setup_reg(registry)
    runctx.set_current(None)
    with pytest.raises(RuntimeError):
        runner_mod.run_automation("auto-a", "daily")
    assert events == ["stop"]


def test_a_second_concurrent_run_does_not_start_another_tray(registry, monkeypatch, paths):
    started = []
    monkeypatch.setattr(runner_mod.tray_mod, "start_for_run", lambda *a, **k: started.append(1))
    setup_reg(registry)
    lock = runner_mod.RunLock(paths.lock_file("auto-a"), 3600)
    assert lock.acquire()
    try:
        runctx.set_current(None)
        # the lock holder is this very process, so it counts as a live owner of its own lock only via a different pid file:
        paths.lock_file("auto-a").write_text(f"{os.getppid()} 0")
        runner_mod.run_automation("auto-a", "daily")
    finally:
        lock.release()
    assert started == []


# ---- the train icon --------------------------------------------------------------------------------------------------------
def test_icon_art_is_square_and_uses_known_colours():
    assert len(_trayicon.ART) == 16 and all(len(r) == 16 for r in _trayicon.ART)
    assert set("".join(_trayicon.ART)) <= set(_trayicon.PALETTE)
    px = _trayicon.train_rgba(32)
    assert len(px) == 32 and all(len(r) == 32 for r in px)
    assert px[0][0][3] == 0 and px[12][16][3] == 255           # transparent corner, opaque body
    with pytest.raises(ValueError):
        _trayicon.train_rgba(8)


@pytest.mark.parametrize("size", [16, 32, 48])
def test_dib_layout_matches_the_bitmap_header(size):
    dib = _trayicon.dib_for(size)
    hdr_size, w, h, planes, bpp = struct.unpack("<IiiHH", dib[:16])
    assert (hdr_size, w, h, planes, bpp) == (40, size, size * 2, 1, 32)
    mask_stride = ((size + 31) // 32) * 4
    assert len(dib) == 40 + size * size * 4 + mask_stride * size


def test_ico_is_valid_and_matches_the_committed_asset():
    data = _trayicon.ico_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, 3)
    for i, size in enumerate(_trayicon.SIZES):
        w, h, _c, _r, planes, bpp, length, offset = struct.unpack("<BBBBHHII", data[6 + 16 * i:22 + 16 * i])
        assert (w, h, planes, bpp) == (size, size, 1, 32)
        assert offset + length <= len(data) and data[offset:offset + 4] == struct.pack("<I", 40)
    asset = Path(tray.__file__).parent / "assets" / "train.ico"
    assert asset.read_bytes() == data, "regenerate with: python -m wrapper_core._trayicon --write wrapper_core/assets/train.ico"
    assert _trayicon.best_size(20) == 32 and _trayicon.best_size(16) == 16 and _trayicon.best_size(99) == 48
    assert _trayicon.png_bytes(16, 2).startswith(b"\x89PNG\r\n\x1a\n")


# ---- the real window (Windows with a desktop only) ------------------------------------------------------------------------
def _has_desktop() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.GetShellWindow())
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _has_desktop() or os.environ.get("AUTOMAGIX_TEST_TRAY_WINDOW") != "1",
                    reason="set AUTOMAGIX_TEST_TRAY_WINDOW=1 on an interactive Windows desktop to add a real icon for ~3 s")
def test_real_icon_is_registered_and_the_helper_exits_on_the_stop_file(tmp_path):
    stop_file = tmp_path / "t.stop"
    env = dict(os.environ, PYTHONPATH=str(Path(tray.__file__).resolve().parent.parent))
    proc = subprocess.Popen([sys.executable, "-m", "wrapper_core", "tray", "--name", "pytest", "--pid", str(os.getpid()), "--timeout-s", "30",
                             "--stop-file", str(stop_file), "--runs-dir", str(tmp_path), "--verify"], env=env, stdout=subprocess.PIPE, text=True)
    try:
        info = json.loads(proc.stdout.readline())
        assert info["added"] is True and info["registered"] is True
        stop_file.write_text("stop")
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
