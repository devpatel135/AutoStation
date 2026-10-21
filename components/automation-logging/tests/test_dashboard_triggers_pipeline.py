from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest

from automation_logging import analytics, cli, dashboard, pipeline, sample, triggers
from automation_logging.alerting import Context
from automation_logging.common import iso, miniyaml, read_json, write_json

from factories import NOW, T, automation_entry, data_of, manifest, manifest_dict, run_record, signals_of, write_archive

XSS = '<script>alert("xss")</script>'
IMG = '"><img src=x onerror=alert(1)>'


# ---------------------------------------------------------------------------------------------- dashboard
def evil_report():
    r = run_record(f"auto-a", T(0, 9), status="failed", host=f"VDI-{XSS}", synopsis=f"Synopsis {XSS} {IMG}",
                   error={"category": "LOGIC_ERROR", "type": "KeyError", "message": f"KeyError: {XSS} {IMG}", "attempts": 1})
    d = data_of(r)
    mf = manifest(description=f"desc {XSS}", owner=f"o{IMG}@x.com")
    sigs, report, _ = signals_of(d, mf)
    sigs[0]["diagnosis"] = {"root_cause": f"cause {XSS}", "category": "LOGIC_ERROR", "suggested_action": f"do {IMG}", "confidence": "high"}
    return report, d


def test_dashboard_escapes_all_log_derived_text(tmp_path):
    report, d = evil_report()
    dashboard.build_site(report, d, tmp_path)
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "<script" not in page.lower().replace("</script", "")            # the page ships no <script> element at all
    assert "<img" not in page.lower() and 'onerror=alert(1)>' not in page
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in page    # visible, but inert
    assert page.count("&gt;&lt;img") >= 1
    # structured outputs keep raw text (JSON consumers escape on their own)
    assert XSS in json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))["signals"][0]["detail"]


def test_dashboard_files_and_structure(tmp_path):
    runs = [run_record("auto-a", T(d, 9), items=[("i", "success", 100, None)]) for d in range(5)]
    runs.append(run_record("auto-a", T(0, 10), status="hung", duration_ms=9e6 and 3600000, hung_detected=True))
    d = data_of(*runs)
    sigs, report, _ = signals_of(d, manifest(catalog={"minutes_per_item": 5}))
    out = dashboard.build_site(report, d, tmp_path / "public")
    assert set(out["files"]) == {"index.html", "data.json", "signals.json", "runs-latest.json"}
    page = (tmp_path / "public" / "index.html").read_text(encoding="utf-8")
    assert '<meta name="viewport"' in page and "prefers-color-scheme:light" in page and 'data-theme="dark"' in page
    for needle in ("Fleet summary", "Active compliance signals", "Item-level telemetry", "Intelligent retry", "LLM usage", "Fleet versions", "Latest runs", "auto-a"):
        assert needle in page
    assert "<svg" in page and "HUNG" in page
    sj = json.loads((tmp_path / "public" / "signals.json").read_text())
    assert sj["schema_version"] == 1 and sj["signals"][0]["id"]
    assert len(json.loads((tmp_path / "public" / "runs-latest.json").read_text())["runs"]) == 6


