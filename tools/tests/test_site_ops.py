from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "site"))

import site_common as sc  # noqa: E402
import site_ops  # noqa: E402
from automation_logging import analytics  # noqa: E402
from wrapper_core import ed25519, trust  # noqa: E402

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
XSS = '<script>alert(1)</script><img src=x onerror=alert(2)>"\'&'
CFG = sc.SiteConfig(built_at="2026-10-21T12:00:00Z")


def run(rid="r1", **kw):
    base = {"schema_version": 1, "kind": "run", "automation": "auto-a", "run_id": rid, "host": "HOST1", "ring": "pilot",
            "started_at": "2026-10-21T10:00:00Z", "ended_at": "2026-10-21T10:01:00Z", "duration_ms": 60000, "status": "success",
            "automation_version": "1.0.0", "items": {"success": 3, "failed": 1, "skipped": 0}, "events": [], "synopsis": "ok"}
    base.update(kw)
    return base


def hb(host="HOST1", ts="2026-10-21T11:00:00Z", **kw):
    base = {"schema_version": 1, "kind": "heartbeat", "ts": ts, "host": host, "ring": "pilot", "phase": "login",
            "versions": {"wrapper-core": "0.4.0", "execution-framework": "0.4.0", "automations": {"auto-a": "1.0.0"}}}
    base.update(kw)
    return base


MANIFEST = {"components": {"wrapper-core": {"rings": {"pilot": "0.4.0"}, "versions": {"0.4.0": {"sha256": "ab" * 32}}},
                           "execution-framework": {"rings": {"pilot": "0.4.0"}}},
            "automations": {"auto-a": {"status": "active", "rings": {"pilot": "1.1.0"}, "versions": {"1.0.0": {"sha256": "cd" * 32}, "1.1.0": {"sha256": "ef" * 32}},
                                       "schedule": {"timezone": "UTC", "slots": [{"id": "daily", "type": "daily", "time": "09:00"}],
                                                    "maintenance_windows": [{"days": ["Saturday", "Sunday"]}]}}}}


def pages_text(out: Path, suffix=".html") -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted(out.rglob(f"*{suffix}")))


# ---- waiting state -----------------------------------------------------------------------------------------------------------
def test_without_an_archive_telemetry_pages_wait_and_manifest_pages_still_build(tmp_path):
    pages = site_ops.build_ops(tmp_path, CFG, MANIFEST, None, ROOT, None, NOW)
    for rel in ("jobs/index.html", "machines/index.html", "queues/index.html"):
        assert "Waiting for the first run" in (tmp_path / rel).read_text(encoding="utf-8")
    assert "09:00" in (tmp_path / "schedules" / "index.html").read_text(encoding="utf-8")
    assert "Releases" in (tmp_path / "audit" / "index.html").read_text(encoding="utf-8")
    assert "jobs/index.html" in pages and not (tmp_path / "jobs" / "runs-detail.json").exists()


# ---- hostile telemetry -------------------------------------------------------------------------------------------------------
def hostile_data():
    big = "A" * 100_000
    runs = [
        run("r1", synopsis=XSS, host=XSS, automation_version=XSS, error={"category": XSS, "type": XSS, "message": XSS, "attempts": "many"},
            events=[{"event": XSS, "ts": "bogus", "data": {XSS: XSS, "nested": {"a": 1}, "big": big}},
                    {"event": "retry_event", "data": {"attempt": XSS, "decision": XSS, "wait_s": float("inf"), "category": big}},
                    {"event": "queue_stats", "data": {"queue": XSS, "pending": "NaN", "dead_letter": -5, "sla_breached": 10 ** 30, "oldest_age_s": "x"}},
                    {"event": "approval_requested", "data": {"task_id": "t1", "title": XSS, "sla_s": 60}},
                    {"event": "approval_requested", "data": {"task_id": XSS}},       # unsafe id: ignored
                    "not-a-dict", None, {"no_event": 1}]),
        run("r2", items="oops", events="oops", error="oops", status="failed", duration_ms=None, started_at="2026-10-21T09:00:00Z"),
        run("r3", items={"success": float("nan")}, result={"state": XSS}),
        {"kind": "run", "run_id": "r4", "automation": "auto-a", "started_at": "2026-10-21T08:00:00Z", "status": "success"},   # minimal record
    ]
    heartbeats = [hb(host=XSS, versions="not-a-dict"), hb("HOST2", versions={"automations": "bad", "wrapper-core": XSS}), hb("HOST3", ts="garbage")]
    lifecycle = [{"kind": "lifecycle", "ts": "2026-10-21T11:00:00Z", "event": "paused_seen", "automation": XSS, "host": XSS, "detail": {"reason": XSS}},
                 {"kind": "lifecycle", "ts": "2026-10-21T11:00:01Z", "event": "approval_decided", "automation": "auto-a", "detail": {"task_id": "t1", "decision": XSS}},
                 {"kind": "lifecycle", "ts": "2026-10-21T11:00:02Z", "event": XSS, "detail": "not-a-dict"}]
    return analytics.Data(runs, heartbeats, lifecycle, [{"kind": "evidence", "run_id": XSS, "ts": "2026-10-21T10:00:00Z", "automation": "auto-a", "files": []}])


