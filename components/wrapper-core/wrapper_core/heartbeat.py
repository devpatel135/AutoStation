"""Heartbeats and lifecycle events (SPEC §6.3, §6.4). Best effort: spooled on failure, never raise."""
from __future__ import annotations

from . import selfheal, transport
from .paths import Paths, platform_config
from .util import hostname, iso, user_hash, utcnow


def send_heartbeat(paths: Paths, phase: str, automation: str | None = None, run_id: str | None = None,
                   expected_finish_at: str | None = None) -> bool:
    if phase in ("login", "idle"):  # a heartbeat that shows the machine is idle: a good moment to repair what would fail the next run
        try:
            from . import remediation
            remediation.proactive_pass(paths)
        except Exception:  # noqa: BLE001 - housekeeping must never stop a heartbeat
            pass
    cfg = platform_config(paths)
    snap = selfheal._versions_snapshot(paths)
    now = utcnow()
    host = hostname()
    payload = {"schema_version": 1, "kind": "heartbeat", "host": host, "user_hash": user_hash(), "ts": iso(now),
               "phase": phase, "automation": automation, "run_id": run_id, "expected_finish_at": expected_finish_at,
               "versions": snap, "ring": cfg["ring"]}
    return transport.emit("heartbeat", host, f"{now.strftime('%Y%m%dT%H%M%S')}-{phase}", payload, paths)


def send_lifecycle(paths: Paths, event: str, automation: str | None, detail: dict | None = None) -> bool:
    now = utcnow()
    host = hostname()
    art_id = f"{now.strftime('%Y%m%dT%H%M%S%f')}-{event}"
    payload = {"schema_version": 1, "kind": "lifecycle", "event": event, "automation": automation, "host": host,
               "user_hash": user_hash(), "ts": iso(now), "detail": detail or {}}
    return transport.emit("lifecycle", automation or host, art_id, payload, paths)
