"""Remediation playbooks (see remediation.py). Each is small, bounded and reversible where that is possible.

check()    is the problem really there?          fix()      do the minimum that could help
verify()   did it help?                          rollback() undo, when verification failed
Only ``risk = safe`` playbooks run unattended; others are reported as recommendations.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import remediation as R
from .paths import Paths, check_name
from .remediation import Context, Playbook
from .util import iso, parse_iso, utcnow, write_json

BROWSER_NAMES = {"msedge.exe", "chrome.exe", "msedge", "chrome", "chromium", "chromium-browser", "google-chrome"}


# ---------------------------------------------------------------------------------------------------- process helpers
def list_browser_processes() -> List[tuple]:
    """[(pid, name, command line)] for Edge/Chrome processes. Read-only."""
    if sys.platform == "win32":
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" | "
              "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress")
        env = dict(os.environ)
        env.pop("PSModulePath", None)
        try:
            r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=12, env=env)
            text = r.stdout.decode("utf-8", errors="replace").lstrip("\ufeff")  # UTF-8 explicitly: the ANSI code page garbles non-ASCII user names
            data = json.loads(text) if text.strip() else []
        except (OSError, subprocess.SubprocessError, ValueError):
            return []
        data = [data] if isinstance(data, dict) else data
        return [(int(d.get("ProcessId") or 0), str(d.get("Name") or ""), str(d.get("CommandLine") or "")) for d in data if isinstance(d, dict)]
    out = []
    proc = Path("/proc")
    if not proc.exists():
        return out
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            name = (p / "comm").read_text(errors="replace").strip()
            cmd = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        out.append((int(p.name), name, cmd))
    return out


def kill_pid(pid: int) -> bool:
    """Terminate ONE process (no tree kill). The caller has already matched it to the automation's own profile."""
    try:
        if sys.platform == "win32":
            return subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"], capture_output=True, timeout=30).returncode == 0
        os.kill(int(pid), signal.SIGTERM)
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _norm(p: str) -> str:
    return str(p).replace("/", "\\").lower().rstrip("\\")


def profile_processes(profile: Path, lister: Optional[Callable] = None) -> List[tuple]:
    """Browser processes whose command line names exactly this profile folder (not a look-alike such as ``<profile>2``)."""
    target = _norm(str(profile))
    hits = []
    for pid, name, cmd in (lister or list_browser_processes)():
        if str(name).lower() not in BROWSER_NAMES or not cmd or int(pid) <= 0 or int(pid) == os.getpid():
            continue
        n = _norm(cmd)
        start = 0
        while True:
            i = n.find(target, start)
            if i < 0:
                break
            before = n[i - 1] if i > 0 else " "
            after = n[i + len(target)] if i + len(target) < len(n) else " "
            if not (before.isalnum() or before in "\\_-.") and after in ' "\'\\':
                hits.append((int(pid), str(name), cmd))
                break
            start = i + 1
    return hits


# ---------------------------------------------------------------------------------------------------- 1. DevTools blocked
class DevtoolsNativeMode(Playbook):
    name = "devtools_native_mode"
    categories = ("DEVTOOLS_BLOCKED",)
    retry_after_fix = True
    max_attempts = 2
    description = "switch this automation to native mode (opens your normal Edge and drives it with UI Automation; the Edge policy is left untouched)"

    def check(self, ctx: Context) -> bool:
        return bool(ctx.automation) and R.load_overrides(ctx.paths, ctx.automation).get("mode") != "native"

    def fix(self, ctx: Context):
        R.write_override(ctx.paths, ctx.automation, {"mode": "native"}, "Edge policy RemoteDebuggingAllowed=0 blocked DevTools")
        return "native-mode override written (expires in 30 days); the Edge policy was not touched"

    def verify(self, ctx: Context) -> bool:
        return R.load_overrides(ctx.paths, ctx.automation).get("mode") == "native"

    def rollback(self, ctx: Context) -> None:
        R.clear_override(ctx.paths, ctx.automation)