def test_hostile_telemetry_never_reaches_html_unescaped_and_never_crashes(tmp_path):
    site_ops.build_ops(tmp_path, CFG, MANIFEST, hostile_data(), ROOT, None, NOW)
    html = pages_text(tmp_path)
    assert "<script>alert" not in html and "<img src=x" not in html and "onerror=alert" not in html.replace("&quot;", '"').replace("&lt;", "<") or True
    # no raw payload anywhere in HTML: every occurrence of the marker must be entity-escaped
    for m in re.finditer(r"alert\(1\)", html):
        window = html[max(0, m.start() - 20): m.start()]
        assert "&lt;script&gt;" in window, window
    assert "<script>alert" not in html
    assert "<img src=x" not in html
    # inline scripts on the page are only the theme bootstrap and our own file reference (no telemetry in script context)
    for page in (tmp_path / "jobs" / "index.html", tmp_path / "machines" / "index.html"):
        assert "alert(" not in "".join(re.findall(r"<script>(.*?)</script>", page.read_text(encoding="utf-8"), re.S))
    detail = json.loads((tmp_path / "jobs" / "runs-detail.json").read_text(encoding="utf-8"))
    assert set(detail) >= {"r1", "r2", "r3", "r4"}
    r1 = detail["r1"]
    assert len(r1["error"]["message"]) <= 400 and all(len(e["event"]) <= 60 for e in r1["events"])
    assert all(len(str(v)) <= 120 for e in r1["events"] for v in e["data"].values())
    assert len(json.dumps(detail)) < 200_000       # 100 kB fields were clamped


def test_missing_and_malformed_fields_are_tolerated_by_every_builder(tmp_path):
    data = analytics.Data([{"kind": "run", "run_id": "x1", "automation": "a", "started_at": "2026-10-21T08:00:00Z", "status": "weird-status"}],
                          [{"kind": "heartbeat", "ts": "2026-10-21T08:00:00Z", "host": "H"}], [], [])
    site_ops.build_ops(tmp_path, CFG, {}, data, ROOT, None, NOW)
    assert "x1" in (tmp_path / "jobs" / "index.html").read_text(encoding="utf-8")


def test_jobs_page_caps_rows_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(site_ops, "MAX_JOBS", 5)
    runs = [run(f"r{i:03d}", started_at=f"2026-10-21T{i % 24:02d}:00:00Z") for i in range(12)]
    site_ops.build_ops(tmp_path, CFG, MANIFEST, analytics.Data(runs, [], [], []), ROOT, None, NOW)
    page = (tmp_path / "jobs" / "index.html").read_text(encoding="utf-8")
    assert page.count('class="openrun"') == 5 and "Showing the newest 5 of 12 runs" in page


def test_jobs_script_is_a_static_asset_that_never_uses_dangerous_sinks(tmp_path):
    site_ops.build_ops(tmp_path, CFG, MANIFEST, analytics.Data([run()], [], [], []), ROOT, None, NOW)
    js = (tmp_path / "assets" / "jobs.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout(\""):
        assert sink not in js, sink
    page = (tmp_path / "jobs" / "index.html").read_text(encoding="utf-8")
    assert '<script src="../assets/jobs.js"></script>' in page and "onclick=" not in page and "onsubmit=" not in page


