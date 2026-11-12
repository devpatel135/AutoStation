"""Task Scheduler registration + startup-folder maintenance (two-layer persistence, SPEC §2).

* One Task Scheduler task per schedule *slot*: ``\\AUTOMAGIX\\<automation>-<slot>`` (per-user, least privilege, no admin).
* ``bin\\run-<automation>.vbs`` -> ``bin\\run-<automation>.cmd`` -> ``python -m wrapper_core run``. The VBS shim avoids a
  console flash; every launcher first calls ``bin\\apply-pending.cmd``, which swaps in a staged wrapper-core *before* Python starts,
  verifies it with ``wrapper_core selftest`` and rolls back to the previous copy when it fails.
* A Startup-folder script runs ``wrapper_core maintain`` at every login and re-asserts every task.

Stability rules (a task must never be re-created, and therefore never "caught up", just because a day passed):
* the digest that decides "unchanged" is computed from a date-independent form of the task XML (``stable=True``);
* the real XML's start boundary is the NEXT occurrence, never a time in the past;
* a slot that cannot be registered right now (unresolvable time zone, ...) keeps its existing task; only slots that were removed
  from the manifest are pruned.

All command execution goes through an injectable ``runner`` so the XML/script generation is testable off-Windows.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

from . import manifest as mf, schedule as _schedule
from .paths import Paths, check_name, get_logger
from .util import atomic_write_text, read_json, user_hash, write_json

IS_WINDOWS = sys.platform == "win32"
TASK_FOLDER = "AUTOMAGIX"
HEARTBEAT_TASK = f"{TASK_FOLDER}\\heartbeat"
HEARTBEAT_INTERVAL_H = 4  # fire every 4 hours independent of any automation run
RANDOM_DELAY = "PT2M"                      # bounded jitter: a fleet-wide 09:00 must not hit the registry and the log repo in the same second
STABLE_DAY = "2000-01-01"                  # the date used wherever a date-independent form of the XML is needed
_DAY_TAG = {"Monday": "Monday", "Tuesday": "Tuesday", "Wednesday": "Wednesday", "Thursday": "Thursday",
            "Friday": "Friday", "Saturday": "Saturday", "Sunday": "Sunday"}


def _cmd_safe(value: str, what: str) -> str:
    """Values interpolated into the generated .cmd/.vbs must not break out of their quotes or trigger %var% expansion."""
    if any(c in value for c in '"%\r\n\0'):
        raise ValueError(f"{what} contains a character that is unsafe in a launcher script: {value!r}")
    return value


def _now() -> datetime:
    """Timezone-aware local 'now' (a seam for tests that freeze the clock)."""
    return datetime.now().astimezone()


def task_name(automation: str, slot_id: str) -> str:
    return f"{TASK_FOLDER}\\{check_name(automation)}-{check_name(slot_id)}"


def _local_time(hhmm: str, tz_name: str | None, now: datetime | None = None) -> str:
    """Convert a manifest wall-clock time in the schedule's timezone to this machine's local HH:MM."""
    h, m = (int(x) for x in hhmm.split(":"))
    tz = mf._tz(tz_name, strict=True)
    if tz is None:
        return f"{h:02d}:{m:02d}"
    base = (now or _now()).astimezone(tz)
    local = base.replace(hour=h, minute=m, second=0, microsecond=0).astimezone()
    return f"{local.hour:02d}:{local.minute:02d}"


def _boundary(start_day: str, hhmm: str, now_local: datetime | None) -> str:
    """StartBoundary of a calendar trigger. ``now_local`` given: the NEXT occurrence of hh:mm (never in the past, so registering a task
    cannot make Windows 'catch up' a run that was never missed). None: the fixed ``start_day`` (the stable, date-independent form)."""
    day = date.fromisoformat(start_day)
    if now_local is not None:
        h, m = (int(x) for x in hhmm.split(":"))
        if datetime.combine(day, dtime(h, m)) <= now_local:
            day += timedelta(days=1)
    return f"{day.isoformat()}T{hhmm}:00"


def slot_trigger_xml(slot: dict, tz_name: str | None, now: datetime | None = None, stable: bool = False) -> str:
    now = now or _now()
    now_local = None if stable else now.astimezone().replace(tzinfo=None)
    start_day = STABLE_DAY if stable else now.astimezone().strftime("%Y-%m-%d")
    stype = slot["type"]
    if stype in _schedule.NEW_TYPES:
        xml = _schedule.trigger_xml(slot, tz_name, start_day)

        def _bound(m: "re.Match") -> str:
            return f"<StartBoundary>{_boundary(m.group(1), m.group(2), now_local)}</StartBoundary>"
        xml = re.sub(r"<StartBoundary>(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}):00</StartBoundary>", _bound, xml)
        return re.sub(r"(<CalendarTrigger>.*?<Enabled>true</Enabled>)(<Schedule)", rf"\1<RandomDelay>{RANDOM_DELAY}</RandomDelay>\2", xml)
    if stype == "daily":
        t = _local_time(str(slot["time"]), tz_name, now)
        return (f"<CalendarTrigger><StartBoundary>{_boundary(start_day, t, now_local)}</StartBoundary><Enabled>true</Enabled>"
                f"<RandomDelay>{RANDOM_DELAY}</RandomDelay><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>")
    if stype == "weekly":
        t = _local_time(str(slot["time"]), tz_name, now)
        days = "".join(f"<{_DAY_TAG[str(d).capitalize()]} />" for d in (slot.get("days") or ["Monday"]))
        return (f"<CalendarTrigger><StartBoundary>{_boundary(start_day, t, now_local)}</StartBoundary><Enabled>true</Enabled>"
                f"<RandomDelay>{RANDOM_DELAY}</RandomDelay><ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek>{days}</DaysOfWeek></ScheduleByWeek></CalendarTrigger>")
    if stype == "hourly":
        n = int(slot.get("every", 1))
        return (f"<TimeTrigger><StartBoundary>{start_day}T00:00:00</StartBoundary><Enabled>true</Enabled>"
                f"<Repetition><Interval>PT{n}H</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger>")
    if stype == "poll":
        n = int(slot.get("every_minutes", 5))
        return (f"<TimeTrigger><StartBoundary>{start_day}T00:00:00</StartBoundary><Enabled>true</Enabled>"
                f"<Repetition><Interval>PT{n}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger>")
    raise ValueError(f"unknown slot type {stype!r}")


def _current_user() -> str:
    """DOMAIN\\user of the account that owns the tasks (explicit in the principal, so another user's same-named task can never be adopted)."""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    dom = os.environ.get("USERDOMAIN") or ""
    if not user or any(c in user + dom for c in '<>&"\r\n\0'):
        return ""
    return f"{dom}\\{user}" if dom else user


def task_xml(paths: Paths, automation: str, slot: dict, tz_name: str | None, now: datetime | None = None, stable: bool = False) -> str:
    check_name(automation)
    check_name(str(slot["id"]))  # the slot id travels through wscript -> cmd.exe: only [A-Za-z0-9._-] may get there
    vbs = paths.bin / f"run-{automation}.vbs"
    who = _current_user()
    user_id = f"<UserId>{escape(who)}</UserId>" if who else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>AUTOMAGIX {escape(automation)} / {escape(str(slot['id']))} (managed by wrapper-core; do not edit)</Description></RegistrationInfo>
  <Triggers>{slot_trigger_xml(slot, tz_name, now, stable)}</Triggers>
  <Principals><Principal id="Author">{user_id}<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT4H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
  </Settings>
  <Actions Context="Author"><Exec><Command>wscript.exe</Command><Arguments>//B //Nologo "{escape(str(vbs))}" {escape(str(slot['id']))}</Arguments></Exec></Actions>
</Task>
"""


def apply_pending_cmd(paths: Paths, python_exe: str | None = None) -> str:
    """``bin\\apply-pending.cmd``: swap in a staged wrapper-core safely. Called by EVERY launcher (run, maintain, heartbeat) before Python starts.

    * a half-finished earlier swap is repaired first (``wrapper-core`` missing but ``wrapper-core.old`` present -> restore it);
    * nothing is swapped while any run holds a lock in ``state\\locks`` (its Python has wrapper-core loaded); the next launcher retries;
    * the current copy is kept as ``wrapper-core.old`` until the new copy has passed ``wrapper_core selftest``; a rename that fails (files in use,
      antivirus scan) is retried, then rolled back;
    * a copy that fails its self-test is moved to ``wrapper-core.bad``, the previous copy is restored and the rejected version is recorded in
      ``state\\rejected-wrapper-core.txt`` so self-heal does not stage the same broken release again.
    """
    py = _cmd_safe(python_exe or sys.executable, "python path")
    root = _cmd_safe(str(paths.root), "AUTOMAGIX_HOME")
    return (
        "@echo off\r\n"
        "rem AUTOMAGIX: apply a staged wrapper-core update safely (managed by wrapper-core; do not edit)\r\n"
        "setlocal\r\n"
        f'set "AMX_ROOT={root}"\r\n'
        f'set "AMX_PY={py}"\r\n'
        "rem repair a swap that was interrupted between its two renames\r\n"
        'if not exist "%AMX_ROOT%\\wrapper-core" if exist "%AMX_ROOT%\\wrapper-core.old" ren "%AMX_ROOT%\\wrapper-core.old" wrapper-core\r\n'
        'if not exist "%AMX_ROOT%\\wrapper-core.pending" exit /b 0\r\n'
        "rem never swap while a run is active: its Python has wrapper-core loaded\r\n"
        'dir /b "%AMX_ROOT%\\state\\locks\\*.lock" >nul 2>nul && exit /b 0\r\n'
        "rem keep the current copy (as .old) until the new one passes its self-test\r\n"
        'if exist "%AMX_ROOT%\\wrapper-core.old" rmdir /s /q "%AMX_ROOT%\\wrapper-core.old"\r\n'
        'if exist "%AMX_ROOT%\\wrapper-core" ren "%AMX_ROOT%\\wrapper-core" wrapper-core.old\r\n'
        'if exist "%AMX_ROOT%\\wrapper-core" exit /b 0\r\n'
        "set TRIES=0\r\n"
        ":swap\r\n"
        'ren "%AMX_ROOT%\\wrapper-core.pending" wrapper-core 2>nul\r\n'
        'if exist "%AMX_ROOT%\\wrapper-core" goto verify\r\n'
        "set /a TRIES+=1\r\n"
        "if %TRIES% GEQ 4 goto restore\r\n"
        "ping -n 3 127.0.0.1 >nul\r\n"
        "goto swap\r\n"
        ":verify\r\n"
        'set "PYTHONPATH=%AMX_ROOT%\\wrapper-core"\r\n'
        'set "AUTOMAGIX_HOME=%AMX_ROOT%"\r\n'
        'call "%AMX_PY%" -m wrapper_core selftest >nul 2>nul\r\n'
        "if errorlevel 1 goto bad\r\n"
        "exit /b 0\r\n"
        ":bad\r\n"
        'if exist "%AMX_ROOT%\\wrapper-core.bad" rmdir /s /q "%AMX_ROOT%\\wrapper-core.bad"\r\n'
        'ren "%AMX_ROOT%\\wrapper-core" wrapper-core.bad\r\n'
        'if exist "%AMX_ROOT%\\wrapper-core.bad\\VERSION" copy /y "%AMX_ROOT%\\wrapper-core.bad\\VERSION" "%AMX_ROOT%\\state\\rejected-wrapper-core.txt" >nul\r\n'
        'ren "%AMX_ROOT%\\wrapper-core.old" wrapper-core\r\n'
        '>>"%AMX_ROOT%\\logs\\wrapper.log" echo %date% %time% apply-pending: the staged wrapper-core failed its self-test and was rolled back (kept in wrapper-core.bad)\r\n'
        "exit /b 0\r\n"
        ":restore\r\n"
        'if exist "%AMX_ROOT%\\wrapper-core.old" ren "%AMX_ROOT%\\wrapper-core.old" wrapper-core\r\n'
        'if exist "%AMX_ROOT%\\wrapper-core.pending" >>"%AMX_ROOT%\\logs\\wrapper.log" echo %date% %time% apply-pending: could not swap the staged wrapper-core in (files in use); kept the current copy\r\n'
        "exit /b 0\r\n")


def _call_apply_pending(root: str) -> str:
    f = f"{root}\\bin\\apply-pending.cmd"
    return f'if exist "{f}" call "{f}"\r\n'


def launcher_cmd(paths: Paths, automation: str, python_exe: str | None = None) -> str:
    py = _cmd_safe(python_exe or sys.executable, "python path")
    root = _cmd_safe(str(paths.root), "AUTOMAGIX_HOME")
    check_name(automation)
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        f'set "AUTOMAGIX_HOME={root}"\r\n'
        "rem apply a staged wrapper-core update (self-tested, rolled back on failure) before Python imports it\r\n"
        + _call_apply_pending(root) +
        f'set "PYTHONPATH={root}\\wrapper-core"\r\n'
        f'"{py}" -m wrapper_core run "{automation}" --slot "%~1"\r\n'
        "exit /b %ERRORLEVEL%\r\n")


def launcher_vbs(paths: Paths, automation: str) -> str:
    cmd = Path(_cmd_safe(str(paths.bin / f"run-{check_name(automation)}.cmd"), "launcher path"))
    return ('Set sh = CreateObject("WScript.Shell")\r\n'
            f'rc = sh.Run("cmd.exe /c """"{cmd}"""" " & WScript.Arguments(0), 0, True)\r\n'
            "WScript.Quit rc\r\n")


def maintain_cmd(paths: Paths, python_exe: str | None = None) -> str:
    py = _cmd_safe(python_exe or sys.executable, "python path")
    root = _cmd_safe(str(paths.root), "AUTOMAGIX_HOME")
    return ("@echo off\r\n"
            f'set "AUTOMAGIX_HOME={root}"\r\n'
            + _call_apply_pending(root) +
            f'set "PYTHONPATH={root}\\wrapper-core"\r\n'
            f'start "" /b "{py}" -m wrapper_core maintain\r\n')


def heartbeat_cmd(paths: Paths, python_exe: str | None = None) -> str:
    py = _cmd_safe(python_exe or sys.executable, "python path")
    root = _cmd_safe(str(paths.root), "AUTOMAGIX_HOME")
    return ("@echo off\r\n"
            f'set "AUTOMAGIX_HOME={root}"\r\n'
            + _call_apply_pending(root) +
            f'set "PYTHONPATH={root}\\wrapper-core"\r\n'
            f'"{py}" -m wrapper_core heartbeat\r\n'
            "exit /b %ERRORLEVEL%\r\n")


def heartbeat_task_xml(paths: Paths, python_exe: str | None = None) -> str:
    cmd = paths.bin / "heartbeat.cmd"
    return (f'<?xml version="1.0" encoding="UTF-16"?>\n'
            f'<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            f'  <RegistrationInfo><Description>AUTOMAGIX machine heartbeat — fires every {HEARTBEAT_INTERVAL_H}h'
            f' independent of automation runs (managed by wrapper-core; do not edit)</Description></RegistrationInfo>\n'
            f'  <Triggers>\n'
            f'    <TimeTrigger>\n'
            f'      <Repetition><Interval>PT{HEARTBEAT_INTERVAL_H}H</Interval>'
            f'<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>\n'
            f'      <StartBoundary>2000-01-01T00:00:00</StartBoundary><Enabled>true</Enabled>\n'
            f'    </TimeTrigger>\n'
            f'  </Triggers>\n'
            f'  <Principals><Principal id="Author">'
            f'<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>'
            f'</Principal></Principals>\n'
            f'  <Settings>\n'
            f'    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
            f'    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
            f'    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
            f'    <StartWhenAvailable>true</StartWhenAvailable>\n'
            f'    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>\n'
            f'    <Enabled>true</Enabled><Hidden>false</Hidden>\n'
            f'  </Settings>\n'
            f'  <Actions Context="Author"><Exec>'
            f'<Command>cmd.exe</Command>'
            f'<Arguments>/c &quot;{escape(str(cmd))}&quot;</Arguments>'
            f'</Exec></Actions>\n'
            f'</Task>\n')


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
    def write_apply_pending(self) -> None:
        self.paths.bin.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.paths.bin / "apply-pending.cmd", apply_pending_cmd(self.paths, self.python_exe).replace("\r\n", "\n").replace("\n", "\r\n"))

    def write_launchers(self, automation: str) -> None:
        self.paths.bin.mkdir(parents=True, exist_ok=True)
        self.write_apply_pending()
        atomic_write_text(self.paths.bin / f"run-{automation}.cmd", launcher_cmd(self.paths, automation, self.python_exe).replace("\r\n", "\n").replace("\n", "\r\n"))
        atomic_write_text(self.paths.bin / f"run-{automation}.vbs", launcher_vbs(self.paths, automation))

    def write_startup_script(self) -> Path:
        d = startup_dir()
        d.mkdir(parents=True, exist_ok=True)
        self.write_apply_pending()
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

    @staticmethod
    def _owned(st: dict, automation: str) -> list:
        """Task names that belong to exactly this automation (``a`` must not match ``a-b``'s tasks)."""
        owners = st.get("_owners") or {}
        prefix = f"{TASK_FOLDER}\\{automation}-"
        return [n for n in st if n not in ("_owners", "_alias") and (owners.get(n) == automation or (n not in owners and n.startswith(prefix)))]

    def _alias_for(self, st: dict, name: str) -> str:
        """The name a task is actually registered under: normally ``name``; ``name-<user>`` once another user on this machine owns ``name``."""
        return (st.get("_alias") or {}).get(name, name)

    def register(self, automation: str, schedule: dict | None, force: bool = False, now: datetime | None = None) -> list[str]:
        """Create/refresh one task per slot. Returns the task names touched. Idempotent: the 'unchanged' digest is computed from the
        date-independent form of the XML, so re-registering at every login is a no-op until the schedule really changes.
        A slot that cannot be expressed right now (unsafe id, unresolvable time zone, ...) is skipped AND its existing task is kept: only
        slots removed from the manifest are pruned."""
        check_name(automation)
        slots = (schedule or {}).get("slots") or []
        tz_name = (schedule or {}).get("timezone")
        self.write_launchers(automation)
        st = self._state()
        owners = st.setdefault("_owners", {})
        aliases = st.setdefault("_alias", {})
        touched: list[str] = []
        wanted: set[str] = set()
        kept: set[str] = set()      # slots we could not (re)build this time: their current task stays
        for slot in slots:
            try:
                logical = task_name(automation, str(slot["id"]))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                self.log.error("skipping slot of %s: %s", automation, exc)
                continue
            name = self._alias_for(st, logical)
            try:
                xml = task_xml(self.paths, automation, slot, tz_name, now)
                digest = hashlib.sha256(task_xml(self.paths, automation, slot, tz_name, now, stable=True).encode("utf-8")).hexdigest()
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                self.log.error("cannot build the task for slot %s of %s (kept as it is): %s", slot.get("id"), automation, exc)
                kept.add(name)
                continue
            if owners.get(name) not in (None, automation):  # ``a`` + slot ``b-daily`` and ``a-b`` + ``daily`` share a name
                self.log.error("task %s already belongs to automation %s: slot %s of %s skipped", name, owners[name], slot.get("id"), automation)
                continue
            wanted.add(name)
            if not force and st.get(name) == digest and self.exists(name):
                owners[name] = automation
                continue
            actual = self._create_or_alias(logical, name, xml, st)
            if actual:
                st[actual] = digest
                owners[actual] = automation
                wanted.add(actual)
                touched.append(actual)
        for name in [n for n in self._owned(st, automation) if n not in wanted and n not in kept]:
            self._delete(name)
            st.pop(name, None)
            owners.pop(name, None)
            touched.append(f"-{name}")
        self._save_state(st)
        return touched

    def _create_or_alias(self, logical: str, name: str, xml: str, st: dict) -> str | None:
        """Create the task; when that fails because the name belongs to ANOTHER user of this machine (the create fails and the task exists),
        register under ``<name>-<user>`` instead (recorded in state) rather than silently never scheduling this user's runs."""
        if self._create(name, xml):
            return name
        if name == logical and self.exists(name):
            alt = f"{logical}-{user_hash()[:8]}"
            if self._create(alt, xml):
                st.setdefault("_alias", {})[logical] = alt
                self.log.warning("task %s belongs to another user of this machine: registered %s instead", logical, alt)
                return alt
        return None

    def unregister(self, automation: str) -> list[str]:
        check_name(automation)
        st = self._state()
        gone = []
        for name in self._owned(st, automation):
            self._delete(name)
            st.pop(name, None)
            (st.get("_owners") or {}).pop(name, None)
            gone.append(name)
        for logical, alt in list((st.get("_alias") or {}).items()):
            if alt in gone:
                st["_alias"].pop(logical, None)
        self._save_state(st)
        for ext in ("cmd", "vbs"):
            f = self.paths.bin / f"run-{automation}.{ext}"
            if f.exists():
                f.unlink()
        return gone

    def register_heartbeat(self) -> bool:
        """Ensure the every-4-hour heartbeat task exists. Returns True if it was created or updated."""
        self.write_apply_pending()
        atomic_write_text(self.paths.bin / "heartbeat.cmd", heartbeat_cmd(self.paths, self.python_exe))
        xml = heartbeat_task_xml(self.paths, self.python_exe)
        digest = hashlib.sha256(xml.encode("utf-8")).hexdigest()
        st = self._state()
        if st.get(HEARTBEAT_TASK) == digest and self.exists(HEARTBEAT_TASK):
            return False
        if self._create(HEARTBEAT_TASK, xml):
            st[HEARTBEAT_TASK] = digest
            self._save_state(st)
            return True
        return False

    def unregister_heartbeat(self) -> None:
        """Remove the heartbeat task (called when the last automation is uninstalled)."""
        self._delete(HEARTBEAT_TASK)
        st = self._state()
        st.pop(HEARTBEAT_TASK, None)
        self._save_state(st)
        for leftover in ("heartbeat.cmd", "apply-pending.cmd"):
            f = self.paths.bin / leftover
            if f.exists():
                f.unlink()

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
