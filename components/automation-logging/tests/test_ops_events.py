from __future__ import annotations

from wrapper_core.transport import DirTransport

from automation_logging import analytics, ingest, validate
from factories import T, lifecycle_record, run_record

OPS_EVENTS = [
    {"ts": "2026-10-14T09:00:00Z", "event": "queue_stats", "data": {"queue": "invoices", "pending": 3, "dead_letter": 1, "sla_breached": 0}},
    {"ts": "2026-10-14T09:00:01Z", "event": "approval_requested", "data": {"task_id": "t1", "title": "Pay invoice 17", "sla_s": 600}},
    {"ts": "2026-10-14T09:00:02Z", "event": "approval_decided", "data": {"task_id": "t1", "decision": "approved", "decided_by": "boss"}},
]


def test_run_with_queue_and_approval_events_validates_ingests_and_loads(tmp_path):
    r = run_record("auto-a", T(0, 9), host="VDI-1")
    r["events"] = list(r.get("events") or []) + OPS_EVENTS
    assert validate.validate_record(r, "run") is None
    life = lifecycle_record("approval_requested", "auto-a", "VDI-1", T(0, 9, 3))
    life["detail"] = {"task_id": "t2", "title": "x"}
    assert validate.validate_record(life, "lifecycle") is None
    src, arch = tmp_path / "logs", tmp_path / "archive"
    t = DirTransport(src, "pilot")
    t.send("run", "auto-a", r["run_id"], r)
    t.send("lifecycle", "auto-a", "evt-ops", life)
    stats = ingest.ingest_dir(src, arch)
    assert stats["archived"] == 2 and not stats["errors"]
    data = analytics.load_archive(arch, T(0, 12))
    assert len(data.runs) == 1 and data.skipped_malformed == 0
    assert [e["event"] for e in data.runs[0]["events"] if e["event"] in ("queue_stats", "approval_requested", "approval_decided")] == \
        ["queue_stats", "approval_requested", "approval_decided"]


def test_malformed_ops_events_are_tolerated_or_rejected_cleanly_never_crash(tmp_path):
    r = run_record("auto-a", T(0, 9), host="VDI-1")
    r["events"] = [{"ts": "2026-10-14T09:00:00Z", "event": "queue_stats", "data": {"pending": "lots", "dead_letter": {"x": 1}}}]
    assert validate.validate_record(r, "run") is None          # field-level garbage is the consumer's job to clamp (site_ops does)
    r["events"] = [{"ts": "2026-10-14T09:00:00Z", "event": "approval_requested", "data": "not-an-object"}]
    assert validate.validate_record(r, "run") is not None       # structurally broken events are rejected like any other event
    data = analytics.Data([run_record("auto-a", T(0, 9))], [], [], [])
    analytics.compute(data, {}, T(0, 12), None, {})             # analytics ignores event kinds it does not know