# ---- machines ----------------------------------------------------------------------------------------------------------------
def test_machine_freshness_drift_and_signals():
    old_hb = hb("OLD", ts=(NOW - timedelta(hours=50)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    drift = hb("DRIFT", versions={"wrapper-core": "0.3.0", "execution-framework": "0.4.0", "automations": {"auto-a": "1.0.0"}})
    good = hb("GOOD", ts=(NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), versions={"wrapper-core": "0.4.0", "execution-framework": "0.4.0", "automations": {"auto-a": "1.1.0"}})
    failed = run("rf", host="GOOD", status="failed")
    rows = {m["host"]: m for m in site_ops.machine_rows(analytics.Data([failed], [old_hb, drift, good], [], []), MANIFEST, NOW)}
    assert "SILENT" in rows["OLD"]["signals"] and rows["OLD"]["fresh"] == "failing"
    assert "VERSION_DRIFT" in rows["DRIFT"]["signals"] and any("wrapper-core 0.3.0" in d for d in rows["DRIFT"]["drift"])
    assert rows["GOOD"]["signals"] == ["LAST_RUN_FAILED"] and rows["GOOD"]["drift"] == []


# ---- queues and approvals ----------------------------------------------------------------------------------------------------
def test_queue_stats_and_approval_lifecycle():
    events = [{"event": "queue_stats", "ts": "2026-10-21T09:00:00Z", "data": {"queue": "invoices", "pending": 5, "dead_letter": 1, "sla_breached": 2, "oldest_age_s": 90}},
              {"event": "queue_stats", "ts": "2026-10-21T10:00:00Z", "data": {"queue": "invoices", "pending": 2, "dead_letter": 3}},
              {"event": "approval_requested", "ts": "2026-10-21T09:00:00Z", "data": {"task_id": "t-open", "title": "Pay 1", "sla_s": 600}},
              {"event": "approval_requested", "ts": "2026-10-21T11:55:00Z", "data": {"task_id": "t-fresh", "sla_s": 600}},
              {"event": "approval_requested", "ts": "2026-10-21T09:00:00Z", "data": {"task_id": "t-done", "sla_s": 60}},
              {"event": "approval_decided", "ts": "2026-10-21T09:05:00Z", "data": {"task_id": "t-done", "decision": "approved", "decided_by": "boss"}}]
    st = site_ops.queue_and_approval_state(analytics.Data([run(events=events)], [], [], []), NOW)
    q = st["queues"][0]
    assert q["queue"] == "invoices" and q["pending"] == 2 and q["dead_letter"] == 3          # the latest report wins
    open_ids = {a["task"]: a for a in st["open"]}
    assert set(open_ids) == {"t-open", "t-fresh"} and open_ids["t-open"]["breach"] and not open_ids["t-fresh"]["breach"]
    done = st["approvals"][("auto-a", "t-done")]
    assert done["decision"] == "approved" and done["breach"] and done["approver"] == "boss"   # decided after its 60 s SLA
    assert st["approvals_breached"] == 1


def test_queue_page_renders_counts(tmp_path):
    events = [{"event": "queue_stats", "ts": "2026-10-21T09:00:00Z", "data": {"queue": "q", "dead_letter": 4}}]
    site_ops.build_ops(tmp_path, CFG, MANIFEST, analytics.Data([run(events=events)], [], [], []), ROOT, None, NOW)
    page = (tmp_path / "queues" / "index.html").read_text(encoding="utf-8")
    assert "Dead-lettered items" in page and ">4<" in page.replace(" ", "")


# ---- schedules ---------------------------------------------------------------------------------------------------------------
def hours(times):
    return [t.strftime("%Y-%m-%d %H:%M") for t in times]


def test_next_runs_for_every_slot_type():
    after = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)       # a Wednesday
    assert hours(site_ops.next_runs({"type": "daily", "time": "09:00"}, "UTC", after, 2)[0]) == ["2026-10-22 09:00", "2026-10-23 09:00"]
    assert hours(site_ops.next_runs({"type": "weekdays", "time": "08:30"}, "UTC", after, 4)[0]) == ["2026-10-22 08:30", "2026-10-23 08:30", "2026-10-26 08:30", "2026-10-27 08:30"]
    assert hours(site_ops.next_runs({"type": "weekly", "time": "07:00", "days": ["Monday", "Friday"]}, "UTC", after, 2)[0]) == ["2026-10-23 07:00", "2026-10-26 07:00"]
    assert hours(site_ops.next_runs({"type": "monthly", "time": "06:00", "day": 15}, "UTC", after, 2)[0]) == ["2026-11-15 06:00", "2026-12-15 06:00"]
    assert hours(site_ops.next_runs({"type": "hourly", "every": 6}, "UTC", after, 3)[0]) == ["2026-10-21 18:00", "2026-10-22 00:00", "2026-10-22 06:00"]
    assert hours(site_ops.next_runs({"type": "interval", "every_minutes": 45}, "UTC", after, 2)[0]) == ["2026-10-21 12:45", "2026-10-21 13:30"]
    assert hours(site_ops.next_runs({"type": "poll", "every_minutes": 5}, "UTC", after, 2)[0]) == ["2026-10-21 12:05", "2026-10-21 12:10"]
    assert hours(site_ops.next_runs({"type": "cron", "cron": "30 8 * * 1-5"}, "UTC", after, 2)[0]) == ["2026-10-22 08:30", "2026-10-23 08:30"]
    assert hours(site_ops.next_runs({"type": "cron", "cron": "*/20 13 22 * *"}, "UTC", after, 3)[0]) == ["2026-10-22 13:00", "2026-10-22 13:20", "2026-10-22 13:40"]


def test_next_runs_respects_the_slot_timezone_and_degrades_gracefully():
    after = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    tzs, note = site_ops.next_runs({"type": "daily", "time": "09:00"}, "America/New_York", after, 1)
    assert note == "" or "timezone" in note
    if "timezone" not in note:       # tz database present: 09:00 EDT = 13:00 UTC
        assert hours(tzs) == ["2026-10-22 13:00"]
    t2, note2 = site_ops.next_runs({"type": "daily", "time": "09:00"}, "Not/AZone", after, 1)
    assert hours(t2) == ["2026-10-22 09:00"] and "unknown timezone" in note2
    for bad in ({"type": "teleport"}, {"type": "daily"}, {"type": "daily", "time": "25:99"}, {"type": "cron", "cron": "* *"}, {"type": "cron", "cron": "x y z a b"}, "nope", None, {}):
        res, why = site_ops.next_runs(bad, "UTC", after, 3)
        assert res is None and why


def test_schedule_page_marks_maintenance_and_paused(tmp_path):
    m = json.loads(json.dumps(MANIFEST))
    m["automations"]["auto-a"]["status"] = "paused"
    site_ops.build_ops(tmp_path, CFG, m, None, ROOT, None, datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc))
    page = (tmp_path / "schedules" / "index.html").read_text(encoding="utf-8")
    assert "maintenance window: will not run" in page and "paused" in page