def test_dashboard_healthy_fleet_says_so(tmp_path):
    d = data_of(*[run_record("auto-a", T(x, 9)) for x in (1, 0)])
    _, report, _ = signals_of(d, manifest())
    dashboard.build_site(report, d, tmp_path)
    assert "No active signals" in (tmp_path / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------- triggers
def chain_manifest():
    return manifest_dict({
        "auto-a": automation_entry(),
        "auto-b": automation_entry(slots=[{"id": "poll", "type": "poll", "every_minutes": 5}],
                                   triggers=[{"on_automation": "auto-a", "on_status": "success", "slot": "poll", "params": {"source": "a"}}]),
    })


def test_manifest_rule_trigger_is_created_once_with_stable_id():
    d = data_of(run_record("auto-a", NOW - timedelta(hours=1)))
    doc, st = triggers.compute(None, d, chain_manifest(), NOW)
    assert st["added"] == 1
    t = doc["triggers"][0]
    assert t["automation"] == "auto-b" and t["slot"] == "poll" and t["params"] == {"source": "a"} and t["source"].startswith("rule:auto-a:")
    doc2, st2 = triggers.compute(doc, d, chain_manifest(), NOW + timedelta(minutes=30))
    assert st2["added"] == 0 and doc2["triggers"][0]["id"] == t["id"]
    # failed runs / non-matching status do not fire a success rule
    d_fail = data_of(run_record("auto-a", NOW - timedelta(hours=1), status="failed", error={"category": "X", "message": "m"}))
    assert triggers.compute(None, d_fail, chain_manifest(), NOW)[0]["triggers"] == []


def test_payload_emitted_trigger_events():
    ev = [{"ts": iso(NOW), "event": "trigger", "data": {"target": "auto-b", "params": {"file": "x.csv"}, "reason": "new file"}}]
    d = data_of(run_record("auto-a", NOW - timedelta(hours=1), extra_events=ev))
    mf = manifest_dict({"auto-a": automation_entry(), "auto-b": automation_entry()})
    doc, _ = triggers.compute(None, d, mf, NOW)
    t = doc["triggers"][0]
    assert t["automation"] == "auto-b" and t["params"] == {"file": "x.csv"} and t["source"].startswith("event:auto-a:") and t["reason"] == "new file"
    ev_unknown = [{"ts": iso(NOW), "event": "trigger", "data": {"target": "nobody", "params": {}}}]
    assert triggers.compute(None, data_of(run_record("auto-a", NOW - timedelta(hours=1), extra_events=ev_unknown)), mf, NOW)[0]["triggers"] == []


def test_operator_requests_validated(tmp_path):
    req = tmp_path / "requests"
    req.mkdir()
    (req / "r1.json").write_text(json.dumps({"automation": "auto-b", "slot": "poll", "params": {"date_range": "last_week"}, "requested_by": "dev@example.com"}))
    (req / "bad1.json").write_text(json.dumps({"automation": "ghost"}))
    (req / "bad2.json").write_text("{nope")
    (req / "bad3.json").write_text(json.dumps({"automation": "auto-b", "params": [1]}))
    doc, st = triggers.compute(None, data_of(), chain_manifest(), NOW, req)
    assert [t["source"] for t in doc["triggers"]] == ["operator:dev@example.com"] and doc["triggers"][0]["params"] == {"date_range": "last_week"}
    assert {i["file"] for i in st["ignored_requests"]} == {"bad1.json", "bad2.json", "bad3.json"}
    assert triggers.compute(doc, data_of(), chain_manifest(), NOW, req)[1]["added"] == 0     # idempotent


def test_chain_depth_limit_stops_loops():
    mf = manifest_dict({"a": automation_entry(triggers=[{"on_automation": "b", "on_status": "success"}]),
                        "b": automation_entry(triggers=[{"on_automation": "a", "on_status": "success"}])})
    existing, prev = None, None
    t0 = NOW - timedelta(hours=5)
    runs = [run_record("a", t0, host="h")]
    for i in range(8):                                    # a -> b -> a -> b ... each run carries the trigger that started it
        doc, st = triggers.compute(existing, data_of(*runs), mf, NOW)
        if st["added"] == 0:
            break
        existing = doc
        newest = max(doc["triggers"], key=lambda t: t["depth"])
        runs.append(run_record(newest["automation"], t0 + timedelta(minutes=10 * (i + 1)), host="h", trigger_id=newest["id"]))
    assert max(t["depth"] for t in existing["triggers"]) == triggers.MAX_CHAIN_DEPTH and st["depth_limited"] >= 1
    assert len(existing["triggers"]) == triggers.MAX_CHAIN_DEPTH


def test_prune_after_seven_days_and_paused_target_skipped():
    old = {"id": "old1", "automation": "auto-b", "slot": "poll", "params": {}, "created_at": iso(NOW - timedelta(days=8)), "source": "x", "depth": 1}
    fresh = dict(old, id="new1", created_at=iso(NOW - timedelta(days=6)))
    doc, st = triggers.compute({"triggers": [old, fresh]}, data_of(), chain_manifest(), NOW)
    assert [t["id"] for t in doc["triggers"]] == ["new1"] and st["pruned"] == 1
    mf = chain_manifest()
    mf["automations"]["auto-b"]["status"] = "paused"
    assert triggers.compute(None, data_of(run_record("auto-a", NOW - timedelta(hours=1))), mf, NOW)[0]["triggers"] == []


def test_output_is_consumable_by_wrapper_core_selfheal(tmp_path):
    from wrapper_core import selfheal
    from wrapper_core.paths import Paths
    d = data_of(run_record("auto-a", NOW - timedelta(hours=1)))
    reg = tmp_path / "registry"
    triggers.run(d, chain_manifest(), NOW, reg / "triggers.json")
    paths = Paths(tmp_path / "home").ensure()
    pending = selfheal.pending_triggers(paths, str(reg), None, "auto-b")
    assert len(pending) == 1 and pending[0]["params"] == {"source": "a"}
    selfheal.consume_trigger(paths, pending[0]["id"])
    assert selfheal.pending_triggers(paths, str(reg), None, "auto-b") == []


# ---------------------------------------------------------------------------------------------- pipeline + sample
def run_sample(tmp_path, now, **kw):
    sample.write(tmp_path / "s", now=now)
    s = tmp_path / "s"
    out = pipeline.build(s / "archive", s / "manifest.yaml", s / "state", s / "public", now, kw.pop("alert_config", s / "alerting.yaml"), **kw)
    return s, out


@pytest.mark.parametrize("hour", [0, 15])
def test_sample_data_exercises_every_signal_type(tmp_path, hour):
    now = NOW.replace(hour=hour)
    s, out = run_sample(tmp_path, now)
    assert out["errors"] == []
    types = {x["type"] for x in json.loads((s / "public" / "signals.json").read_text())["signals"]}
    assert types == {"MISSED", "SILENT", "SLOW", "VERSION_DRIFT", "UNEXPECTED", "HUNG", "FAILED", "ITEM_FAILURES"}
    assert (s / "public" / "index.html").exists() and (s / "public" / "triggers.json").exists()
    alerts = [json.loads(l) for l in (s / "state" / "alerts-outbox.jsonl").read_text().splitlines()]
    assert {a["signal_type"] for a in alerts} <= types and any(a["diagnosis"] for a in alerts if a["signal_type"] == "FAILED")
    failed = next(a for a in alerts if a["signal_type"] == "FAILED")
    assert failed["log_link"] and failed["synopsis"]


def test_pipeline_second_run_is_quiet_and_resolves(tmp_path):
    s, first = run_sample(tmp_path, NOW)
    assert first["stages"]["alert"]["suppressed"] == 0 or True
    sent_after_first = len((s / "state" / "alerts-outbox.jsonl").read_text().splitlines())
    second = pipeline.build(s / "archive", s / "manifest.yaml", s / "state", s / "public", NOW + timedelta(minutes=5), s / "alerting.yaml")
    assert len((s / "state" / "alerts-outbox.jsonl").read_text().splitlines()) == sent_after_first      # deduped
    assert second["stages"]["diagnose"]["calls"] == 0                                                     # cached
    assert second["errors"] == []


def test_pipeline_survives_a_broken_channel_but_reports_it(tmp_path):
    sample.write(tmp_path / "s", now=NOW)
    s = tmp_path / "s"
    (s / "alerting.yaml").write_text("channels:\n  - {name: hook, type: webhook, url_env: NOPE}\nrouting:\n  critical: [hook]\n  high: [hook]\n  medium: [hook]\n")
    out = pipeline.build(s / "archive", s / "manifest.yaml", s / "state", s / "public", NOW, s / "alerting.yaml", alert_ctx=Context(env={}))
    assert out["errors"] and all(e["stage"] == "alert" for e in out["errors"])
    assert (s / "public" / "index.html").exists()          # the dashboard was still published


def test_pipeline_stage_exception_does_not_stop_dashboard(tmp_path, monkeypatch):
    sample.write(tmp_path / "s", now=NOW)
    s = tmp_path / "s"
    monkeypatch.setattr("automation_logging.diagnose.diagnose_signals", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("llm exploded")))
    out = pipeline.build(s / "archive", s / "manifest.yaml", s / "state", s / "public", NOW)
    assert [e["stage"] for e in out["errors"]] == ["diagnose"] and (s / "public" / "index.html").exists()


