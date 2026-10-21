"""Task Scheduler registration + startup-folder maintenance (two-layer persistence, SPEC §2).

* One Task Scheduler task per schedule *slot*: ``\\AUTOMAGIX\\<automation>-<slot>`` (per-user, least privilege, no admin).
* ``bin\\run-<automation>.vbs`` -> ``bin\\run-<automation>.cmd`` -> ``python -m wrapper_core run``. The VBS shim avoids a
  console flash; the CMD applies a staged wrapper-core update *before* Python starts.
* A Startup-folder script runs ``wrapper_core maintain`` at every login and re-asserts every task.

All command execution goes through an injectable ``runner`` so the XML/script generation is testable off-Windows.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

from . import manifest as mf
from .paths import Paths, get_logger
from .util import atomic_write_text, read_json, write_json

IS_WINDOWS = sys.platform == "win32"
TASK_FOLDER = "AUTOMAGIX"
_DAY_TAG = {"Monday": "Monday", "Tuesday": "Tuesday", "Wednesday": "Wednesday", "Thursday": "Thursday",
            "Friday": "Friday", "Saturday": "Saturday", "Sunday": "Sunday"}


def task_name(automation: str, slot_id: str) -> str:
    return f"{TASK_FOLDER}\\{automation}-{slot_id}"


def _local_time(hhmm: str, tz_name: str | None) -> str:
    """Convert a manifest wall-clock time in the schedule's timezone to this machine's local HH:MM."""
    h, m = (int(x) for x in hhmm.split(":"))
    tz = mf._tz(tz_name)
    if tz is None:
        return f"{h:02d}:{m:02d}"
    today = datetime.now(tz).replace(hour=h, minute=m, second=0, microsecond=0)
    local = today.astimezone()
    return f"{local.hour:02d}:{local.minute:02d}"


def slot_trigger_xml(slot: dict, tz_name: str | None) -> str:
    start_day = datetime.now().strftime("%Y-%m-%d")
    stype = slot["type"]
    if stype == "daily":
        t = _local_time(str(slot["time"]), tz_name)
        return (f"<CalendarTrigger><StartBoundary>{start_day}T{t}:00</StartBoundary><Enabled>true</Enabled>"
                f"<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>")
    if stype == "weekly":
        t = _local_time(str(slot["time"]), tz_name)
        days = "".join(f"<{_DAY_TAG[str(d).capitalize()]} />" for d in (slot.get("days") or ["Monday"]))
        return (f"<CalendarTrigger><StartBoundary>{start_day}T{t}:00</StartBoundary><Enabled>true</Enabled>"
                f"<ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek>{days}</DaysOfWeek></ScheduleByWeek></CalendarTrigger>")
    if stype == "hourly":
        n = int(slot.get("every", 1))
        return (f"<TimeTrigger><StartBoundary>{start_day}T00:00:00</StartBoundary><Enabled>true</Enabled>"
                f"<Repetition><Interval>PT{n}H</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger>")
    if stype == "poll":
        n = int(slot.get("every_minutes", 5))
        return (f"<TimeTrigger><StartBoundary>{start_day}T00:00:00</StartBoundary><Enabled>true</Enabled>"
                f"<Repetition><Interval>PT{n}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger>")
    raise ValueError(f"unknown slot type {stype!r}")


def task_xml(paths: Paths, automation: str, slot: dict, tz_name: str | None) -> str:
    vbs = paths.bin / f"run-{automation}.vbs"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>AUTOMAGIX {escape(automation)} / {escape(str(slot['id']))} (managed by wrapper-core; do not edit)</Description></RegistrationInfo>
  <Triggers>{slot_trigger_xml(slot, tz_name)}</Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT4H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
  </Settings>
  <Actions Context="Author"><Exec><Command>wscript.exe</Command><Arguments>//B //Nologo "{escape(str(vbs))}" {escape(str(slot['id']))}</Arguments></Exec></Actions>
</Task>
"""


def launcher_cmd(paths: Paths, automation: str, python_exe: str | None = None) -> str:
    py = python_exe or sys.executable
    root = str(paths.root)
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        f'set "AUTOMAGIX_HOME={root}"\r\n'
        "rem apply a staged wrapper-core update before Python imports it\r\n"
        f'if exist "{root}\\wrapper-core.pending" (\r\n'
        f'  if exist "{root}\\wrapper-core.old" rmdir /s /q "{root}\\wrapper-core.old"\r\n'
        f'  if exist "{root}\\wrapper-core" ren "{root}\\wrapper-core" wrapper-core.old\r\n'
        f'  ren "{root}\\wrapper-core.pending" wrapper-core\r\n'
        f'  if exist "{root}\\wrapper-core.old" rmdir /s /q "{root}\\wrapper-core.old"\r\n'
        ")\r\n"
        f'set "PYTHONPATH={root}\\wrapper-core"\r\n'
        f'"{py}" -m wrapper_core run {automation} --slot %1\r\n'
        "exit /b %ERRORLEVEL%\r\n")


