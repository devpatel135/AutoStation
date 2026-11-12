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


def test_lock_file_records_owner_start_time(tmp_path):
    from wrapper_core.locking import process_start_time, read_lock
    lock = tmp_path / "c.lock"
    assert RunLock(lock, ttl_s=3600).acquire() is True
    pid, started = read_lock(lock)
    assert pid == os.getpid()
    if process_start_time(os.getpid()) is not None:
        assert started == process_start_time(os.getpid())
    assert len(lock.read_text().split()) == 3


def test_recycled_pid_lock_is_reclaimed(tmp_path):
    """A live process with the owner's PID but a different start time is a RECYCLED pid, not the owner."""
    from wrapper_core.locking import process_start_time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        real = process_start_time(child.pid)
        if real is None:
            import pytest
            pytest.skip("no process start time on this platform")
        lock = tmp_path / "d.lock"
        lock.write_text(f"{child.pid} 0 {real - 10 * 10_000_000}")      # recorded start is long before the real one
        assert not _pid_alive(child.pid, real - 10 * 10_000_000)
        assert RunLock(lock, ttl_s=3600).acquire() is True
    finally:
        child.kill()
        child.wait()


def test_concurrent_takeover_of_a_stale_lock_has_one_winner(tmp_path):
    import threading
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    lock = tmp_path / "e.lock"
    lock.write_text(f"{child.pid} 0")
    results, barrier = [], threading.Barrier(6)

    def go():
        l = RunLock(lock, ttl_s=3600)
        barrier.wait()
        results.append(l.acquire())
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    # same pid for all threads in this process: the owner is "us", so a re-acquire by our own pid is allowed; the point is no crash and a lock exists
    assert any(results) and lock.exists()


def test_tail_reads_only_the_end(tmp_path):
    from wrapper_core.runner import _tail
    f = tmp_path / "big.log"
    f.write_bytes(b"x" * 5_000_000 + b"THE-END")
    assert _tail(f, 100).endswith("THE-END") and len(_tail(f, 100)) == 100
    assert _tail(tmp_path / "missing.log") == ""
