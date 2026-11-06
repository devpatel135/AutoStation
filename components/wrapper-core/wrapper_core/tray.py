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

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from .paths import Paths, check_name, get_logger, platform_config

MAX_TIP = 127                      # NOTIFYICONDATAW.szTip is 128 UTF-16 units including the terminator
MAX_NAME = 60
MAX_LIFETIME_S = 4 * 3600 + 900    # a little over the runner's lock TTL: a leaked helper can never outlive a stuck run for long
MAX_BALLOON_TITLE = 63             # NOTIFYICONDATAW.szInfoTitle is 64 UTF-16 units including the terminator
MAX_BALLOON_BODY = 255             # NOTIFYICONDATAW.szInfo is 256 UTF-16 units including the terminator

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


def format_elapsed_ago(ended_at: str) -> str:
    """Human-readable elapsed since an ISO 8601 UTC timestamp (e.g. '3m ago', '1h 2m ago')."""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        elapsed = int((datetime.now(timezone.utc) - dt).total_seconds())
        if elapsed < 0:
            return ""
        if elapsed < 60:
            return f"{elapsed}s ago"
        m = elapsed // 60
        if m < 60:
            return f"{m}m ago"
        h, rm = divmod(m, 60)
        return f"{h}h {rm}m ago" if rm else f"{h}h ago"
    except Exception:  # noqa: BLE001
        return ""


def format_last_run_label(last_run: Mapping[str, Any]) -> Optional[str]:
    """Single-line tray menu label for a last-run record, e.g. 'Last: success  ·  3m ago'."""
    raw_status = str(last_run.get("status") or "").strip()
    if not raw_status:
        return None
    status = sanitize_name(raw_status, limit=20)
    ago = format_elapsed_ago(str(last_run.get("ended_at") or ""))
    return f"Last: {status}  ·  {ago}" if ago else f"Last: {status}"


def format_balloon(name: Any, record: Optional[Mapping[str, Any]]) -> Optional[Tuple[str, str]]:
    """Return (title, body) for a completion balloon notification, or None when there is nothing worth showing.

    Called by the runner after a run ends and before ``stop()`` is called; the result is stored on the
    ``TrayHandle`` so the helper subprocess can read it from the stop file and display it.
    """
    if not record:
        return None
    raw = str(record.get("synopsis") or "").strip()
    if not raw:
        return None
    synopsis = sanitize_name(raw, limit=MAX_BALLOON_BODY)
    status = str(record.get("status") or "")
    items = record.get("items") or {}
    total = int(items.get("total") or 0) if isinstance(items, dict) else 0
    name_s = sanitize_name(name)
    if status in ("failed", "hung"):
        title = f"AUTOSTATION — {name_s} failed"
    elif status == "partial":
        title = f"AUTOSTATION — {name_s} partial"
    else:
        title = f"AUTOSTATION — {name_s} done"
    body = f"{synopsis}  •  {total:,} processed" if total else synopsis
    return title[:MAX_BALLOON_TITLE], body[:MAX_BALLOON_BODY]


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
                   pid_start: Optional[int] = None, last_run_file: Optional[Path] = None) -> List[str]:
    cmd = [_helper_python(), "-m", "wrapper_core", "tray", "--name", sanitize_name(name), "--pid", str(int(pid)),
           "--timeout-s", str(int(timeout_s)), "--stop-file", str(stop_file), "--runs-dir", str(runs_dir)]
    if pid_start:
        cmd += ["--pid-start", str(int(pid_start))]
    if url:
        cmd += ["--url", url]
    if last_run_file:
        cmd += ["--last-run-file", str(last_run_file)]
    return cmd


class TrayHandle:
    def __init__(self, proc: Any, stop_file: Path):
        self.proc, self.stop_file = proc, stop_file
        self.balloon: Optional[Tuple[str, str]] = None  # set by caller before stop() to show a completion notification


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
        cmd = helper_command(name, os.getpid(), stop_file, paths.runs, site_url(cfg),
                             pid_start=process_start_time(os.getpid()), last_run_file=paths.last_run_file(name))
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
    """Ask the helper to remove its icon and exit; terminate only if it does not (that can leave a ghost icon until hover).

    If ``handle.balloon`` is set the stop file carries a JSON payload so the helper can show a completion
    balloon before removing the icon.  The file content is either the plain string ``"stop"`` or a JSON
    object ``{"title": "...", "body": "..."}``.
    """
    if handle is None:
        return
    try:
        handle.stop_file.parent.mkdir(parents=True, exist_ok=True)
        if handle.balloon is not None:
            title, body = handle.balloon
            content = json.dumps({"title": str(title)[:MAX_BALLOON_TITLE], "body": str(body)[:MAX_BALLOON_BODY]})
            handle.stop_file.write_text(content, encoding="utf-8")
        else:
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
    ap.add_argument("--last-run-file", default=None, help="path to last-run summary JSON for the context menu")
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
    last_run_file = Path(a.last_run_file) if a.last_run_file else None
    return _traywin.run(sanitize_name(a.name if not a.demo else "demo"), pid, Path(a.stop_file) if a.stop_file else None,
                        timeout, url, runs_dir, verify=a.verify, pid_start=a.pid_start, last_run_file=last_run_file)


if __name__ == "__main__":
    sys.exit(main())