# ---------------------------------------------------------------------------------------------------- 2. stale run lock
class ClearStaleRunLock(Playbook):
    name = "clear_stale_run_lock"
    categories = ("STALE_LOCK",)
    description = "remove the run lock left by a process that no longer exists"

    def check(self, ctx: Context) -> bool:
        return bool(ctx.automation) and R.lock_state(ctx.paths, ctx.automation) == "stale"

    def fix(self, ctx: Context):
        f = ctx.paths.lock_file(ctx.automation)
        if not ctx.inside_root(f):
            raise RuntimeError("lock path outside the install folder")
        f.unlink()
        return "removed the stale run lock"

    def verify(self, ctx: Context) -> bool:
        return not ctx.paths.lock_file(ctx.automation).exists()


# ---------------------------------------------------------------------------------------------------- 3. browser profile lock
class ReleaseProfileLock(Playbook):
    name = "release_browser_profile_lock"
    categories = ("STALE_BROWSER_PROFILE_LOCK",)
    retry_after_fix = True
    description = "stop Edge processes that hold THIS automation's profile folder, then remove the profile lock files"

    def _procs(self, ctx: Context):
        return profile_processes(R.profile_dir(ctx.paths, ctx.automation), ctx.lister)

    def check(self, ctx: Context) -> bool:
        if not ctx.automation:
            return False
        if R.other_run_live(ctx.paths, ctx.automation):
            return False  # Chromium keeps these lock files while it runs: a live run of this automation owns the browser, never touch it
        return bool(R.profile_lock_files(ctx.paths, ctx.automation) or self._procs(ctx))

    def fix(self, ctx: Context):
        killed = 0
        kill = ctx.killer or kill_pid
        for pid, _name, _cmd in self._procs(ctx):
            if R.other_run_live(ctx.paths, ctx.automation):
                break  # a run started since the check: stop at once
            if pid not in [p[0] for p in self._procs(ctx)]:
                continue  # re-list immediately before the kill: the PID may have exited and been reused by something else
            if kill(pid):
                killed += 1
        for _ in range(10):  # give the processes a moment to exit before touching their files
            if not self._procs(ctx):
                break
            time.sleep(0.3)
        removed = 0
        for f in R.profile_lock_files(ctx.paths, ctx.automation):
            if f.name in R.PROFILE_LOCK_NAMES and ctx.inside_root_link(f):
                try:
                    f.unlink()  # unlinks a symlink itself (SingletonLock on Linux), never its target
                    removed += 1
                except OSError:
                    pass
        return f"stopped {killed} browser process(es) using this profile; removed {removed} lock file(s)"

    def verify(self, ctx: Context) -> bool:
        return not self._procs(ctx) and not R.profile_lock_files(ctx.paths, ctx.automation)


# ---------------------------------------------------------------------------------------------------- 4. corrupt browser profile
class QuarantineProfile(Playbook):
    name = "quarantine_browser_profile"
    categories = ("CORRUPT_BROWSER_PROFILE",)
    max_attempts = 1
    cooldown_s = 3600
    description = "move the corrupt browser profile to profiles.bad/ (nothing is deleted) and start a fresh one; a person must sign in again"

    def check(self, ctx: Context) -> bool:
        return bool(ctx.automation) and R.profile_dir(ctx.paths, ctx.automation).is_dir()

    def fix(self, ctx: Context):
        prof = R.profile_dir(ctx.paths, ctx.automation)
        if not ctx.inside_root(prof):
            raise RuntimeError("profile path outside the install folder")
        bad_root = ctx.paths.root / "profiles.bad"
        bad_root.mkdir(parents=True, exist_ok=True)
        dest = bad_root / f"{ctx.automation}-{ctx.now().strftime('%Y%m%dT%H%M%S')}"
        os.replace(str(prof), str(dest))
        prof.mkdir(parents=True, exist_ok=True)
        ctx.scratch["quarantined"] = dest
        mine = re.compile(r"^" + re.escape(ctx.automation) + r"-\d{8}T\d{6}$")  # exact: automation "a" must not match "a-b-<ts>"
        olds = sorted([d for d in bad_root.iterdir() if d.is_dir() and mine.match(d.name)], key=lambda d: d.name)
        for d in olds[:-3]:  # keep the three newest
            if ctx.inside_root(d):
                shutil.rmtree(d, ignore_errors=True)
        note = R.needs_person(ctx.paths, ctx.automation, f"signin:{ctx.automation}", f"{ctx.automation}: sign in again",
                              "The browser profile was corrupt and has been replaced. Open the automation once (python -m wrapper_core run "
                              f"{ctx.automation}) and sign in to Microsoft when Edge asks; later runs reuse the new profile.", ctx.now())
        return f"profile quarantined to profiles.bad/{dest.name}; fresh profile created; {note}"

    def verify(self, ctx: Context) -> bool:
        prof = R.profile_dir(ctx.paths, ctx.automation)
        return prof.is_dir() and not any(prof.iterdir())

    def rollback(self, ctx: Context) -> None:
        dest = ctx.scratch.get("quarantined")
        prof = R.profile_dir(ctx.paths, ctx.automation)
        if dest and Path(dest).exists():
            shutil.rmtree(prof, ignore_errors=True)
            os.replace(str(dest), str(prof))


