"""The run lifecycle - what Task Scheduler actually launches (``python -m wrapper_core run <automation>``).

    lock -> spool flush -> self-heal (+kill-switch) -> maintenance window -> compat check -> trigger selection
         -> execute payload (watchdog, retry, LLM broker) -> status/items/error -> synopsis -> log + heartbeat -> notify
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import manifest as mf
from . import miniyaml, notify, runctx, selfheal, transport, versions
from ._version import __version__ as WRAPPER_VERSION
from .broker import Broker
from .heartbeat import send_heartbeat, send_lifecycle
from .logging import log_event
from .paths import Paths, get_logger, platform_config
from .retry import (Category, Escalation, RetryPolicy, Skipped, AuthExpired, DataError, LogicError, NetworkError,
                    SystemUnavailable, classify_text, run_with_retry)
from .synopsis import build as build_synopsis
from .util import hostname, iso, new_run_id, read_json, user_hash, utcnow, write_json

EXIT_OK, EXIT_FAILED, EXIT_HUNG = 0, 1, 2
_SECRET_ENV = ("AUTOSTATION_LLM_KEY", "AUTOMAGIX_REGISTRY_TOKEN")


def _sleep(seconds: float) -> None:  # indirection so tests can run instantly
    time.sleep(seconds)


@dataclass
class RunOutcome:
    record: dict | None
    exit_code: int


class RunLock:
    """File lock with age-based staleness (os.kill(pid, 0) would TERMINATE the process on Windows - never use it)."""

    def __init__(self, path: Path, ttl_s: int):
        self.path, self.ttl_s, self.held = path, ttl_s, False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time()}".encode())
                os.close(fd)
                self.held = True
                return True
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.ttl_s:
                        self.path.unlink()  # stale: previous run died hard
                        continue
                except OSError:
                    pass
                return False
        return False

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass


def _tail(path: Path, n: int = 4000) -> str:
    try:
        data = path.read_bytes()
        return data[-n:].decode("utf-8", "replace")
    except OSError:
        return ""


def _descriptor(adir: Path) -> dict:
    f = adir / "automation.yaml"
    if not f.exists():
        raise FileNotFoundError(f"{f} not found")
    d = miniyaml.load(f)
    if not isinstance(d, dict) or not d.get("entrypoint"):
        raise ValueError("automation.yaml needs an entrypoint")
    d.setdefault("runtime", "python")
    d.setdefault("timeout_s", 1800)
    d.setdefault("args", [])
    return d


def build_command(paths: Paths, adir: Path, desc: dict) -> tuple[list[str], dict]:
    runtime, entry = desc["runtime"], str(adir / desc["entrypoint"])
    extra_env: dict = {}
    args = [str(a) for a in desc.get("args") or []]
    wrapper_dir = paths.wrapper_core if (paths.wrapper_core / "wrapper_core").exists() else Path(__file__).resolve().parent.parent
    if runtime == "python":
        cmd = [os.environ.get("AUTOMAGIX_PYTHON", sys.executable), entry] + args
        extra_env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(wrapper_dir), os.environ.get("PYTHONPATH", "")]))
        extra_env["PYTHONUTF8"] = "1"
    elif runtime == "node":
        node = os.environ.get("AUTOMAGIX_NODE") or str(paths.framework / "node" / "node.exe")
        if not Path(node).exists():
            node = shutil.which("node") or node
        cmd = [node, entry] + args
        extra_env["NODE_PATH"] = str(paths.framework / "node_modules")
    elif runtime == "powershell":
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", entry] + args
    elif runtime == "exe":
        cmd = [entry] + args
    else:
        raise ValueError(f"unknown runtime {runtime!r}")
    return cmd, extra_env


def _aggregate_items(events: list[dict]) -> dict:
    counts = {"total": 0, "success": 0, "skipped": 0, "failed": 0}
    for e in events:
        if e.get("event") == "item_processed":
            o = (e.get("data") or {}).get("outcome")
            counts["total"] += 1
            if o in counts:
                counts[o] += 1
    return counts


def _exc_for(cat: Category, msg: str) -> Exception:
    return {Category.NETWORK_ERROR: NetworkError, Category.AUTH_EXPIRED: AuthExpired,
            Category.SYSTEM_UNAVAILABLE: SystemUnavailable, Category.DATA_ERROR: DataError,
            Category.LOGIC_ERROR: LogicError}.get(cat, RuntimeError)(msg)


class _Execution:
    """One attempt-loop around the payload process, with watchdog and process-level retry."""

    def __init__(self, paths, ctx, name, desc, adir, env, policy, entry, started_mono, expected_ms, hung_after_ms, cfg):
        self.paths, self.ctx, self.name, self.desc, self.adir = paths, ctx, name, desc, adir
        self.env, self.policy, self.entry = env, policy, entry
        self.started_mono, self.expected_ms, self.hung_after_ms = started_mono, expected_ms, hung_after_ms
        self.cfg = cfg
        self.hung_detected = False
        self.timed_out = False
        self.exit_code: int | None = None
        self.stderr_tail = ""

    def _launch_once(self) -> None:
        cmd, extra = build_command(self.paths, self.adir, self.desc)
        env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV and not k.startswith("AUTOMAGIX_CRED_")}
        env.update(self.env)
        env.update(extra)
        out_f, err_f = self.ctx.run_dir / "stdout.log", self.ctx.run_dir / "stderr.log"
        (self.ctx.run_dir / "error.json").unlink(missing_ok=True)
        timeout_s = float(self.desc.get("timeout_s", 1800))
        with open(out_f, "ab") as so, open(err_f, "ab") as se:
            proc = subprocess.Popen(cmd, cwd=str(self.adir), env=env, stdout=so, stderr=se, stdin=subprocess.DEVNULL)
            while True:
                try:
                    self.exit_code = proc.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    pass
                elapsed_ms = (time.monotonic() - self.started_mono) * 1000
                if not self.hung_detected and self.hung_after_ms and elapsed_ms > self.hung_after_ms:
                    self.hung_detected = True
                    data = {"elapsed_ms": int(elapsed_ms), "expected_ms": self.expected_ms}
                    log_event("run_hung", data)
                    send_lifecycle(self.paths, "run_hung", self.name, {"run_id": self.ctx.run_id, **data})
                if elapsed_ms > timeout_s * 1000:
                    self.timed_out = True
                    _kill_tree(proc)
                    proc.wait()
                    self.exit_code = -9
                    break
        self.stderr_tail = _tail(err_f)

    def execute(self) -> Exception | None:
        """Run with process-level retry. Returns None on success, else the terminal exception."""
        def attempt():
            self._launch_once()
            if self.timed_out:
                raise Escalation("run exceeded timeout_s and was killed", Category.SYSTEM_UNAVAILABLE, 1)
            if self.exit_code == 0:
                return True
            err = self.ctx.read_error() or {}
            if err.get("escalated"):
                raise Escalation(f"{err.get('category')}: {err.get('message')}", Category(err.get("category", "UNKNOWN")),
                                 int(err.get("attempts", 1)))
            cat = Category(err["category"]) if err.get("category") in Category.__members__ else classify_text(self.stderr_tail)
            msg = err.get("message") or self.stderr_tail.strip().splitlines()[-1:] and self.stderr_tail.strip().splitlines()[-1] or f"exit code {self.exit_code}"
            raise _exc_for(cat, str(msg))

        try:
            result = run_with_retry(attempt, self.policy, sleep=lambda s: _sleep(s))
            if isinstance(result, Skipped):  # DATA_ERROR for a whole run: don't retry, report failure
                return DataError(self.stderr_tail.strip().splitlines()[-1] if self.stderr_tail.strip() else "data error")
            return None
        except Escalation as esc:
            return esc


def _expected_ms(schedule: dict | None) -> tuple[int | None, int | None]:
    e = (schedule or {}).get("expected_duration_ms") or {}
    base = e.get("baseline")
    if not base:
        return None, None
    return int(base), int(base * float(e.get("warn_threshold", 2.0)))


def _finalise(paths: Paths, record: dict, ctx, dry_run: bool, use_llm: bool = True) -> None:
    events = ctx.read_events()
    record["events"] = events
    record["items"] = _aggregate_items(events)
    record["synopsis"], record["synopsis_source"] = build_synopsis(record, use_llm=use_llm)
    if dry_run:
        print(json.dumps({k: v for k, v in record.items() if k != "events"}, indent=2, default=str))
        return
    transport.emit("run", record["automation"], record["run_id"], record, paths)


def run_automation(name: str, slot_id: str | None = None, dry_run: bool = False, paths: Paths | None = None,
                   ignore_maintenance: bool = False) -> RunOutcome:
    paths = (paths or Paths()).ensure()
    log = get_logger(paths)
    cfg = platform_config(paths)
    ring = cfg["ring"]

    # applying a staged wrapper-core is the launcher's job; do it here too when launched directly
    try:
        selfheal.apply_pending(paths)
    except Exception as exc:  # noqa: BLE001
        log.warning("apply_pending failed: %s", exc)

    lock = RunLock(paths.lock_file(name), ttl_s=4 * 3600 + 600)
    if not lock.acquire():
        log.info("%s already running - exiting", name)
        return RunOutcome(None, EXIT_OK)
    try:
        if not dry_run:
            transport.flush_spool(paths)
        started_dt = utcnow()
        t0 = time.monotonic()

        heal = selfheal.run(paths, cfg, name)
        m = heal.manifest
        entry = (mf.automation(m, name) if m else None) or {}
        schedule = entry.get("schedule")
        slots = (schedule or {}).get("slots") or []
        slot = next((s for s in slots if str(s.get("id")) == slot_id), None) if slot_id else (slots[0] if slots else None)
        slot_id = slot_id or (str(slot["id"]) if slot else "adhoc")

        def base_record(run_id: str, params: dict) -> dict:
            return {
                "schema_version": 1, "kind": "run", "run_id": run_id, "automation": name, "slot": slot_id,
                "automation_version": heal.versions.get("automations", {}).get(name),
                "wrapper_version": heal.versions.get("wrapper-core") or WRAPPER_VERSION,
                "framework_version": heal.versions.get("execution-framework"), "ring": ring, "host": hostname(),
                "user_hash": user_hash(), "started_at": iso(started_dt), "params": params,
                "self_heal": {"installed": heal.installed, "errors": heal.errors, "offline": heal.offline},
                "hung_detected": False, "exit_code": None, "error": None,
            }

        def finish(record: dict, ctx, status: str, exit_code: int, use_llm: bool = True) -> RunOutcome:
            now = utcnow()
            record.update(status=status, ended_at=iso(now), duration_ms=int((time.monotonic() - t0) * 1000))
            _finalise(paths, record, ctx, dry_run, use_llm)
            if not dry_run:
                write_json(paths.last_run_file(name), {k: record[k] for k in ("run_id", "status", "started_at", "ended_at", "automation_version")})
                send_heartbeat(paths, "run_end", name, record["run_id"])
            _prune_runs(paths)
            return RunOutcome(record, exit_code)

        # ---- kill-switch ---------------------------------------------------------------------------
        if heal.halted:
            ctx = _new_ctx(paths, name, slot_id)
            record = base_record(ctx.run_id, {})
            record["halt_reason"] = heal.halt_reason
            log_event("phase", {"name": "self_heal", "status": "halted", "reason": heal.halt_reason})
            if not dry_run:
                notify.paused_notice(paths, name, heal.halt_reason)
                send_lifecycle(paths, "paused_seen", name, {"reason": heal.halt_reason})
            return finish(record, ctx, "halted_paused", EXIT_OK, use_llm=False)

        notify.clear_status(paths, name)
        if heal.installed and not dry_run:
            notify.version_bump_notice(name, heal.installed)

        # ---- invocations: one for the slot, or one per pending trigger for poll slots ----------------
        slot_params = dict((slot or {}).get("params") or {})
        invocations: list[tuple[dict, str | None]]
        if slot and slot.get("type") == "poll":
            invocations = [({**slot_params, **(t.get("params") or {})}, t.get("id")) for t in heal.triggers]
            if not invocations:
                log.info("%s poll: no pending triggers", name)
                return RunOutcome(None, EXIT_OK)
        else:
            invocations = [(slot_params, None)]

        last: RunOutcome | None = None
        for params, trigger_id in invocations:
            if not dry_run and trigger_id:
                selfheal.consume_trigger(paths, trigger_id)
            t0 = time.monotonic()
            started_dt = utcnow()
            last = _run_once(paths, cfg, name, slot_id, params, trigger_id, heal, entry, schedule, base_record,
                             finish, dry_run, ignore_maintenance, t0)
        return last or RunOutcome(None, EXIT_OK)
    finally:
        lock.release()


def _new_ctx(paths: Paths, name: str, slot_id: str):
    run_id = new_run_id()
    ctx = runctx.RunContext(paths.runs / run_id, run_id, name, slot_id)
    runctx.set_current(ctx)
    return ctx


def _run_once(paths, cfg, name, slot_id, params, trigger_id, heal, entry, schedule, base_record, finish, dry_run,
              ignore_maintenance, t0) -> RunOutcome:
    log = get_logger(paths)
    ctx = _new_ctx(paths, name, slot_id)
    record = base_record(ctx.run_id, params)
    if trigger_id:
        record["trigger_id"] = trigger_id
    adir = paths.automation_dir(name)

    window = None if ignore_maintenance else mf.in_maintenance(schedule)
    if window:
        log_event("phase", {"name": "maintenance_window", "status": "skipped", "window": window})
        return finish(record, ctx, "skipped_maintenance", EXIT_OK, use_llm=False)

    # payload + compatibility
    try:
        if not adir.exists():
            raise FileNotFoundError(f"automation payload {name} is not installed")
        desc = _descriptor(adir)
    except (OSError, ValueError, miniyaml.YamlError) as exc:
        record["error"] = {"category": "LOGIC_ERROR", "type": type(exc).__name__, "message": str(exc)[:300], "attempts": 0}
        if heal.errors:
            record["error"]["message"] += " | self-heal: " + "; ".join(heal.errors)[:300]
        return finish(record, ctx, "self_heal_failed", EXIT_FAILED)

    snap = heal.versions
    wv, fv = snap.get("wrapper-core") or WRAPPER_VERSION, snap.get("execution-framework")
    problems = []
    req = desc.get("requires") or {}
    if not versions.satisfies(wv, req.get("wrapper_core")):
        problems.append(f"wrapper-core {wv} does not satisfy {req.get('wrapper_core')}")
    if fv and not versions.satisfies(fv, req.get("execution_framework")):
        problems.append(f"execution-framework {fv} does not satisfy {req.get('execution_framework')}")
    if heal.manifest and fv and not mf.compatible(heal.manifest, wv, fv):
        problems.append(f"wrapper-core {wv} + execution-framework {fv} is not in the compatibility matrix")
    if problems:
        record["error"] = {"category": "LOGIC_ERROR", "type": "IncompatibleVersions", "message": "; ".join(problems), "attempts": 0}
        return finish(record, ctx, "self_heal_failed", EXIT_FAILED)

    policy = RetryPolicy.from_manifest(entry.get("error_handling"), cwd=str(adir))
    base_ms, hung_ms = _expected_ms(schedule)
    if base_ms:
        record["expected_finish_at"] = iso(utcnow().replace(microsecond=0) + __import__("datetime").timedelta(milliseconds=base_ms))
    if not dry_run:
        send_heartbeat(paths, "run_start", name, ctx.run_id, record.get("expected_finish_at"))
        if heal.installed:
            send_lifecycle(paths, "updated", name, {"installed": heal.installed})

    # LLM gating context for this process (broker + synopsis run in-process)
    saved_env = {k: os.environ.get(k) for k in ("AUTOMAGIX_LLM_ALLOWED_CLASSIFICATION", "AUTOMAGIX_DATA_CLASSIFICATION")}
    if entry.get("llm_allowed_classification"):
        os.environ["AUTOMAGIX_LLM_ALLOWED_CLASSIFICATION"] = str(entry["llm_allowed_classification"])
    if entry.get("data_classification"):
        os.environ["AUTOMAGIX_DATA_CLASSIFICATION"] = str(entry["data_classification"])
    broker = Broker().start()
    try:
        env = {
            "AUTOMAGIX_HOME": str(paths.root), "AUTOMAGIX_RUN_ID": ctx.run_id, "AUTOMAGIX_RUN_DIR": str(ctx.run_dir),
            "AUTOMAGIX_AUTOMATION": name, "AUTOMAGIX_AUTOMATION_VERSION": str(snap.get("automations", {}).get(name, "")),
            "AUTOMAGIX_SLOT": slot_id, "AUTOMAGIX_RING": cfg["ring"], "AUTOMAGIX_FRAMEWORK_DIR": str(paths.framework),
            "AUTOMAGIX_DRY_RUN": "1" if dry_run else "0", **broker.env(),
            "AUTOMAGIX_ERROR_POLICY": json.dumps(entry.get("error_handling") or {}),
        }
        if entry.get("data_classification"):
            env["AUTOMAGIX_DATA_CLASSIFICATION"] = str(entry["data_classification"])
        for k, v in params.items():
            env[f"AUTOMATION_PARAM_{str(k).upper()}"] = str(v)
        log_event("phase", {"name": "execute", "status": "start", "slot": slot_id, "params": sorted(params)})
        ex = _Execution(paths, ctx, name, desc, adir, env, policy, entry, t0, base_ms, hung_ms, cfg)
        failure = ex.execute()
    finally:
        broker.stop()

    record["exit_code"] = ex.exit_code
    record["hung_detected"] = ex.hung_detected
    record["result"] = ctx.read_result()
    events = ctx.read_events()
    item_counts = _aggregate_items(events)

    if failure is None:
        status, code = ("partial" if item_counts["failed"] else "success"), EXIT_OK
    else:
        cat = failure.category.value if isinstance(failure, Escalation) else classify_text(str(failure)).value
        err = ctx.read_error() or {}
        attempts = sum(1 for e in events if e.get("event") == "retry_event") + 1
        record["error"] = {"category": err.get("category") or cat, "type": err.get("type") or type(failure).__name__,
                           "message": str(err.get("message") or failure)[:500],
                           "traceback_tail": err.get("traceback_tail") or ex.stderr_tail.strip().splitlines()[-6:],
                           "attempts": attempts}
        status, code = ("hung", EXIT_HUNG) if ex.timed_out else ("failed", EXIT_FAILED)
    try:
        out = finish(record, ctx, status, code)  # synopsis runs here, still under this automation's LLM gate
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if not dry_run and entry.get("notify_on_complete"):
        notify.run_complete_notice(name, status, record.get("synopsis", ""))
    return out


def _prune_runs(paths: Paths, keep_days: int = 14) -> None:
    cutoff = time.time() - keep_days * 86400
    try:
        for d in paths.runs.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
