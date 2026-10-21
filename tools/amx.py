#!/usr/bin/env python3
"""amx - the one command for AutoStation developers and operators (a thin dispatcher over the real tools).

    python tools/amx.py new <name> --description ... --owner team@x      scaffold a new automation
    python tools/amx.py dev <payload_dir> [--param k=v]                  run it offline through the real lifecycle
    python tools/amx.py review <file.py>                                 compliance review (AUTOMATION_STANDARD)
    python tools/amx.py test [--only suite ...]                          every test suite
    python tools/amx.py validate [--manifest path]                       validate the registry manifest
    python tools/amx.py promote|rollback|pause|resume|fleet-pause|... -- registry operator commands (a PR on GitHub)
    python tools/amx.py inventory <uipath-dir> --out inv/                scan UiPath projects (migration kit)
    python tools/amx.py migrate-scaffold|parity|migration-dashboard      migration kit
    python tools/amx.py site --out site [--demo]                         build the Pages site
    python tools/amx.py e2e [--out dir]                                  full offline walkthrough
    python tools/amx.py doctor                                           diagnose this machine
    python tools/amx.py status                                           installed automations and last runs
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
C = ROOT / "components"
T = ROOT / "tools"

REGISTRY_OPS = {"promote", "rollback", "pause", "resume", "fleet-pause", "fleet-resume", "decommission", "trigger-add",
                "new-automation", "add-version", "compat", "build-portal"}


def run(argv: list[str], cwd: Path | None = None) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(C / "wrapper-core"), env.get("PYTHONPATH", "")]))
    return subprocess.call([str(a) for a in argv], cwd=str(cwd or ROOT), env=env)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    py = sys.executable
    table = {
        "new": lambda: run([py, T / "new_automation.py", *rest]),
        "dev": lambda: run([py, "-m", "wrapper_core", "dev", *rest]),
        "review": lambda: run([py, "-m", "wrapper_core", "assist", "review", *rest]),
        "test": lambda: run([py, T / "test_all.py", *rest]),
        "validate": lambda: run([py, "-m", "registry", "validate", *rest], C / "automation-registry"),
        "doctor": lambda: run([py, "-m", "wrapper_core", "doctor", *rest]),
        "status": lambda: run([py, "-m", "wrapper_core", "status", *rest]),
        "e2e": lambda: run([py, T / "e2e_demo.py", *rest]),
        "site": lambda: run([py, T / "build_site.py", *rest]),
        "inventory": lambda: run([py, T / "uipath" / "inventory.py", *rest]),
        "migrate-scaffold": lambda: run([py, T / "uipath" / "scaffold.py", *rest]),
        "parity": lambda: run([py, T / "uipath" / "parity.py", *rest]),
        "migration-dashboard": lambda: run([py, T / "uipath" / "dashboard.py", *rest]),
    }
    if cmd in REGISTRY_OPS or cmd == "registry":
        args = rest if cmd == "registry" else [cmd, *rest]
        return run([py, "-m", "registry", *args], C / "automation-registry")
    if cmd not in table:
        print(f"unknown command {cmd!r}\n")
        print(__doc__)
        return 2
    return table[cmd]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
