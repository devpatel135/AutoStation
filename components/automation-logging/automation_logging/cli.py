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
    i.add_argument("--max-refs", type=int, default=2000, help="git: process at most this many refs per run (rest wait for the next run)")
    i.add_argument("--consume", action="store_true", help="dir: delete each source file after it is archived (default: leave --from untouched)")
    i.add_argument("--window-days", type=int, default=60, help="dir: skip files dated older than this many days without reading them")
    i.add_argument("--max-rejected", type=int, default=50, help="exit code 3 when more artefacts than this were rejected")

    def common(sp, alerts=True):
        sp.add_argument("--archive", required=True)
        sp.add_argument("--manifest", default=None)
        sp.add_argument("--state", default="state")
        sp.add_argument("--now", default=None, help="ISO timestamp (testing)")

    a = sub.add_parser("analyze", help="compute report + signals JSON")
    common(a)
    a.add_argument("--out", required=True)
    a.add_argument("--state-in", default=None, help="read the analytics state from this directory instead of --state (e.g. a restored cache)")
    a.add_argument("--state-out", default=None, help="write the analytics state to this directory instead of --state")
    a.add_argument("--fail-on-data-quality", action="store_true", help="exit 3 when the archive is incomplete (ingest capped/errors) or nothing loaded")

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

    au = sub.add_parser("audit", help="query archive for LLM classification decisions (Pattern 1/2/3 audit trail)")
    au.add_argument("--archive", required=True)
    au.add_argument("--automation", default=None, help="filter to one automation")
    au.add_argument("--days", type=int, default=30, help="how many days of archive to scan (default 30)")
    au.add_argument("--min-confidence", type=float, default=None, metavar="0.0-1.0", help="only show decisions at or above this confidence")
    au.add_argument("--format", dest="fmt", choices=["json", "table"], default="json")
    au.add_argument("--out", default=None, help="write output to file instead of stdout")
    au.add_argument("--now", default=None, help="ISO timestamp (testing)")

    args = p.parse_args(argv)

    if args.cmd == "audit":
        from . import audit as _audit
        result = _audit.run(args.archive, automation=args.automation, days=args.days,
                            min_confidence=args.min_confidence, now=_now(args.now))
        text = _audit.format_table(result) if args.fmt == "table" else json.dumps(result, indent=2)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"audit: {result['total']} decision(s) written to {args.out}")
        else:
            sys.stdout.write(text)
        return 0

    if args.cmd == "ingest":
        from . import ingest
        extra = ({"delete": not args.keep_refs, "max_refs": args.max_refs} if args.source == "git"
                 else {"consume": args.consume, "window_days": args.window_days})
        stats = ingest.ingest(args.source, args.out, remote=args.remote, src_dir=args.src, **extra)
        problems = ingest.data_quality_problems(stats, args.max_rejected)
        print(json.dumps(dict(stats, data_quality_problems=problems), indent=2))
        if stats["errors"]:
            return 1
        return 3 if problems else 0

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
        state_in = Path(args.state_in or args.state) / "analytics.json"
        state_path = Path(args.state_out or args.state) / "analytics.json"
        report, st = analytics.compute(data, manifest, now, None, read_json(state_in, {}))
        write_json(state_path, st)
        write_json(args.out, report)
        print(json.dumps({"signals": len(report["signals"]), "fleet": report["fleet"]["status"]}))
        dq = report.get("data_quality") or {}
        if args.fail_on_data_quality and (dq.get("ingest_incomplete") or not dq.get("records_loaded")):
            print("analyze: data quality check failed (incomplete ingest or nothing loaded)", file=sys.stderr)
            return 3
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
