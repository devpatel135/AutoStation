"""The scheduled CI pipeline: analytics -> diagnose -> alert -> triggers -> dashboard (+ state persistence).

Exit-code contract (see cli): signals never fail the job; *pipeline* problems (an exception in a stage, a broken alert
channel) are collected in ``summary['errors']`` and make the job exit non-zero - but the remaining stages (notably the
dashboard) still run, so a flaky Jira never blanks the portal.
"""
from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path

from . import alerting, analytics, dashboard, diagnose, program, triggers
from .common import iso, read_json, utcnow, wc_manifest, write_json


def load_manifest(path) -> dict:
    if path and Path(path).exists():
        return wc_manifest.load(path)
    return {}


def build(archive, manifest_path, state_dir, out, now: datetime | None = None, alert_config=None,
          dry_run_alerts: bool = False, requests_dir=None, max_llm_calls: int = 20, config: dict | None = None,
          alert_ctx: alerting.Context | None = None, log_base_url: str | None = None, llm_call=None,
          program_config=None) -> dict:
    now = now or utcnow()
    state_dir, out = Path(state_dir), Path(out)
    state_dir.mkdir(parents=True, exist_ok=True)
    summary: dict = {"generated_at": iso(now), "errors": [], "stages": {}}

    def stage(name, fn):
        try:
            summary["stages"][name] = fn()
        except Exception as exc:  # noqa: BLE001 - keep going so the dashboard is still published
            summary["errors"].append({"stage": name, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-800:]})
            summary["stages"][name] = None
            return None
        return summary["stages"][name]

    manifest: dict = {}
    data = analytics.Data()
    report: dict = {}
    box: dict = {}

    def s_load():
        nonlocal manifest, data
        manifest = load_manifest(manifest_path)
        data = analytics.load_archive(archive, now, (config or {}).get("window_days", analytics.DEFAULTS["window_days"]))
        return {"runs": len(data.runs), "heartbeats": len(data.heartbeats), "lifecycle": len(data.lifecycle),
                "manifest": bool(manifest)}

    def s_analyze():
        nonlocal report
        state = read_json(state_dir / "analytics.json", {}) or {}
        report, new_state = analytics.compute(data, manifest, now, config, state)
        write_json(state_dir / "analytics.json", new_state)
        return {"signals": len(report["signals"]), "fleet_status": report["fleet"]["status"]}

    stage("load", s_load)
    if summary["stages"].get("load") is None:
        return summary
    if stage("analyze", s_analyze) is None:
        return summary

    def s_diag():
        sigs, stats = diagnose.diagnose_signals(report["signals"], data, manifest, state_dir / "diagnoses.json", max_llm_calls, llm_call)
        return stats

    stage("diagnose", s_diag)

    def s_alert():
        alerting.enrich(report["signals"], data)
        cfg_path = alert_config
        if not cfg_path:
            return {"skipped": "no alert config"}
        res = alerting.run(report["signals"], cfg_path, state_dir / "alerts.json", now, dry_run_alerts, alert_ctx, log_base_url)
        for e in res["errors"]:
            summary["errors"].append({"stage": "alert", "error": e})
        return {k: (len(v) if isinstance(v, list) else v) for k, v in res.items()} | {"detail": res}

    stage("alert", s_alert)

    stage("triggers", lambda: triggers.run(data, manifest, now, Path(out) / "triggers.json", requests_dir))
    stage("dashboard", lambda: dashboard.build_site(report, data, out))
    stage("program", lambda: program.build(report, data, manifest, Path(out) / "program", program.load_config(program_config), now)["program"])
    return summary
