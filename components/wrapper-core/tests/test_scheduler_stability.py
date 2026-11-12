"""Phase 1 (1.1/1.2): stable task registration + launcher swap script content. All hermetic (fake schtasks)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from wrapper_core import scheduler
from wrapper_core.paths import Paths
from wrapper_core.scheduler import Scheduler, apply_pending_cmd, heartbeat_cmd, launcher_cmd, maintain_cmd, task_xml

from test_platform import FakeSchtasks

SCHED = {"timezone": "UTC", "slots": [{"id": "daily", "type": "daily", "time": "09:00"},
                                       {"id": "wk", "type": "weekdays", "time": "08:30"}]}


@pytest.fixture(autouse=True)
def _windows(monkeypatch):
    monkeypatch.setattr("wrapper_core.scheduler.IS_WINDOWS", True)


def _sched(tmp_path=None):
    fake = FakeSchtasks()
    return Scheduler(Paths().ensure(), runner=fake), fake


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_reregister_across_midnight_is_a_noop():
    sch, fake = _sched()
    sch.register("auto-a", SCHED, now=_utc(2026, 3, 1, 23, 50))
    creates = [c for c in fake.calls if c[1] == "/Create"]
    assert len(creates) == 2
    sch.register("auto-a", SCHED, now=_utc(2026, 3, 2, 0, 10))       # date changed: nothing may be re-created (no 'missed' catch-up run)
    sch.register("auto-a", SCHED, now=_utc(2026, 3, 5, 12, 0))      # (a DST change of the machine WOULD legitimately re-register: stay clear of one)
    assert [c for c in fake.calls if c[1] == "/Create"] == creates


def test_start_boundary_is_never_in_the_past():
    now = _utc(2026, 3, 1, 10, 0)                                      # 09:00 already passed today
    xml = task_xml(Paths(), "auto-a", SCHED["slots"][0], "UTC", now=now)
    m = re.search(r"<StartBoundary>(.*?)</StartBoundary>", xml)
    assert m
    boundary = datetime.fromisoformat(m.group(1))
    assert boundary > now.astimezone().replace(tzinfo=None)
    assert "<RandomDelay>PT2M</RandomDelay>" in xml and "<RunOnlyIfNetworkAvailable>true" in xml


def test_new_types_get_future_boundary_and_random_delay():
    now = _utc(2026, 3, 1, 10, 0)
    xml = task_xml(Paths(), "auto-a", SCHED["slots"][1], "UTC", now=now)
    assert "<RandomDelay>PT2M</RandomDelay>" in xml
    b = datetime.fromisoformat(re.search(r"<StartBoundary>(.*?)</StartBoundary>", xml).group(1))
    assert b > now.astimezone().replace(tzinfo=None)


def test_failed_slot_keeps_its_existing_task():
    sch, fake = _sched()
    sch.register("auto-a", SCHED)
    assert "AUTOMAGIX\\auto-a-daily" in fake.existing
    bad = {"timezone": "Not/AZone", "slots": SCHED["slots"][:1] + [SCHED["slots"][1]]}
    sch.register("auto-a", bad)                                        # the time zone cannot be resolved now
    assert "AUTOMAGIX\\auto-a-daily" in fake.existing                  # old task stays
    assert not [c for c in fake.calls if c[1] == "/Delete"]


def test_slot_removed_from_manifest_is_pruned_but_others_stay():
    sch, fake = _sched()
    sch.register("auto-a", SCHED)
    sch.register("auto-a", {"timezone": "UTC", "slots": SCHED["slots"][:1]})
    assert fake.existing == {"AUTOMAGIX\\auto-a-daily"}


def test_foreign_owned_name_falls_back_to_user_alias():
    sch, fake = _sched()
    fake.existing.add("AUTOMAGIX\\auto-a-daily")                       # another user's task already owns the name

    orig = fake.__call__

    def runner(args, input_text=None):
        if args[1] == "/Create" and args[3] == "AUTOMAGIX\\auto-a-daily":
            fake.calls.append(args)
            r = type("R", (), {"stdout": "", "stderr": "Access is denied", "returncode": 1})()
            return r
        return orig(args, input_text)
    sch.runner = runner
    sch.register("auto-a", {"timezone": "UTC", "slots": SCHED["slots"][:1]})
    alias = [n for n in fake.existing if n.startswith("AUTOMAGIX\\auto-a-daily-")]
    assert len(alias) == 1
    before = len(fake.calls)
    sch.register("auto-a", {"timezone": "UTC", "slots": SCHED["slots"][:1]})   # idempotent via the alias
    assert not [c for c in fake.calls[before:] if c[1] == "/Create"]


def test_task_principal_has_explicit_user(monkeypatch):
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("USERDOMAIN", "CORP")
    xml = task_xml(Paths(), "auto-a", SCHED["slots"][0], "UTC")
    assert "<UserId>CORP\\alice</UserId>" in xml


def test_apply_pending_script_is_crash_safe():
    s = apply_pending_cmd(Paths(), "C:/py/python.exe")
    assert 'if not exist "%AMX_ROOT%\\wrapper-core" if exist "%AMX_ROOT%\\wrapper-core.old"' in s        # repairs an interrupted swap
    assert "state\\locks" in s and "selftest" in s                                                        # lock check + verification
    assert s.index("selftest") < s.index(":bad")
    assert "rmdir /s /q \"%AMX_ROOT%\\wrapper-core.old\"" in s
    # .old is deleted only BEFORE the current copy is renamed to it, never after the new copy is installed
    after_verify = s[s.index(":verify"):]
    assert "rmdir /s /q \"%AMX_ROOT%\\wrapper-core.old\"" not in after_verify.split(":bad")[0]
    assert "GEQ 4" in s                                                                                    # bounded retries


@pytest.mark.parametrize("builder", [launcher_cmd, maintain_cmd, heartbeat_cmd])
def test_every_launcher_calls_apply_pending_before_python(builder):
    args = (Paths(), "auto-a", "C:/py/python.exe") if builder is launcher_cmd else (Paths(), "C:/py/python.exe")
    cmd = builder(*args)
    assert "apply-pending.cmd" in cmd
    assert cmd.index("apply-pending.cmd") < cmd.index("-m wrapper_core")