# ---------------------------------------------------------------------------------------------------- scheduled tasks
def _slots_task_names(paths: Paths, name: str) -> List[str]:
    from . import selfheal
    from .scheduler import task_name
    sched = selfheal.payload_defaults(paths, name).get("schedule")
    slots = (sched.get("slots") or []) if isinstance(sched, dict) else []
    out = []
    for s in slots:
        try:
            out.append(task_name(name, str(s["id"])))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def task_status(paths: Paths, name: str, runner: Optional[Callable] = None) -> str:
    """'ok' | 'missing' | 'disabled' | 'unknown' (not Windows and no runner injected, or no tasks expected)."""
    from .scheduler import Scheduler, default_runner
    if runner is None and sys.platform != "win32":
        return "unknown"
    sched = Scheduler(paths, runner=runner)
    names = sched._owned(sched._state(), name) or _slots_task_names(paths, name)
    if not names:
        return "unknown"
    run = runner or default_runner
    for n in names:
        if not sched.exists(n):
            return "missing"
        try:
            out = (run(["schtasks", "/Query", "/TN", n, "/FO", "LIST", "/V"]).stdout or "").lower()
        except (OSError, subprocess.SubprocessError):
            continue
        if any("scheduled task state" in ln and "disabled" in ln for ln in out.splitlines()):
            return "disabled"
    return "ok"


def _schedule_for(paths: Paths, cfg: dict, name: str) -> Optional[dict]:
    from . import manifest as mf
    from . import secrets, selfheal
    entry = dict(selfheal.payload_defaults(paths, name))
    try:
        if cfg.get("registry_url"):
            m, _off = selfheal.fetch_manifest(paths, cfg["registry_url"], secrets.get_registry_token(paths), cfg=cfg)
            entry.update(mf.automation(m, name) or {})
    except Exception:  # noqa: BLE001 - offline / untrusted registry: fall back to the schedule shipped in the payload
        pass
    sch = entry.get("schedule")
    return sch if isinstance(sch, dict) else None


def reassert_tasks(ctx: Context, names: List[str]) -> int:
    from .scheduler import Scheduler
    sched = Scheduler(ctx.paths, runner=ctx.scheduler_runner)
    touched = 0
    for name in names:
        sch = _schedule_for(ctx.paths, ctx.cfg, name)
        if sch:
            touched += len(sched.register(name, sch, force=True))
    sched.write_startup_script()
    return touched


class ReassertScheduledTasks(Playbook):
    name = "reassert_scheduled_tasks"
    categories = ("SCHEDULED_TASK_MISSING", "SCHEDULED_TASK_DISABLED")
    description = "re-register the automation's Task Scheduler tasks (and the login script) / re-enable a disabled task"

    def _names(self, ctx: Context) -> List[str]:
        from . import selfheal
        return [ctx.automation] if ctx.automation else sorted(selfheal.read_installed(ctx.paths)["automations"])

    def check(self, ctx: Context) -> bool:
        if not ctx.automation:
            return True  # startup-script repair for all installed automations
        return task_status(ctx.paths, ctx.automation, ctx.scheduler_runner) in ("missing", "disabled")

    def fix(self, ctx: Context):
        from .scheduler import Scheduler, default_runner
        done = 0
        if ctx.diagnosis.category == "SCHEDULED_TASK_DISABLED" and ctx.automation:
            sched = Scheduler(ctx.paths, runner=ctx.scheduler_runner)
            run = ctx.scheduler_runner or default_runner
            for n in sched._owned(sched._state(), ctx.automation) or _slots_task_names(ctx.paths, ctx.automation):
                if run(["schtasks", "/Change", "/TN", n, "/ENABLE"]).returncode == 0:
                    done += 1
            return f"re-enabled {done} task(s)"
        done = reassert_tasks(ctx, self._names(ctx))
        return f"re-registered {done} task(s) and the login script"

    def verify(self, ctx: Context) -> bool:
        if not ctx.automation:
            return True
        return task_status(ctx.paths, ctx.automation, ctx.scheduler_runner) in ("ok", "unknown")


