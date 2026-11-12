"""Heartbeats and lifecycle events (SPEC §6.3, §6.4). Best effort: spooled on failure, never raise."""
from __future__ import annotations

import json
import os
from datetime import timedelta

from . import selfheal, transport
from .paths import Paths, platform_config
from .util import atomic_write_text, hostname, iso, parse_iso, user_hash, utcnow

IDLE_PHASES = ("login", "idle")


def _idle_every_h() -> float:
    """Hours between 'idle'/'login' heartbeats on the GitHub transport (default 20: about one a day per machine). 0 = no throttle.
    SILENT detection needs one heartbeat per 36 h; run_start/run_end heartbeats are folded into each run's bundle and are not throttled."""
    try:
        return max(0.0, float(os.environ.get("AUTOMAGIX_HEARTBEAT_EVERY_H", "20")))
    except ValueError:
        return 20.0


def _idle_recently_sent(paths: Paths, now) -> bool:
    try:
        data = json.loads((paths.state / "heartbeat_idle.json").read_text(encoding="utf-8"))
        return now - parse_iso(data["ts"]) < timedelta(hours=_idle_every_h())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def health_fields(paths: Paths) -> dict:
    """What an operator needs to know about this machine's ability to report: the offline queue and whether the credential works."""
    out = {"spool": transport.spool_stats(paths)}
    failure = transport.auth_failure(paths)
    if failure:
        out["logging_auth_failed"] = {"http": failure.get("http"), "reason": failure.get("reason"), "ts": failure.get("ts")}
    return out


def send_heartbeat(paths: Paths, phase: str, automation: str | None = None, run_id: str | None = None,
                   expected_finish_at: str | None = None) -> bool:
    if phase in IDLE_PHASES:  # a heartbeat that shows the machine is idle: a good moment to repair what would fail the next run
        try:
            from . import remediation
            remediation.proactive_pass(paths)
        except Exception:  # noqa: BLE001 - housekeeping must never stop a heartbeat
            pass
    cfg = platform_config(paths)
    now = utcnow()
    if phase in IDLE_PHASES and cfg.get("log_transport") == "github-api" and _idle_every_h() > 0 and _idle_recently_sent(paths, now):
        return True   # one idle heartbeat per machine per day is enough and keeps a big fleet inside GitHub's write limits
    snap = selfheal._versions_snapshot(paths)
    host = hostname()
    payload = {"schema_version": 1, "kind": "heartbeat", "host": host, "user_hash": user_hash(), "ts": iso(now),
               "phase": phase, "automation": automation, "run_id": run_id, "expected_finish_at": expected_finish_at,
               "versions": snap, "ring": cfg["ring"]}
    try:
        payload.update(health_fields(paths))
    except Exception:  # noqa: BLE001 - the counters are a bonus, never a reason to lose the heartbeat
        pass
    ok = transport.emit("heartbeat", host, f"{now.strftime('%Y%m%dT%H%M%S')}-{phase}", payload, paths)
    if ok and phase in IDLE_PHASES:
        try:
            atomic_write_text(paths.state / "heartbeat_idle.json", json.dumps({"ts": iso(now)}))
        except OSError:
            pass
    return ok


def send_lifecycle(paths: Paths, event: str, automation: str | None, detail: dict | None = None) -> bool:
    now = utcnow()
    host = hostname()
    art_id = f"{now.strftime('%Y%m%dT%H%M%S%f')}-{event}"
    payload = {"schema_version": 1, "kind": "lifecycle", "event": event, "automation": automation, "host": host,
               "user_hash": user_hash(), "ts": iso(now), "detail": detail or {}}
    return transport.emit("lifecycle", automation or host, art_id, payload, paths)