def launcher_vbs(paths: Paths, automation: str) -> str:
    cmd = paths.bin / f"run-{automation}.cmd"
    return ('Set sh = CreateObject("WScript.Shell")\r\n'
            f'rc = sh.Run("cmd.exe /c """"{cmd}"""" " & WScript.Arguments(0), 0, True)\r\n'
            "WScript.Quit rc\r\n")


def maintain_cmd(paths: Paths, python_exe: str | None = None) -> str:
    py = python_exe or sys.executable
    root = str(paths.root)
    return ("@echo off\r\n"
            f'set "AUTOMAGIX_HOME={root}"\r\n'
            f'set "PYTHONPATH={root}\\wrapper-core"\r\n'
            f'start "" /b "{py}" -m wrapper_core maintain\r\n')


def startup_dir() -> Path:
    override = os.environ.get("AUTOMAGIX_STARTUP_DIR")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" if appdata else Path.home() / "Startup"


def default_runner(args: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=60, input=input_text)


class Scheduler:
    def __init__(self, paths: Paths | None = None, runner: Callable | None = None, python_exe: str | None = None):
        self.paths = paths or Paths()
        self.runner = runner or default_runner
        self.python_exe = python_exe
        self.log = get_logger(self.paths)

    # ---- file generation (always works) -------------------------------------------------------
    def write_launchers(self, automation: str) -> None:
        self.paths.bin.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.paths.bin / f"run-{automation}.cmd", launcher_cmd(self.paths, automation, self.python_exe).replace("\r\n", "\n").replace("\n", "\r\n"))
        atomic_write_text(self.paths.bin / f"run-{automation}.vbs", launcher_vbs(self.paths, automation))

    def write_startup_script(self) -> Path:
        d = startup_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = d / "AUTOMAGIX-maintain.cmd"
        atomic_write_text(f, maintain_cmd(self.paths, self.python_exe))
        return f

    def remove_startup_script(self) -> None:
        f = startup_dir() / "AUTOMAGIX-maintain.cmd"
        if f.exists():
            f.unlink()

    # ---- task state ---------------------------------------------------------------------------
    def _state(self) -> dict:
        return read_json(self.paths.state / "tasks.json", {}) or {}

    def _save_state(self, st: dict) -> None:
        write_json(self.paths.state / "tasks.json", st)

    def register(self, automation: str, schedule: dict | None, force: bool = False) -> list[str]:
        """Create/refresh one task per slot. Returns the task names touched. Idempotent via an XML hash."""
        slots = (schedule or {}).get("slots") or []
        tz_name = (schedule or {}).get("timezone")
        self.write_launchers(automation)
        st = self._state()
        touched: list[str] = []
        wanted = set()
        for slot in slots:
            name = task_name(automation, str(slot["id"]))
            wanted.add(name)
            xml = task_xml(self.paths, automation, slot, tz_name)
            digest = hashlib.sha256(xml.encode("utf-8")).hexdigest()
            if not force and st.get(name) == digest and self.exists(name):
                continue
            if self._create(name, xml):
                st[name] = digest
                touched.append(name)
        for name in [n for n in st if n.startswith(f"{TASK_FOLDER}\\{automation}-") and n not in wanted]:
            self._delete(name)
            st.pop(name, None)
            touched.append(f"-{name}")
        self._save_state(st)
        return touched

    def unregister(self, automation: str) -> list[str]:
        st = self._state()
        gone = []
        for name in [n for n in st if n.startswith(f"{TASK_FOLDER}\\{automation}-")]:
            self._delete(name)
            st.pop(name, None)
            gone.append(name)
        self._save_state(st)
        for ext in ("cmd", "vbs"):
            f = self.paths.bin / f"run-{automation}.{ext}"
            if f.exists():
                f.unlink()
        return gone

    # ---- schtasks -----------------------------------------------------------------------------
    def exists(self, name: str) -> bool:
        if not IS_WINDOWS and self.runner is default_runner:
            return True  # off-Windows dry mode: trust state file
        try:
            return self.runner(["schtasks", "/Query", "/TN", name]).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _create(self, name: str, xml: str) -> bool:
        if not IS_WINDOWS and self.runner is default_runner:
            self.log.info("[dry] would register task %s", name)
            return True
        xml_path = self.paths.state / f"task-{name.replace(chr(92), '_')}.xml"
        xml_path.write_bytes(b"\xff\xfe" + xml.replace("\r\n", "\n").encode("utf-16-le"))
        try:
            r = self.runner(["schtasks", "/Create", "/TN", name, "/XML", str(xml_path), "/F"])
        finally:
            try:
                xml_path.unlink()
            except OSError:
                pass
        if r.returncode != 0:
            self.log.error("schtasks create %s failed: %s", name, (r.stderr or r.stdout).strip()[:300])
        return r.returncode == 0

    def _delete(self, name: str) -> None:
        if not IS_WINDOWS and self.runner is default_runner:
            return
        self.runner(["schtasks", "/Delete", "/TN", name, "/F"])
