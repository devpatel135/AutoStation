"""``python -m wrapper_core <command>``"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "secrets":
        from . import secrets
        return secrets.main(argv[1:])
    if argv and argv[0] == "doctor":
        from . import doctor
        return doctor.main(argv[1:])
    if argv and argv[0] == "trust":
        from . import trust
        return trust.main(argv[1:])
    if argv and argv[0] == "assist":
        from . import assist
        return assist.main(argv[1:])

    p = argparse.ArgumentParser(prog="wrapper_core", description="AUTOSTATION wrapper-core")
    p.add_argument("--version", action="version", version=f"wrapper-core {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run an automation (what Task Scheduler launches)")
    r.add_argument("automation")
    r.add_argument("--slot", default=None)
    r.add_argument("--dry-run", action="store_true", help="execute but do not send logs or touch state")
    r.add_argument("--ignore-maintenance", action="store_true")

    sub.add_parser("maintain", help="re-assert scheduled tasks + heartbeat (runs at login)")

    i = sub.add_parser("install", help="install an automation + register its schedule (called by the installer)")
    i.add_argument("automation")
    u = sub.add_parser("uninstall", help="decommission an automation")
    u.add_argument("automation")
    u.add_argument("--keep-shared", action="store_true")

    sub.add_parser("doctor", help="diagnose this machine/install (read-only, PASS/WARN/FAIL with fixes)")
    sub.add_parser("status", help="show installed components and last runs")
    sub.add_parser("heartbeat", help="send a heartbeat now")
    sub.add_parser("apply-pending", help="swap in a staged wrapper-core")
    sub.add_parser("flush-spool", help="resend spooled log artefacts")

    d = sub.add_parser("dev", help="run a local payload folder with dir transport + mock LLM (no registry)")
    d.add_argument("payload_dir")
    d.add_argument("--param", action="append", default=[], metavar="K=V")
    d.add_argument("--keep", action="store_true", help="keep the temp AUTOMAGIX_HOME")

    sub.add_parser("test-schedule", help="print the Task Scheduler XML for each slot of --manifest")

    args = p.parse_args(argv)

    from .paths import Paths
    if args.cmd == "run":
        from .runner import run_automation
        out = run_automation(args.automation, args.slot, dry_run=args.dry_run, ignore_maintenance=args.ignore_maintenance)
        return out.exit_code
    if args.cmd == "maintain":
        from .lifecycle import maintain
        print(json.dumps(maintain(), indent=2))
        return 0
    if args.cmd == "install":
        from .lifecycle import install_automation
        res = install_automation(args.automation)
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1
    if args.cmd == "uninstall":
        from .lifecycle import uninstall_automation
        print(json.dumps(uninstall_automation(args.automation, purge_shared=not args.keep_shared), indent=2))
        return 0
    if args.cmd == "status":
        from . import selfheal
        from .paths import platform_config
        from .util import read_json
        paths = Paths()
        inst = selfheal.read_installed(paths)
        out = {"home": str(paths.root), "wrapper_core": __version__, "config": platform_config(paths),
               "installed": inst,
               "last_runs": {n: read_json(paths.last_run_file(n)) for n in inst["automations"]},
               "spooled": len(list(paths.spool.glob("*.json"))) if paths.spool.exists() else 0}
        print(json.dumps(out, indent=2, default=str))
        return 0
    if args.cmd == "heartbeat":
        from .heartbeat import send_heartbeat
        return 0 if send_heartbeat(Paths().ensure(), "idle") else 1
    if args.cmd == "apply-pending":
        from . import selfheal
        print(selfheal.apply_pending(Paths()))
        return 0
    if args.cmd == "flush-spool":
        from . import transport
        print(transport.flush_spool(Paths()))
        return 0
    if args.cmd == "dev":
        from .dev import run_dev
        return run_dev(args.payload_dir, dict(kv.split("=", 1) for kv in args.param), args.keep)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