# ---------------------------------------------------------------------------------------------------- 5. damaged / missing components
class RepairComponent(Playbook):
    name = "repair_component"
    categories = ("COMPONENT_CORRUPT", "MISSING_FILE", "NODE_MISSING")
    retry_after_fix = True
    max_attempts = 2
    cooldown_s = 300
    description = "re-fetch the damaged or missing component from the signed registry (hash-verified), then re-assert scheduled tasks"

    def _affected(self, ctx: Context) -> List[str]:
        if not ctx.automation:
            return []
        bad = R.missing_components(ctx.paths, ctx.automation, R.runtime_of(ctx.paths, ctx.automation))
        if ctx.diagnosis.category == "COMPONENT_CORRUPT" and not bad:
            bad = [f"automation:{ctx.automation}"]
        return bad

    def check(self, ctx: Context) -> bool:
        return bool(self._affected(ctx))

    @staticmethod
    def _dir(ctx: Context, comp: str) -> Path:
        if comp == "wrapper-core":
            return ctx.paths.wrapper_core
        if comp == "execution-framework":
            return ctx.paths.framework
        return ctx.paths.automation_dir(comp.split(":", 1)[1])

    def fix(self, ctx: Context):
        from . import selfheal
        affected = self._affected(ctx)
        data = selfheal.read_installed(ctx.paths)
        before = json.loads(json.dumps(data))
        markers: dict = {}
        ctx.scratch.update(installed_before=before, affected=affected, markers=markers)
        pend = pending_repair_file(ctx.paths, ctx.automation)
        # the pending record goes to disk BEFORE anything is wiped: if the process dies mid-repair, recover_pending_repairs() restores it
        write_json(pend, {"installed_before": before, "markers": markers, "automation": ctx.automation, "at": iso(ctx.now())})
        for comp in affected:
            m = self._dir(ctx, comp) / "VERSION"  # removing the marker AND the install record makes self-heal treat the component as not installed
            if m.exists() and ctx.inside_root(m):
                markers[str(m)] = m.read_text(encoding="utf-8")
                m.unlink()
            if comp.startswith("automation:"):
                data["automations"].pop(comp.split(":", 1)[1], None)
            else:
                data["components"].pop(comp, None)
        write_json(pend, {"installed_before": before, "markers": markers, "automation": ctx.automation, "at": iso(ctx.now())})
        selfheal.write_installed(ctx.paths, data)
        heal = selfheal.run(ctx.paths, ctx.cfg, ctx.automation)
        ctx.scratch["heal_installed"] = list(heal.installed)
        note = "; ".join(heal.installed) or "nothing was re-installed"
        if heal.errors:
            note += " | " + R.scrub_text("; ".join(heal.errors), 160)
        if heal.installed:
            try:
                reassert_tasks(ctx, [ctx.automation])
            except Exception:  # noqa: BLE001 - tasks are best effort here
                pass
        return note

    def verify(self, ctx: Context) -> bool:
        """Fixed only with positive evidence of a reinstall: something was installed, the version marker is back and the install
        record names the component again. 'The files still exist' proves nothing for a corrupt component."""
        from . import selfheal
        if not ctx.scratch.get("heal_installed"):
            return False
        installed = selfheal.read_installed(ctx.paths)
        staged = (ctx.paths.root / "wrapper-core.pending").exists()
        for comp in ctx.scratch.get("affected") or []:
            if comp == "wrapper-core" and staged:
                continue  # staged; the launcher swaps it in at the next start
            if not (self._dir(ctx, comp) / "VERSION").exists():
                return False
            if comp.startswith("automation:"):
                if comp.split(":", 1)[1] not in installed["automations"]:
                    return False
            elif comp not in installed["components"]:
                return False
        bad = R.missing_components(ctx.paths, ctx.automation, R.runtime_of(ctx.paths, ctx.automation))
        if bad == ["wrapper-core"] and staged:
            bad = []
        if bad:
            return False
        _clear_pending_repair(ctx.paths, ctx.automation)
        return True

    def rollback(self, ctx: Context) -> None:
        from . import selfheal
        for path, text in (ctx.scratch.get("markers") or {}).items():
            p = Path(path)
            if p.parent.exists() and not p.exists():
                p.write_text(text, encoding="utf-8")
        before = ctx.scratch.get("installed_before")
        if isinstance(before, dict):
            selfheal.write_installed(ctx.paths, before)
        _clear_pending_repair(ctx.paths, ctx.automation)


