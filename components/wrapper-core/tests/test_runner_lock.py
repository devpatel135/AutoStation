from __future__ import annotations

import os
import subprocess
import sys

from wrapper_core.runner import RunLock, _pid_alive


def test_lock_held_by_live_process_blocks(tmp_path):
    lock = tmp_path / "a.lock"
    lock.write_text(f"{os.getpid() + 0} 0")        # our own pid is alive but equals ours -> treated as ours (re-acquire allowed)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock.write_text(f"{child.pid} 0")
        assert _pid_alive(child.pid)
        assert RunLock(lock, ttl_s=3600).acquire() is False
    finally:
        child.kill()
        child.wait()


def test_lock_of_dead_process_is_reclaimed_immediately(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    lock = tmp_path / "b.lock"
    lock.write_text(f"{child.pid} 0")
    assert not _pid_alive(child.pid)
    assert RunLock(lock, ttl_s=3600).acquire() is True
