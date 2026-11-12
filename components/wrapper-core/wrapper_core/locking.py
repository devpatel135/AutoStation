"""Cross-process file locks whose staleness check survives PID reuse and takeover races (used by the run lock and the self-heal lock).

A lock file holds ``<pid> <unix time> <process start time>``. It is stale when it is older than its TTL, when the process that wrote it is
gone, or when that PID now belongs to a DIFFERENT process (the start time recorded in the file no longer matches: Windows recycles PIDs
quickly, and after a crash or power loss the saved PID can be handed to an unrelated process at the next boot, which used to make a dead
lock look alive for hours). Taking over a stale lock is serialised by a short-lived ``.takeover`` mutex, so two contenders can never both
delete the other's fresh lock.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

TAKEOVER_STALE_S = 30.0          # a takeover mutex older than this belonged to a contender that died mid-takeover
START_TOLERANCE_TICKS = 20_000_000   # 2 s in 100 ns FILETIME ticks (Windows); POSIX start times are in clock ticks (see below)
_POSIX_TOLERANCE = 200            # 2 s at 100 ticks/s


def _kernel32():
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)   # private handle: the shared ctypes.windll must not get our argtypes
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    return k32


def process_start_time(pid: int) -> Optional[int]:
    """When a process started: FILETIME ticks (Windows) or clock ticks since boot (Linux /proc), else None."""
    pid = int(pid)
    if os.name == "nt":
        try:
            import ctypes
            k32 = _kernel32()
            handle = k32.OpenProcess(0x1000, 0, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
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
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        return int(raw.rsplit(")", 1)[1].split()[19])          # field 22 (starttime), counted after the "(comm)" field
    except (OSError, ValueError, IndexError):
        return None


def pid_alive(pid: int, started: Optional[int] = None) -> bool:
    """Query-only liveness (``os.kill(pid, 0)`` would TERMINATE the process on Windows - never use it). With ``started`` (the start time
    recorded when the lock was taken) a PID that now belongs to a different process counts as dead."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = _kernel32()
        handle = k32.OpenProcess(0x1000, 0, pid)
        if not handle:
            return ctypes.get_last_error() == 5            # access denied => it exists
        try:
            code = ctypes.c_ulong()
            if not (k32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259):   # STILL_ACTIVE
                return False
        finally:
            k32.CloseHandle(handle)
        tol = START_TOLERANCE_TICKS
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            pass                                           # exists but not ours (EPERM)
        tol = _POSIX_TOLERANCE
    if started:
        now = process_start_time(pid)
        if now is not None and abs(now - int(started)) > tol:
            return False                                    # same PID, different process: the recorded owner is gone
    return True


def read_lock(path: Path) -> tuple[int, Optional[int]]:
    """(owner pid or 0, owner start time or None) from a lock file; tolerant of the older ``pid time`` format and of garbage."""
    try:
        parts = path.read_text(encoding="ascii", errors="replace").split()
    except OSError:
        return 0, None
    try:
        owner = int(parts[0])
    except (ValueError, IndexError):
        owner = 0
    started: Optional[int] = None
    if len(parts) >= 3:
        try:
            started = int(parts[2])
        except ValueError:
            started = None
    return owner, started


class FileLock:
    """Exclusive lock file. ``acquire()`` never blocks; ``acquire(wait_s=N)`` polls for up to N seconds."""

    def __init__(self, path: Path, ttl_s: int):
        self.path, self.ttl_s, self.held = Path(path), ttl_s, False

    # -- staleness -----------------------------------------------------------------------------------------------------------------------
    def stale(self) -> bool:
        try:
            if time.time() - self.path.stat().st_mtime > self.ttl_s:
                return True
        except OSError:
            return False                                    # vanished (released) while we looked: not stale, just gone
        owner, started = read_lock(self.path)
        return bool(owner and owner != os.getpid() and not pid_alive(owner, started))

    def _create(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):          # PermissionError: a lock file just deleted by another process is 'delete pending' on Windows
            return False
        try:
            start = process_start_time(os.getpid())
            os.write(fd, f"{os.getpid()} {time.time()} {start if start is not None else ''}".encode())
        finally:
            os.close(fd)
        return True

    def _takeover(self) -> None:
        """Remove a stale lock under the takeover mutex (re-checking staleness inside it), so racing contenders cannot delete each other's fresh lock."""
        mutex = self.path.with_name(self.path.name + ".takeover")
        try:
            if time.time() - mutex.stat().st_mtime > TAKEOVER_STALE_S:
                mutex.unlink()
        except OSError:
            pass
        try:
            fd = os.open(mutex, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            return                                          # someone else is taking over: let them finish, caller retries
        try:
            os.close(fd)
            if self.stale():
                grave = self.path.with_name(f"{self.path.name}.stale-{os.getpid()}-{int(time.time() * 1000)}")
                try:
                    os.replace(self.path, grave)
                    grave.unlink()
                except OSError:
                    pass
        finally:
            try:
                mutex.unlink()
            except OSError:
                pass

    # -- API -----------------------------------------------------------------------------------------------------------------------------
    def acquire(self, wait_s: float = 0.0) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            if self._create():
                self.held = True
                return True
            if self.stale():
                self._takeover()
                if self._create():
                    self.held = True
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