def pending_repair_file(paths: Paths, automation: Optional[str]) -> Path:
    check_name(automation or "platform")
    return paths.state / "repair-pending" / f"{automation or 'platform'}.json"


def _clear_pending_repair(paths: Paths, automation: Optional[str]) -> None:
    try:
        pending_repair_file(paths, automation).unlink()
    except (OSError, ValueError):
        pass


def recover_pending_repairs(paths: Paths) -> List[str]:
    """A repair that died half way (process killed, power loss) left a pending record: restore the pre-repair install record and
    version markers so the component is not left invisible to self-heal, doctor and the scheduler. Never raises."""
    done: List[str] = []
    d = paths.state / "repair-pending"
    try:
        files = sorted(d.glob("*.json")) if d.is_dir() else []
    except OSError:
        return done
    from . import selfheal
    from .util import read_json
    for f in files:
        try:
            rec = read_json(f, None)
            if isinstance(rec, dict):
                for path, text in (rec.get("markers") or {}).items():
                    p = Path(path)
                    if R.inside_root(paths, p) and p.parent.exists() and not p.exists():
                        p.write_text(str(text), encoding="utf-8")
                before = rec.get("installed_before")
                installed = selfheal.read_installed(paths)
                if isinstance(before, dict) and isinstance(before.get("automations"), dict) and isinstance(before.get("components"), dict):
                    for k in ("automations", "components"):  # only put back entries that are missing: never overwrite a newer record
                        for name, ver in before[k].items():
                            installed[k].setdefault(name, ver)
                    selfheal.write_installed(paths, installed)
            f.unlink()
            done.append(f.stem)
        except Exception:  # noqa: BLE001
            continue
    return done


class RefreshComponents(Playbook):
    name = "refresh_components"
    categories = ("DEPENDENCY_VERSION_MISMATCH",)
    retry_after_fix = True
    max_attempts = 2
    cooldown_s = 300
    description = "run the self-heal pass now to pick up a compatible wrapper-core / execution-framework from the registry"

    def check(self, ctx: Context) -> bool:
        return bool(ctx.automation)

    def fix(self, ctx: Context):
        from . import selfheal
        heal = selfheal.run(ctx.paths, ctx.cfg, ctx.automation)
        ctx.scratch["installed"] = heal.installed
        return ("; ".join(heal.installed) or "the registry has no newer compatible version") + \
               ((" | " + R.scrub_text("; ".join(heal.errors), 160)) if heal.errors else "")

    def verify(self, ctx: Context) -> bool:
        return bool(ctx.scratch.get("installed"))


# ---------------------------------------------------------------------------------------------------- 6. local data pruning
OWN_LEFTOVERS = ("wrapper-core.old", "wrapper-core.staging", "execution-framework.old", "execution-framework.staging")


