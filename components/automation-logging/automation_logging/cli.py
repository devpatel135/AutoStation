"""``python -m automation_logging <command>``"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .common import parse_iso, utcnow


def _now(arg: str | None) -> datetime:
    return parse_iso(arg) if arg else utcnow()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="automation_logging", description="AUTOSTATION log ingestion, analytics, alerting, dashboard")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", help="archive endpoint artefacts")
    i.add_argument("--source", choices=["git", "dir"], required=True)
    i.add_argument("--remote", help="logging repo URL/path (git)")
    i.add_argument("--from", dest="src", help="DirTransport root (dir)")
    i.add_argument("--out", required=True, help="archive directory")
    i.add_argument("--keep-refs", action="store_true", help="git: do not delete processed refs")

    def common(sp, alerts=True):
        sp.add_argument("--archive", required=True)
        sp.add_argument("--manifest", default=None)
        sp.add_argument("--state", default="state")
        sp.add_argument("--now", default=None, help="ISO timestamp (testing)")

    a = sub.add_parser("analyze", help="compute report + signals JSON")
    common(a)
    a.add_argument("--out", required=True)

    d = sub.add_parser("diagnose", help="LLM-diagnose signals in an analyze output (signals.json) ")
    common(d)
    d.add_argument("--signals", required=True)
    d.add_argument("--max-calls", type=int, default=20)

    al = sub.add_parser("alert", help="send alerts for a signals.json")
    al.add_argument("--signals", required=True)
    al.add_argument("--config", required=True)
    al.add_argument("--state", default="state")
    al.add_argument("--archive", default=None)
    al.add_argument("--dry-run", action="store_true")
    al.add_argument("--now", default=None)

    t = sub.add_parser("triggers", help="compute triggers.json")
    common(t)
    t.add_argument("--out", required=True)
    t.add_argument("--requests", default=None)

    b = sub.add_parser("build", help="run the whole pipeline")
    common(b)
    b.add_argument("--out", default="public")
    b.add_argument("--alert-config", default=None)
    b.add_argument("--dry-run-alerts", action="store_true")
    b.add_argument("--requests", default=None)
    b.add_argument("--max-llm-calls", type=int, default=20)
    b.add_argument("--log-base-url", default=None)
    b.add_argument("--program-config", default=None, help="program.yaml (hourly cost, UiPath licence inputs) for the programme dashboard")

    pg = sub.add_parser("program", help="render only the programme/ROI dashboard from an archive")
    common(pg)
    pg.add_argument("--out", required=True)
    pg.add_argument("--program-config", default=None)

    dsh = sub.add_parser("dashboard", help="render the static site from a report.json")
    dsh.add_argument("--report", required=True)
    dsh.add_argument("--archive", required=True)
    dsh.add_argument("--out", default="public")
    dsh.add_argument("--now", default=None)

    pr = sub.add_parser("prune", help="delete archive day-folders older than N days")
    pr.add_argument("--archive", required=True)
    pr.add_argument("--days", type=int, default=90)
    pr.add_argument("--now", default=None)

    s = sub.add_parser("sample", help="write a synthetic fleet history")
    s.add_argument("--out", required=True)
    s.add_argument("--now", default=None)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--days", type=int, default=14)

    args = p.parse_args(argv)

    if args.cmd == "ingest":
        from . import ingest
        stats = ingest.ingest(args.source, args.out, remote=args.remote, src_dir=args.src, **({"delete": not args.keep_refs} if args.source == "git" else {}))
        print(json.dumps(stats, indent=2))
        return 1 if stats["errors"] else 0

    if args.cmd == "prune":
        from . import ingest
        print(json.dumps(ingest.prune(args.archive, args.days, _now(args.now))))
        return 0

    if args.cmd == "sample":
        from . import sample
        print(json.dumps(sample.write(args.out, _now(args.now) if args.now else None, args.seed, args.days)))
        return 0

    # everything below may call the LLM through wrapper_core; keep its housekeeping out of the user's profile
    os.environ.setdefault("AUTOMAGIX_HOME", str(Path(getattr(args, "state", "state")).resolve() / ".wrapper-home"))

    from . import alerting, analytics, diagnose, pipeline, triggers
    from .common import read_json, write_json
    now = _now(getattr(args, "now", None))

    if args.cmd == "analyze":
        manifest = pipeline.load_manifest(args.manifest)
        data = analytics.load_archive(args.archive, now)
        state_path = Path(args.state) / "analytics.json"
        report, st = analytics.compute(data, manifest, now, None, read_json(state_path, {}))
        write_json(state_path, st)
        write_json(args.out, report)
        print(json.dumps({"signals": len(report["signals"]), "fleet": report["fleet"]["status"]}))
        return 0
    if args.cmd == "diagnose":
        manifest = pipeline.load_manifest(args.manifest)
        data = analytics.load_archive(args.archive, now)
        doc = read_json(args.signals, {})
        sigs, stats = diagnose.diagnose_signals(doc["signals"], data, manifest, Path(args.state) / "diagnoses.json", args.max_calls)
        doc["signals"] = sigs
        write_json(args.signals, doc)
        print(json.dumps(stats))
        return 0
    if args.cmd == "alert":
        doc = read_json(args.signals, {})
        if args.archive:
            alerting.enrich(doc["signals"], analytics.load_archive(args.archive, now))
        res = alerting.run(doc["signals"], args.config, Path(args.state) / "alerts.json", now, args.dry_run)
        print(json.dumps(res, indent=2, default=str))
        return 1 if res["errors"] else 0
    if args.cmd == "dashboard":
        from . import dashboard
        report = read_json(args.report, {})
        data = analytics.load_archive(args.archive, now)
        print(json.dumps(dashboard.build_site(report, data, args.out)))
        return 0
    if args.cmd == "triggers":
        manifest = pipeline.load_manifest(args.manifest)
        data = analytics.load_archive(args.archive, now)
        print(json.dumps(triggers.run(data, manifest, now, args.out, args.requests)))
        return 0
    if args.cmd == "program":
        from . import program
        manifest = pipeline.load_manifest(args.manifest)
        data = analytics.load_archive(args.archive, now)
        report, _ = analytics.compute(data, manifest, now, None, read_json(Path(args.state) / "analytics.json", {}))
        print(json.dumps(program.build(report, data, manifest, args.out, program.load_config(args.program_config), now)["program"]))
        return 0
    if args.cmd == "build":
        summary = pipeline.build(args.archive, args.manifest, args.state, args.out, now, args.alert_config, args.dry_run_alerts,
                                 args.requests, args.max_llm_calls, log_base_url=args.log_base_url,
                                 program_config=args.program_config)
        print(json.dumps({k: v for k, v in summary.items() if k != "stages"} | {"stages": {k: (v if not isinstance(v, dict) else {a: b for a, b in v.items() if a != "detail"}) for k, v in summary["stages"].items()}}, indent=2, default=str))
        return 1 if summary["errors"] else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
