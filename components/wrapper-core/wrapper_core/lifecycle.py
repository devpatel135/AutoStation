"""Install / maintain / uninstall (what the NSIS installer and the login script call).

* ``install_automation``: self-heal-style install of shared components + the payload, then register the schedule.
* ``maintain``: re-assert Task Scheduler entries for every installed automation (+ heartbeat). Runs at every login.
* ``uninstall_automation``: remove tasks, payload, reference-counted shared components, key blob, credentials;
  pushes a final ``decommissioned`` lifecycle event *before* the key is deleted.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import manifest as mf
from . import notify, secrets, selfheal, transport
from .heartbeat import send_heartbeat, send_lifecycle
from .paths import Paths, check_name, get_logger, platform_config
from .scheduler import Scheduler
from .util import read_json, write_json


def install_automation(name: str, paths: Paths | None = None, scheduler: Scheduler | None = None) -> dict:
    paths = (paths or Paths()).ensure()
    try:
        check_name(name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "installed": []}
    cfg = platform_config(paths)
    heal = selfheal.run(paths, cfg, name)
    if heal.halted:
        return {"ok": False, "error": f"automation is paused: {heal.halt_reason}", "installed": heal.installed}
    if not paths.automation_dir(name).exists():
        return {"ok": False, "error": "payload not installed: " + ("; ".join(heal.errors) or "unknown"), "installed": heal.installed}
    entry = {**selfheal.payload_defaults(paths, name), **((mf.automation(heal.manifest, name) if heal.manifest else None) or {})}
    sched = scheduler or Scheduler(paths)
    touched = sched.register(name, (entry or {}).get("schedule"), force=True)
    sched.write_startup_script()
    selfheal.write_installed(paths, selfheal.read_installed(paths))
    send_lifecycle(paths, "installed", name, {"installed": heal.installed, "versions": heal.versions})
    send_heartbeat(paths, "login", None)
    return {"ok": True, "installed": heal.installed, "tasks": touched, "errors": heal.errors}


def maintain(paths: Paths | None = None, scheduler: Scheduler | None = None) -> dict:
    paths = (paths or Paths()).ensure()
    log = get_logger(paths)
    cfg = platform_config(paths)
    sched = scheduler or Scheduler(paths)
    m = None
    try:
        if cfg.get("registry_url"):  # falls back to the signature-verified cache itself; HealError = nothing trustworthy
            m, _offline = selfheal.fetch_manifest(paths, cfg["registry_url"], secrets.get_registry_token(paths), cfg=cfg)
    except selfheal.HealError as exc:
        log.warning("maintain: manifest unavailable: %s", exc)
    touched: dict[str, list[str]] = {}
    for name in sorted(selfheal.read_installed(paths)["automations"]):
        try:
            entry = {**selfheal.payload_defaults(paths, name), **((mf.automation(m, name) if m else None) or {})}
            if not isinstance(entry.get("schedule"), dict):
                continue
            touched[name] = sched.register(name, entry.get("schedule"))
        except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the others being re-asserted
            log.warning("maintain: cannot register %s: %s", name, exc)
    # ping the registry at every sign-in: pick up new versions / the kill-switch without waiting for the next scheduled run
    updated: dict[str, list[str]] = {}
    for name in sorted(selfheal.read_installed(paths)["automations"]):
        try:
            heal = selfheal.run(paths, cfg, name)
            if heal.installed:
                updated[name] = heal.installed
                send_lifecycle(paths, "updated", name, {"installed": heal.installed, "via": "login-check"})
        except Exception as exc:  # noqa: BLE001 - an update check must never break sign-in
            log.warning("update check for %s failed: %s", name, exc)
    transport.flush_spool(paths)
    send_heartbeat(paths, "login")
    return {"touched": touched, "updated": updated}


def _other_automations_need(paths: Paths, removed: str) -> bool:
    """Reference count: do any other installed automations still use the shared components?"""
    return any(n != removed for n in selfheal.read_installed(paths)["automations"])


def uninstall_automation(name: str, paths: Paths | None = None, scheduler: Scheduler | None = None,
                         purge_shared: bool = True) -> dict:
    paths = paths or Paths()
    log = get_logger(paths)
    check_name(name)  # becomes directory/file/task names below
    report = {"automation": name, "removed": [], "kept": []}
    # 1. final log event while the key still exists
    sent = send_lifecycle(paths, "decommissioned", name, {"ring": platform_config(paths)["ring"]})
    report["decommission_event_sent"] = sent
    # 2. tasks + launchers
    sched = scheduler or Scheduler(paths)
    sched.unregister(name)
    report["removed"].append("scheduled tasks")
    # 3. payload
    adir = paths.automation_dir(name)
    if adir.exists():
        shutil.rmtree(adir, ignore_errors=True)
        report["removed"].append(str(adir))
    for f in (paths.root / "bundled").glob(f"{name}.*"):
        f.unlink(missing_ok=True)
    data = selfheal.read_installed(paths)
    data["automations"].pop(name, None)
    selfheal.write_installed(paths, data)
    paths.last_run_file(name).unlink(missing_ok=True)
    paths.status_file(name).unlink(missing_ok=True)
    # 4. shared components + secrets only when nothing else needs them
    if not data["automations"] and purge_shared:
        sched.remove_startup_script()
        for d in (paths.framework, paths.wrapper_core):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                report["removed"].append(str(d))
        for f in (paths.secrets / "logging_key.dpapi", paths.secrets / "logging_token.dpapi",
                  paths.secrets / "registry_token.dpapi"):
            if f.exists():
                secrets.wipe_file(f)
                report["removed"].append(str(f))
        tmp = paths.secrets / "tmp"  # materialised git-ssh keys (normally wiped right after each push; a crash can leave one)
        if tmp.is_dir():
            for f in tmp.iterdir():
                if f.is_file():
                    secrets.wipe_file(f)
            shutil.rmtree(tmp, ignore_errors=True)
            report["removed"].append(str(tmp))
        for target in ("AUTOMAGIX/registry_token", "AUTOMAGIX/llm_key"):
            secrets.credman_delete(target)
        report["removed"].append("credential manager entries")
    else:
        report["kept"].append("shared components (still used by other automations)")
    log.info("uninstalled %s: %s", name, report)
    return report