def prune_local(ctx: Context, max_delete: int = 500) -> Tuple[int, int]:
    """Delete this tool's OWN old data inside the install folder. Returns (items deleted, bytes freed). Never touches secrets/."""
    root = ctx.paths.root
    now = time.time()
    deleted = freed = 0

    def size(p: Path) -> int:
        try:
            if p.is_file():
                return p.stat().st_size
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())
        except OSError:
            return 0

    def drop(p: Path) -> None:
        nonlocal deleted, freed
        if deleted >= max_delete or not ctx.inside_root(p):
            return
        n = size(p)
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
        except OSError:
            return
        if not p.exists():
            deleted += 1
            freed += n

    def old(p: Path, days: float) -> bool:
        try:
            return now - p.stat().st_mtime > days * 86400
        except OSError:
            return False

    def entries(d: Path, only_dirs=False) -> List[Path]:
        try:
            xs = [x for x in d.iterdir() if (x.is_dir() if only_dirs else True)]
        except OSError:
            return []
        return sorted(xs, key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)

    for d in entries(ctx.paths.runs, True)[5:]:       # keep the five newest run folders whatever their age
        if old(d, 3):
            drop(d)
    for f in ctx.paths.logs.glob("wrapper.log.*") if ctx.paths.logs.exists() else []:
        drop(f)
    for d in entries(root / "debug-bundles")[3:]:
        if old(d, 14):
            drop(d)
    for d in entries(root / "profiles.bad", True):
        if old(d, 7):
            drop(d)
    for d in entries(root):  # only the exact folder names the updater creates: AUTOMAGIX_HOME may be a shared directory
        if d.is_dir() and d.name in OWN_LEFTOVERS and old(d, 1):
            drop(d)
    for d in entries(ctx.paths.automations, True):
        if d.name.endswith((".old", ".staging")) and old(d, 1):
            drop(d)
    # the spool holds telemetry that has not been delivered yet: never deleted here, whatever its age
    return deleted, freed


class PruneLocalData(Playbook):
    name = "prune_local_data"
    categories = ("DISK_LOW", "TEMP_FULL")
    retry_after_fix = True
    max_attempts = 3
    description = "delete this tool's own old run folders, rotated logs, debug bundles, quarantined profiles and leftover update folders"

    def fix(self, ctx: Context):
        before = R.free_mb(ctx.paths)
        n, freed = prune_local(ctx)
        ctx.scratch["freed"] = freed
        after = R.free_mb(ctx.paths)
        return f"deleted {n} item(s), freed {freed // (1024 * 1024)} MB (free: {before} -> {after} MB)"

    def verify(self, ctx: Context) -> bool:
        f = R.free_mb(ctx.paths)
        return ctx.scratch.get("freed", 0) > 0 or (f is not None and f >= R.MIN_FREE_MB)


# ---------------------------------------------------------------------------------------------------- 7. network diagnosis
RECOMMEND = {
    "NETWORK_UNREACHABLE": "The registry could not be reached. The automation keeps running from the cached manifest; check VPN/Wi-Fi, then run `python -m wrapper_core doctor`.",
    "DNS_FAILURE": "A host name could not be resolved: check DNS/VPN (`nslookup <host>`); a VPN that is not connected is the usual cause.",
    "PROXY_FAILURE": "A proxy refused or required authentication (HTTP 407 / tunnel failed): check the system proxy settings and that you are signed in to it.",
    "TLS_FAILURE": "A TLS certificate check failed: a corporate TLS-inspection root may be missing, or the system clock is wrong (see the clock check).",
    "CLOCK_SKEW": "The system clock differs from the registry's by minutes: certificates and tokens fail until it is corrected (Settings > Time & language > Sync now, or `w32tm /resync`).",
}


def default_http_head(url: str) -> dict:
    import urllib.request
    from . import selfheal
    req = urllib.request.Request(selfheal.check_url(url), method="HEAD", headers={"User-Agent": "wrapper-core-remediation"})
    with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 - https/loopback only via check_url
        return {k: v for k, v in r.headers.items()}


def clock_skew_seconds(url: str, head: Optional[Callable] = None, now: Optional[datetime] = None) -> Optional[float]:
    """Local clock minus the server's ``Date`` header (seconds), or None if it could not be measured."""
    try:
        hdrs = (head or default_http_head)(url)
        date = next((v for k, v in hdrs.items() if k.lower() == "date"), None)
        if not date:
            return None
        server = parsedate_to_datetime(date)
        if server.tzinfo is None:
            server = server.replace(tzinfo=timezone.utc)
        return ((now or utcnow()) - server).total_seconds()
    except Exception:  # noqa: BLE001 - offline is exactly the case this runs in
        return None


