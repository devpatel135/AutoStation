"""System-tray "an automation is running" indicator (the AUTOSTATION train).

While any automation runs, a small train icon sits in the Windows notification area (bottom right of the taskbar, behind the
up-caret until the user drags it out) with the tooltip ``AUTOSTATION - <automation> running - mm:ss``; it disappears when the
run ends. It is a separate tiny process so it can never slow down or fail a run:

    python -m wrapper_core tray --name <automation> --pid <runner pid> [--timeout-s N] [--stop-file F]
    python -m wrapper_core tray --demo 8 [--verify]          # preview the icon for 8 seconds

The runner starts it right after the run lock is acquired (``start_for_run``) and stops it in a ``finally`` (``stop``): it
touches a stop file so the helper removes its icon itself (killing the process would leave a ghost icon until the mouse
passes over it). The helper also exits when the runner PID is gone, when the lifetime cap passes, on logoff/shutdown, and
when the desktop is unavailable (service session) - silently.

Switches (any one turns it off): env ``AUTOMAGIX_NO_TRAY=1`` or ``AUTOMAGIX_NO_TOAST=1`` (CI / tests), ``tray: false`` in
config/platform.json (whole machine), ``tray: false`` in automation.yaml (one automation), non-Windows, dry runs, pytest.
Everything here is stdlib only; the pure helpers work on any OS, the window code (``_traywin``) is Windows-only.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse

from .paths import Paths, check_name, get_logger, platform_config

MAX_TIP = 127                      # NOTIFYICONDATAW.szTip is 128 UTF-16 units including the terminator
MAX_NAME = 60
MAX_LIFETIME_S = 4 * 3600 + 900    # a little over the runner's lock TTL: a leaked helper can never outlive a stuck run for long

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ​-‏‪-‮⁦-⁩]")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


# ---------------------------------------------------------------------------------------------- pure helpers
def sanitize_name(name: Any, limit: int = MAX_NAME) -> str:
    """Printable, single-line, bounded text for a tooltip/menu label (never trusted: automation names come from disk)."""
    text = _CONTROL.sub(" ", str(name if name is not None else "")).strip()
    text = " ".join(text.split())
    return text[:limit] if text else "automation"


def format_elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def tooltip(name: Any, elapsed_s: float) -> str:
    return f"AUTOSTATION - {sanitize_name(name)} running - {format_elapsed(elapsed_s)}"[:MAX_TIP]


def _kernel32():
    """A PRIVATE kernel32 handle: ctypes.windll is shared process-wide, so setting argtypes/restype on it would leak into other code, and
    GetLastError is only reliable through use_last_error=True."""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    return k32


def process_start_time(pid: int) -> Optional[int]:
    """Creation time of a process (100 ns FILETIME ticks) on Windows, else None. Used to tell a reused PID from the original process."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        k32 = _kernel32()
        handle = k32.OpenProcess(0x1000, 0, int(pid))
        if not handle:
            return None
        try:
            c, e, k, u = (ctypes.c_ulonglong() for _ in range(4))
            if not k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return None
            return int(c.value)
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def pid_alive(pid: int, started: Optional[int] = None) -> bool:
    """Query-only liveness (os.kill(pid, 0) would TERMINATE the process on Windows - never use it). With ``started`` (the creation time
    recorded when the runner spawned the helper) a PID that now belongs to a different process counts as dead."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = _kernel32()
        handle = k32.OpenProcess(0x1000, 0, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5        # access denied => it exists
        try:
            code = ctypes.c_ulong()
            if not (k32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259):   # STILL_ACTIVE
                return False
        finally:
            k32.CloseHandle(handle)
        if started:
            now = process_start_time(pid)
            if now is not None and abs(now - int(started)) > 20_000_000:   # more than 2 s apart: not the same process
                return False
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def site_url(cfg: Mapping[str, Any]) -> Optional[str]:
    """The Pages site root derived from registry_url (``https://o.github.io/r/registry/`` -> ``https://o.github.io/r/``).
    https only, plain host, no credentials; anything else -> None (the menu item is then omitted)."""
    raw = str((cfg or {}).get("registry_url") or "").strip()
    try:
        u = urlparse(raw)
        port = u.port          # raises ValueError for an out-of-range / non-numeric port
    except ValueError:
        return None
    if u.scheme != "https" or not u.hostname or u.username or u.password or port not in (None, 443) or not _HOST.match(u.hostname):
        return None
    path = re.sub(r"/registry/?$", "/", u.path or "/")
    if not path.endswith("/"):
        path += "/"
    if re.search(r"[\x00-\x20\"'<>\\^`{|}]", path):
        return None
    return f"https://{u.hostname}{path}"


def disabled_reason(cfg: Mapping[str, Any], descriptor: Optional[Mapping[str, Any]] = None, env: Optional[Mapping[str, str]] = None,
                    platform: Optional[str] = None, dry_run: bool = False) -> Optional[str]:
    """Why the tray indicator is off (None = on). Pure; ``env``/``platform`` are injectable for tests."""
    env = os.environ if env is None else env
    plat = sys.platform if platform is None else platform
    if plat != "win32":
        return "not Windows"
    if env.get("AUTOMAGIX_NO_TRAY") == "1":
        return "AUTOMAGIX_NO_TRAY=1"
    if env.get("AUTOMAGIX_NO_TOAST") == "1":
        return "AUTOMAGIX_NO_TOAST=1 (CI / test run)"
    if env.get("PYTEST_CURRENT_TEST"):
        return "running under pytest"
    if dry_run:
        return "dry run"
    if (cfg or {}).get("tray") is False:
        return "tray: false in platform.json"
    if descriptor and descriptor.get("tray") is False:
        return "tray: false in automation.yaml"
    return None


def automation_descriptor(paths: Paths, name: str) -> Dict[str, Any]:
    """automation.yaml of an installed automation (empty dict when unreadable: the default is 'show the icon')."""
    try:
        from . import miniyaml
        d = miniyaml.load(paths.automation_dir(name) / "automation.yaml")
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _helper_python() -> str:
    exe = sys.executable
    if os.name == "nt":
        pyw = Path(exe).with_name("pythonw.exe")      # no console window at all
        if pyw.exists():
            return str(pyw)
    return exe


def helper_command(name: str, pid: int, stop_file: Path, runs_dir: Path, url: Optional[str], timeout_s: int = MAX_LIFETIME_S,
                   pid_start: Optional[int] = None) -> List[str]:
    cmd = [_helper_python(), "-m", "wrapper_core", "tray", "--name", sanitize_name(name), "--pid", str(int(pid)),
           "--timeout-s", str(int(timeout_s)), "--stop-file", str(stop_file), "--runs-dir", str(runs_dir)]
    if pid_start:
        cmd += ["--pid-start", str(int(pid_start))]
    if url:
        cmd += ["--url", url]
    return cmd


class TrayHandle:
    def __init__(self, proc: Any, stop_file: Path):
        self.proc, self.stop_file = proc, stop_file


# ---------------------------------------------------------------------------------------------- runner integration
def start_for_run(paths: Paths, name: str, cfg: Optional[Mapping[str, Any]] = None, dry_run: bool = False,
                  popen: Any = subprocess.Popen, env: Optional[Mapping[str, str]] = None,
                  platform: Optional[str] = None) -> Optional[TrayHandle]:
    """Start the tray helper for one run. NEVER raises and never waits: a missing icon must not affect a run."""
    log = get_logger(paths)
    try:
        check_name(name)
        cfg = cfg if cfg is not None else platform_config(paths)
        why = disabled_reason(cfg, automation_descriptor(paths, name), env=env, platform=platform, dry_run=dry_run)
        if why:
            log.debug("tray indicator off: %s", why)
            return None
        tray_dir = paths.state / "tray"
        tray_dir.mkdir(parents=True, exist_ok=True)
        stop_file = tray_dir / f"{name}-{os.getpid()}.stop"
        try:
            stop_file.unlink()
        except OSError:
            pass
        cmd = helper_command(name, os.getpid(), stop_file, paths.runs, site_url(cfg), pid_start=process_start_time(os.getpid()))
        child_env = dict(os.environ if env is None else env)
        child_env["AUTOMAGIX_HOME"] = str(paths.root)
        pkg_parent = str(Path(__file__).resolve().parent.parent)
        child_env["PYTHONPATH"] = pkg_parent + (os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else "")
        flags = 0
        if os.name == "nt":
            flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        proc = popen(cmd, env=child_env, creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, shell=False) if os.name == "nt" else \
            popen(cmd, env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  close_fds=True, shell=False)
        return TrayHandle(proc, stop_file)
    except Exception as exc:  # noqa: BLE001
        log.debug("tray indicator not started: %s", exc)
        return None


def stop(handle: Optional[TrayHandle], grace_s: float = 1.0) -> None:
    """Ask the helper to remove its icon and exit; terminate only if it does not (that can leave a ghost icon until hover)."""
    if handle is None:
        return
    try:
        handle.stop_file.parent.mkdir(parents=True, exist_ok=True)
        handle.stop_file.write_text("stop", encoding="ascii")
    except OSError:
        pass
    proc = handle.proc
    try:
        deadline = time.monotonic() + grace_s
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(2)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    try:
        handle.stop_file.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------- command line
def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="wrapper_core tray", description="show the AUTOSTATION train in the notification area while a run is active")
    ap.add_argument("--name", default="automation")
    ap.add_argument("--pid", type=int, default=0, help="runner PID to watch; the icon goes away when it exits")
    ap.add_argument("--timeout-s", type=int, default=MAX_LIFETIME_S)
    ap.add_argument("--pid-start", type=int, default=0, help="creation time of the runner (guards against PID reuse)")
    ap.add_argument("--stop-file", default=None)
    ap.add_argument("--runs-dir", default=None)
    ap.add_argument("--url", default=None)
    ap.add_argument("--demo", type=int, default=0, metavar="SECONDS", help="preview the icon for N seconds")
    ap.add_argument("--verify", action="store_true", help="after adding the icon print whether Windows registered it (JSON)")
    a = ap.parse_args(argv)
    if sys.platform != "win32":
        print("the tray indicator is Windows-only", file=sys.stderr)
        return 0
    paths = Paths()
    timeout = max(1, min(a.demo or a.timeout_s, MAX_LIFETIME_S))
    pid = os.getpid() if a.demo else a.pid
    url = site_url({"registry_url": a.url}) if a.url else None
    from . import _traywin
    runs_dir = Path(a.runs_dir) if a.runs_dir else paths.runs
    return _traywin.run(sanitize_name(a.name if not a.demo else "demo"), pid, Path(a.stop_file) if a.stop_file else None,
                        timeout, url, runs_dir, verify=a.verify, pid_start=a.pid_start)


if __name__ == "__main__":
    sys.exit(main())