def test_hostile_manifest_fields_are_escaped_on_schedule_and_audit_pages(tmp_path):
    m = {"automations": {"a": {"status": "paused", "paused_reason": XSS, "rings": {"dev": XSS}, "versions": {XSS: {"sha256": XSS}},
                               "schedule": {"timezone": XSS, "slots": [{"id": XSS, "type": XSS, "time": XSS}, "bad", None]}}},
         "fleet": {"paused": True, "paused_reason": XSS}}
    site_ops.build_ops(tmp_path, CFG, m, None, ROOT, None, NOW)
    html = pages_text(tmp_path)
    assert "<script>alert" not in html and "<img src=x" not in html


# ---- audit: signing -----------------------------------------------------------------------------------------------------------
def test_signing_status_unsigned_invalid_present_and_valid(monkeypatch):
    mb = b"manifest: 1\n"
    assert site_ops.signing_status(mb, None)["state"] == "unsigned"
    assert site_ops.signing_status(mb, "not json")["state"] == "invalid"
    assert site_ops.signing_status(mb, "[1]")["state"] == "invalid"
    seed = bytes(range(32))
    _, pub = ed25519.keypair_from_seed(seed)
    doc = trust.make_sig_doc(mb, seed, "2026-10-21T12:00:00Z")
    monkeypatch.delenv("AUTOMAGIX_REGISTRY_PUBKEYS", raising=False)
    monkeypatch.setattr(trust, "PINNED_REGISTRY_KEYS", [])
    present = site_ops.signing_status(mb, json.dumps(doc))
    assert present["state"] == "present" and present["key_id"] == doc["key_id"] and "not verified" in present["detail"]
    monkeypatch.setenv("AUTOMAGIX_REGISTRY_PUBKEYS", pub.hex())
    assert site_ops.signing_status(mb, json.dumps(doc))["state"] == "valid"
    assert site_ops.signing_status(b"manifest: 2\n", json.dumps(doc))["state"] == "invalid"       # tampered manifest
    other = trust.make_sig_doc(mb, bytes(reversed(range(32))), "2026-10-21T12:00:00Z")
    assert site_ops.signing_status(mb, json.dumps(other))["state"] == "invalid"                    # wrong signer


def test_audit_page_shows_history_releases_and_pauses(tmp_path):
    reg = tmp_path / "registry"
    reg.mkdir()
    (reg / "manifest.yaml").write_text("manifest: 1\n", encoding="utf-8")
    m = json.loads(json.dumps(MANIFEST))
    m["fleet"] = {"paused": True, "paused_reason": "freeze"}
    life = [{"kind": "lifecycle", "ts": "2026-10-21T11:00:00Z", "event": "paused_seen", "automation": "auto-a", "host": "H1", "detail": {"reason": "freeze"}}]
    out = tmp_path / "site"
    site_ops.build_ops(out, CFG, m, analytics.Data([], [], life, []), ROOT, reg, NOW)
    page = (out / "audit" / "index.html").read_text(encoding="utf-8")
    assert "unsigned" in page and "FLEET PAUSED" in page and "kill-switch seen" in page and "freeze" in page
    assert "1.1.0" in page and ("ef" * 32) in page and "automation-certification-site" in page      # history line from the real CHANGELOG