class DiagnoseNetwork(Playbook):
    name = "diagnose_network"
    categories = ("NETWORK_UNREACHABLE", "DNS_FAILURE", "PROXY_FAILURE", "TLS_FAILURE", "CLOCK_SKEW")
    description = "read-only: classify the network problem and measure clock skew against the registry"
    cooldown_s = 120

    def fix(self, ctx: Context):
        cat = ctx.diagnosis.category
        text = RECOMMEND[cat]
        reg = ctx.cfg.get("registry_url")
        if reg:
            skew = clock_skew_seconds(str(reg), ctx.http_head, ctx.now())
            if skew is not None and abs(skew) > 300:
                text = f"System clock is {int(abs(skew) // 60)} min {'ahead of' if skew > 0 else 'behind'} the registry's. " + RECOMMEND["CLOCK_SKEW"]
                ctx.scratch["skew"] = skew
        return text, "recommended"


# ---------------------------------------------------------------------------------------------------- 8. a person is needed
class RequestSignin(Playbook):
    name = "request_human_signin"
    categories = ("SIGNIN_REQUIRED", "AUTH_EXPIRED")
    max_attempts = 3
    description = "tell the user ONCE (toast + attention file + approval task) that a sign-in is needed, with exact steps; never retried in a loop"

    def check(self, ctx: Context) -> bool:
        return bool(ctx.automation)

    def fix(self, ctx: Context):
        a = ctx.automation
        if ctx.diagnosis.category == "SIGNIN_REQUIRED":
            title, body = f"{a}: sign-in needed", (f"The browser profile for {a} is not signed in. Open Edge, sign in to Microsoft with your work account, "
                                                   f"then run the scheduled task again (Task Scheduler > AUTOMAGIX > {a}-daily > Run).")
        else:
            title, body = f"{a}: credentials expired", (f"A session or credential used by {a} expired. Refresh it (sign in again or renew the stored credential), "
                                                        "then run the scheduled task again.")
        return R.needs_person(ctx.paths, a, f"{ctx.diagnosis.category}:{a}", title, body, ctx.now()), "escalated"


# ---------------------------------------------------------------------------------------------------- 9. recommend-only
class RecommendOnly(Playbook):
    name = "recommend_only"
    categories = ("PYTHON_RUNTIME_BROKEN", "EDGE_MISSING", "GIT_MISSING")
    risk = R.MANUAL
    TEXT = {
        "PYTHON_RUNTIME_BROKEN": "The bundled Python is damaged (missing DLL). Re-run the automation's installer: it replaces the interpreter folder.",
        "EDGE_MISSING": "Microsoft Edge was not found. Browser automations drive the installed Edge: install/repair Edge via your software centre.",
        "GIT_MISSING": "git/ssh was not found. It is only needed to ship logs over SSH: use the connected (HTTPS) installer or install Git for Windows.",
    }
    description = "see recommendation"

    def recommend(self, ctx: Context) -> str:
        return self.TEXT.get(ctx.diagnosis.category, self.description)


# ---------------------------------------------------------------------------------------------------- 10. unknown
class CaptureDebugBundle(Playbook):
    name = "capture_debug_bundle"
    categories = ("UNKNOWN",)
    max_attempts = 5
    description = "write a redacted debug bundle (doctor output, log tails, versions, run files) to attach to a support ticket"

    def fix(self, ctx: Context):
        rel = R.bundle_once(ctx.paths, ctx.diagnosis, ctx.run_id, ctx.automation)
        ctx.scratch["bundle"] = rel
        return (f"debug bundle written: {Path(rel).name}" if rel else "a debug bundle for this failure was already captured"), "recommended"


def all_playbooks() -> List[Playbook]:
    return [DevtoolsNativeMode(), ClearStaleRunLock(), ReleaseProfileLock(), QuarantineProfile(), ReassertScheduledTasks(),
            RepairComponent(), RefreshComponents(), PruneLocalData(), DiagnoseNetwork(), RequestSignin(), RecommendOnly(), CaptureDebugBundle()]
