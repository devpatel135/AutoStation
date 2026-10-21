"""``python -m wrapper_core doctor`` - one command that tells a user or a developer what is wrong and how to fix it.

Checks the machine, the install, the schedule, the registry and the logging path. Every check is read-only and returns
PASS / WARN / FAIL with a concrete next step. Exit code 1 if anything FAILs. ``--json`` for tooling / support tickets
(the output never contains secrets or tokens).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import selfheal
from ._version import __version__
from .paths import Paths, platform_config
from .util import read_json

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _edge_path() -> str | None:
    cands = [os.path.join(os.environ.get(v, ""), r"Microsoft\Edge\Application\msedge.exe")
             for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    return next((c for c in cands if c and os.path.exists(c)), shutil.which("msedge"))


def run_checks(paths: Paths | None = None, online: bool = True) -> list[dict]:
    paths = paths or Paths()
    res: list[dict] = []

    def add(name: str, status: str, detail: str, fix: str = "") -> None:
        res.append({"check": name, "status": status, "detail": detail, **({"fix": fix} if fix else {})})

    add("python", PASS if sys.version_info >= (3, 9) else FAIL, f"{sys.version.split()[0]} at {sys.executable}",
        "wrapper-core needs Python 3.9+ (the installer ships its own)")
    add("wrapper-core", PASS, __version__)
    add("operating system", PASS if sys.platform == "win32" else WARN, sys.platform,
        "Task Scheduler, DPAPI and Credential Manager features need Windows" if sys.platform != "win32" else "")
    git = shutil.which("git")
    add("git", PASS if git else WARN, git or "not on PATH",
        "needed only to ship logs over SSH (git-ssh transport): install Git for Windows" if not git else "")
    node = shutil.which("node") or (str(paths.framework / "node" / "node.exe") if (paths.framework / "node" / "node.exe").exists() else None)
    add("node (browser automations)", PASS if node else WARN, node or "not found",
        "only Node-based automations (Playwright) need it; the execution-framework bundle provides it" if not node else "")
    edge = _edge_path()
    add("microsoft edge (browser automations)", PASS if edge else WARN, edge or "not found",
        "browser automations drive the installed Edge using your signed-in profile" if not edge else "")

    home_ok = paths.root.exists() and os.access(paths.root, os.W_OK)
    add("install folder", PASS if home_ok else WARN, str(paths.root), "run the installer for an automation" if not home_ok else "")
    try:
        cfg = platform_config(paths)
    except ValueError as exc:
        add("platform.json", FAIL, str(exc), "re-run the installer")
        return res
    add("ring", PASS, cfg["ring"])

    inst = selfheal.read_installed(paths) if paths.root.exists() else {"automations": {}, "components": {}}
    autos = inst.get("automations") or {}
    add("installed automations", PASS if autos else WARN, ", ".join(f"{n} {v}" for n, v in sorted(autos.items())) or "none",
        "download an installer from the install page" if not autos else "")

    # schedule: tasks present for every installed automation (Windows only)
    if sys.platform == "win32" and autos:
        for name in sorted(autos):
            try:
                out = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=30).stdout
            except (OSError, subprocess.SubprocessError):
                out = ""
            has = f"AUTOMAGIX\\{name}-" in out
            add(f"scheduled task: {name}", PASS if has else WARN, "registered" if has else "no task found",
                "run:  python -m wrapper_core maintain   (it re-creates missing tasks), or reinstall" if not has else "")
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "AUTOMAGIX-maintain.cmd"
    if sys.platform == "win32":
        add("login maintenance script", PASS if startup.exists() else WARN, str(startup) if startup.exists() else "missing",
            "reinstall: it keeps tasks alive across VDI resets" if not startup.exists() else "")

    for name in sorted(autos):
        last = read_json(paths.last_run_file(name), None)
        if not last:
            add(f"last run: {name}", WARN, "has not run yet", "run it now:  python -m wrapper_core run " + name)
        else:
            ok = last.get("status") in ("success", "partial", "skipped_maintenance", "halted_paused")
            add(f"last run: {name}", PASS if ok else FAIL, f"{last.get('status')} at {last.get('ended_at')}",
                "" if ok else f"see {paths.logs / 'wrapper.log'} and the run folder under {paths.runs}")
        if paths.status_file(name).exists():
            add(f"paused notice: {name}", WARN, paths.status_file(name).read_text(encoding="utf-8").strip().splitlines()[-1],
                "an operator paused this automation; it resumes automatically when un-paused")

    # registry + logging
    reg = cfg.get("registry_url")
    if not reg:
        add("registry", WARN, "not configured", "updates and the kill-switch need registry_url in config/platform.json")
    elif online:
        try:
            selfheal.fetch_bytes(selfheal.resolve_location("manifest.yaml", reg), None, retries=1, timeout_s=15)
            add("registry", PASS, f"{reg} reachable")
        except selfheal.HealError as exc:
            cached = paths.manifest_cache.exists()
            add("registry", WARN, f"{reg} unreachable ({str(exc)[:120]})",
                "the automation keeps running from the last cached manifest" if cached else
                "first-time installs run from the payload bundled in the installer; updates/kill-switch need the registry")
    transport = cfg.get("log_transport")
    remote = cfg.get("log_remote", "")
    add("run-log destination", PASS if remote else WARN, f"{transport}: {remote or 'not configured'}",
        "set log_remote in config/platform.json (or reinstall with a configured installer)" if not remote else "")
    spooled = len(list(paths.spool.glob("*.json"))) if paths.spool.exists() else 0
    add("unsent log spool", PASS if spooled == 0 else WARN, f"{spooled} item(s)",
        "logs could not be sent; they retry on the next run. Check connectivity/keys." if spooled else "")
    if transport == "github-api":
        tok = paths.secrets / "logging_token.dpapi"
        add("logging token (DPAPI)", PASS if tok.exists() else FAIL, "present" if tok.exists() else "missing",
            "this installer was built without live logging; use the connected installer from the install page" if not tok.exists() else "")
    if transport == "git-ssh":
        key = paths.secrets / "logging_key.dpapi"
        add("logging key (DPAPI)", PASS if key.exists() else FAIL, "present" if key.exists() else "missing",
            "reinstall with the configured installer" if not key.exists() else "")
    return res


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="wrapper_core doctor")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", action="store_true", help="skip network checks")
    a = ap.parse_args(argv)
    results = run_checks(online=not a.offline)
    if a.json:
        print(json.dumps(results, indent=2))
    else:
        width = max(len(r["check"]) for r in results)
        for r in results:
            print(f"[{r['status']}] {r['check']:<{width}}  {r['detail']}")
            if r.get("fix") and r["status"] != PASS:
                print(f"       -> {r['fix']}")
        bad = sum(1 for r in results if r["status"] == FAIL)
        warn = sum(1 for r in results if r["status"] == WARN)
        print(f"\n{len(results) - bad - warn} ok, {warn} warning(s), {bad} failure(s)")
    return 1 if any(r["status"] == FAIL for r in results) else 0