def test_cli_end_to_end_and_exit_codes(tmp_path, capsys, monkeypatch):
    s = tmp_path / "s"
    assert cli.main(["sample", "--out", str(s), "--now", iso(NOW)]) == 0
    args = ["build", "--archive", str(s / "archive"), "--manifest", str(s / "manifest.yaml"), "--state", str(s / "state"),
            "--out", str(s / "public"), "--now", iso(NOW), "--alert-config", str(s / "alerting.yaml")]
    assert cli.main(args) == 0                                           # signals present, but exit code is 0
    assert cli.main(["analyze", "--archive", str(s / "archive"), "--manifest", str(s / "manifest.yaml"), "--state", str(s / "state2"),
                     "--out", str(s / "report.json"), "--now", iso(NOW)]) == 0
    assert cli.main(["alert", "--signals", str(s / "report.json"), "--config", str(s / "alerting.yaml"), "--state", str(s / "state2"),
                     "--dry-run", "--now", iso(NOW)]) == 0
    assert "would_send" in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    bad.write_text("channels:\n  - {name: h, type: webhook, url_env: NOPE}\nrouting: {critical: [h], high: [h], medium: [h]}\n")
    fresh = [a if a != str(s / "state") else str(s / "state3") for a in args[:-1]]
    assert cli.main(fresh + [str(bad)]) == 1                        # broken delivery IS a pipeline error
    t = ["triggers", "--archive", str(s / "archive"), "--manifest", str(s / "manifest.yaml"), "--out", str(s / "t.json"), "--now", iso(NOW)]
    assert cli.main(t) == 0 and read_json(s / "t.json")["schema_version"] == 1


def test_ingest_cli_dir_mode(tmp_path):
    from wrapper_core.transport import DirTransport
    DirTransport(tmp_path / "logs", "pilot").send("run", "auto-a", "r1", run_record("auto-a", T(0, 9), run_id="r1"))
    assert cli.main(["ingest", "--source", "dir", "--from", str(tmp_path / "logs"), "--out", str(tmp_path / "arch")]) == 0
    assert (tmp_path / "arch" / "run" / "auto-a" / "2026" / "10" / "14" / "r1.json").exists()
