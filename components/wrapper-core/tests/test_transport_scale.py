"""Telemetry that scales: batching, monthly branches, branch cache, throttling, auth failure, spool caps, record truncation."""
from __future__ import annotations

import base64
import json
import os
import time

import pytest

from test_platform import _fake_github, _gh_env
from wrapper_core import heartbeat, secrets, transport
from wrapper_core.paths import Paths


@pytest.fixture
def gh(monkeypatch):
    state: dict = {}
    srv = _fake_github(state)
    _gh_env(monkeypatch, srv)
    monkeypatch.setattr(transport, "_BATCH", {"on": False})
    yield state
    srv.shutdown()


def body_of(state, path):
    return json.loads(base64.b64decode(state["files"][path]["content"]))


def run_rec(rid="20260305T100000Z-aaaa", **kw):
    rec = {"schema_version": 1, "kind": "run", "automation": "auto-a", "run_id": rid, "started_at": "2026-03-05T10:00:00Z",
           "status": "success", "events": []}
    rec.update(kw)
    return rec


# ---- batching: one commit per run -------------------------------------------------------------------------------------------
def test_a_runs_artefacts_are_sent_as_one_bundle_commit(gh):
    p = Paths().ensure()
    transport.enable_batching()
    assert transport.emit("heartbeat", "HOST1", "20260305T100000-run_start", {"ts": "2026-03-05T10:00:00Z", "host": "HOST1"}, p) is True
    assert transport.emit("run", "auto-a", "20260305T100000Z-aaaa", run_rec(), p) is True
    assert transport.emit("lifecycle", "auto-a", "20260305T100001000000-updated", {"ts": "2026-03-05T10:00:01Z"}, p) is True
    assert transport.emit("heartbeat", "HOST1", "20260305T100030-run_end", {"ts": "2026-03-05T10:00:30Z", "host": "HOST1"}, p) is True
    assert gh["put_count"] == 0 and len(list(p.spool.glob("*.json"))) == 4        # durable locally, nothing sent yet
    assert transport.flush_batch(p) == 4
    assert gh["put_count"] == 1 and not list(p.spool.glob("*.json"))              # ONE commit, spool drained
    (path,) = gh["files"]
    assert "/bundle/" in path and path.endswith(".json") and "/2026/03/05/" in path
    doc = body_of(gh, path)
    assert doc["kind"] == "bundle" and [i["kind"] for i in doc["items"]] == ["heartbeat", "run", "lifecycle", "heartbeat"] or len(doc["items"]) == 4
    assert gh["files"][path]["branch"] == "ingest-2026-03"


def test_batching_is_off_unless_the_entry_point_turns_it_on(gh):
    p = Paths().ensure()
    assert transport.emit("run", "auto-a", "20260305T100000Z-aaaa", run_rec(), p) is True
    assert gh["put_count"] == 1                                                    # immediate, as before


def test_resending_the_same_bundle_is_idempotent(gh):
    p = Paths().ensure()
    transport.enable_batching()
    items = [("heartbeat", "H", "20260305T100000-idle", {"ts": "2026-03-05T10:00:00Z", "host": "H"}),
             ("run", "auto-a", "20260305T100000Z-aaaa", run_rec())]
    for _ in range(2):
        for kind, name, art_id, payload in items:
            transport.emit(kind, name, art_id, payload, p)
        assert transport.flush_batch(p) == 2
    assert len(gh["files"]) == 1                                                   # same contents -> same file; the second PUT was a 422 'already there'


def test_old_spool_and_new_artefacts_travel_in_the_same_flush_and_big_ones_stay_alone(gh):
    p = Paths().ensure()
    big = {"ts": "2026-03-05T10:00:00Z", "automation": "auto-a", "run_id": "r", "blob": "x" * 700_000}
    transport._spool_write(p, "evidence", "auto-a", "20260305T100000Z-aaaa", big)
    transport._spool_write(p, "run", "auto-a", "20260305T100001Z-bbbb", run_rec("20260305T100001Z-bbbb"))
    transport._spool_write(p, "run", "auto-a", "20260305T100002Z-cccc", run_rec("20260305T100002Z-cccc"))
    assert transport.flush_spool(p) == 3
    kinds = sorted(("bundle" if "/bundle/" in k else "evidence") for k in gh["files"])
    assert kinds == ["bundle", "evidence"]                                         # the two small runs bundled, the big evidence file alone


# ---- layout, caching, retries -----------------------------------------------------------------------------------------------
def test_branches_are_per_month_and_a_later_process_does_not_recheck_them(gh):
    p = Paths().ensure()
    transport.emit("run", "auto-a", "20260305T100000Z-aaaa", run_rec(), p)
    transport.emit("run", "auto-a", "20260412T100000Z-bbbb", run_rec("20260412T100000Z-bbbb", started_at="2026-04-12T10:00:00Z"), p)
    assert gh["branches"] == {"ingest-2026-03", "ingest-2026-04"}
    gets = gh["get_count"]
    transport.emit("run", "auto-a", "20260305T110000Z-cccc", run_rec("20260305T110000Z-cccc"), p)   # a new transport object (new process)
    assert gh["get_count"] == gets                                                 # the branch is remembered in state/log-branches.json


def test_a_deleted_branch_is_recreated_once(gh):
    p = Paths().ensure()
    transport.emit("run", "auto-a", "20260305T100000Z-aaaa", run_rec(), p)
    gh["branches"].clear()                                                         # someone deleted the month branch; our cache is stale
    assert transport.emit("run", "auto-a", "20260305T110000Z-bbbb", run_rec("20260305T110000Z-bbbb"), p) is True
    assert "ingest-2026-03" in gh["branches"]


def test_secondary_rate_limit_and_retry_after_are_waited_out_not_fatal(gh):
    p = Paths().ensure()
    gh["script"] = [(403, {"Retry-After": "1"}, {"message": "You have exceeded a secondary rate limit"}),
                    (429, {}, {"message": "slow down"})]
    assert transport.emit("run", "auto-a", "20260305T100000Z-aaaa", run_rec(), p) is True
    assert gh["put_count"] == 3 and transport.auth_failure(p) is None             # a rate limit is not an authentication failure


# ---- authentication failure, token loss ------------------------------------------------------------------------------------
def test_401_is_remembered_pauses_sending_and_clears_on_success(gh):
    p = Paths().ensure()
    gh["script"] = [(401, {}, {"message": "Bad credentials"})]
    assert transport.emit("run", "auto-a", "20260305T100000Z-aaaa", run_rec(), p) is False
    f = transport.auth_failure(p)
    assert f and f["http"] == 401 and f["reason"] == "rejected" and transport.blocked(p)
    puts = gh["put_count"]
    assert transport.emit("run", "auto-a", "20260305T100001Z-bbbb", run_rec("20260305T100001Z-bbbb"), p) is False
    assert gh["put_count"] == puts                                                 # paused: no network attempt, straight to the spool
    assert len(list(p.spool.glob("*.json"))) == 2
    (p.state / "logging_blocked.json").unlink()                                    # the pause ends; the credential works again
    assert transport.flush_spool(p) == 2
    assert transport.auth_failure(p) is None and not transport.blocked(p)


def test_an_undecryptable_token_is_a_clear_state_not_an_endless_retry(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTOMAGIX_LOGGING_TOKEN", raising=False)
    monkeypatch.setenv("AUTOMAGIX_LOG_TRANSPORT", "github-api")
    monkeypatch.setenv("AUTOMAGIX_LOG_REMOTE", "o/logs")
    monkeypatch.setenv("AUTOMAGIX_GITHUB_API", "http://127.0.0.1:9")
    p = Paths().ensure()
    (p.secrets / "logging_token.dpapi").write_bytes(b"not a blob this user can decrypt")
    assert secrets.logging_token_status(p)[0] == "undecryptable"
    assert transport.emit("run", "auto-a", "20260305T100000Z-aaaa", run_rec(), p) is False
    f = transport.auth_failure(p)
    assert f and f["reason"] == "undecryptable" and transport.blocked(p)
    assert transport.flush_spool(p) == 0                                           # paused, not retried every run
    # doctor says exactly what to do
    from wrapper_core import doctor
    rows = {r["check"]: r for r in doctor.run_checks(p, online=False)}
    assert rows["logging token (DPAPI)"]["status"] == "FAIL" and "reinstall" in rows["logging token (DPAPI)"]["fix"].lower()


def test_missing_token_status(monkeypatch):
    monkeypatch.delenv("AUTOMAGIX_LOGGING_TOKEN", raising=False)
    assert secrets.logging_token_status(Paths().ensure())[0] == "missing"
    monkeypatch.setenv("AUTOMAGIX_LOGGING_TOKEN", "x")
    assert secrets.logging_token_status(Paths().ensure()) == ("ok", "environment")


# ---- spool caps -------------------------------------------------------------------------------------------------------------
def test_spool_is_capped_by_age_and_size_dropping_evidence_first(tmp_path):
    p = Paths().ensure()
    old = p.spool / "run-OLD.json"
    old.write_text("{}")
    os.utime(old, (time.time() - 15 * 86400, time.time() - 15 * 86400))
    ev = p.spool / "evidence-E1.json"
    ev.write_text("e" * 500)
    keep = p.spool / "run-NEW.json"
    keep.write_text("r" * 500)
    for i, f in enumerate((ev, keep)):
        os.utime(f, (time.time() - 100 + i, time.time() - 100 + i))
    dropped = transport.cap_spool(p, max_bytes=600)
    assert dropped["age"] == 1 and dropped["size"] == 1
    assert not old.exists() and not ev.exists() and keep.exists()                  # the old run is gone, evidence went first, the newest run stayed
    stats = transport.spool_stats(p)
    assert stats["count"] == 1 and stats["bytes"] == 500


# ---- truncation to the ingest limits ----------------------------------------------------------------------------------------
def test_a_huge_run_arrives_truncated_with_status_and_error_intact():
    events = [{"ts": "2026-03-05T10:00:00Z", "event": "item_processed", "data": {"item_id": str(i), "outcome": "success"}} for i in range(9000)]
    rec = run_rec(events=events, status="failed", error={"category": "X", "message": "boom"}, synopsis="it broke")
    out = transport.cap_run_record(rec)
    assert out["truncated"] is True and len(out["events"]) <= transport.MAX_RUN_EVENTS and out["events_dropped"] == 9000 - len(out["events"])
    assert out["status"] == "failed" and out["error"]["message"] == "boom" and out["synopsis"] == "it broke"
    assert out["events"][0] == events[0] and out["events"][-1] == events[-1]       # the start and the end of the run survive


def test_a_run_with_big_events_is_cut_to_fit_one_megabyte():
    events = [{"ts": "2026-03-05T10:00:00Z", "event": "e", "data": {"blob": "x" * 5000}} for _ in range(500)]
    out = transport.cap_run_record(run_rec(events=events))
    assert len(json.dumps(out)) <= transport.MAX_RUN_RECORD_BYTES and out["truncated"] is True


def test_a_small_run_is_left_untouched():
    rec = run_rec(events=[{"ts": "2026-03-05T10:00:00Z", "event": "e", "data": {}}])
    assert transport.cap_run_record(rec) is rec


def test_artefact_time_falls_back_to_the_id_prefix():
    from datetime import datetime, timezone
    assert transport.art_time("heartbeat", {}, "20260305T101500-idle") == datetime(2026, 3, 5, tzinfo=timezone.utc)
    assert transport.art_time("run", {"started_at": "2026-04-01T00:00:00Z"}, "x").month == 4


# ---- heartbeats -------------------------------------------------------------------------------------------------------------
def test_idle_heartbeats_are_sent_about_once_a_day_on_the_github_transport(gh, monkeypatch):
    p = Paths().ensure()
    assert heartbeat.send_heartbeat(p, "idle") is True
    assert heartbeat.send_heartbeat(p, "idle") is True and heartbeat.send_heartbeat(p, "login") is True
    assert gh["put_count"] == 1                                                    # the second and third were folded into the first day's
    assert heartbeat.send_heartbeat(p, "run_start", "auto-a", "RID") is True
    assert gh["put_count"] == 2                                                    # run heartbeats are never throttled
    monkeypatch.setenv("AUTOMAGIX_HEARTBEAT_EVERY_H", "0")
    assert heartbeat.send_heartbeat(p, "idle") is True and gh["put_count"] == 3


def test_heartbeat_reports_the_offline_queue_and_a_failing_credential(gh):
    p = Paths().ensure()
    transport._spool_write(p, "run", "auto-a", "20260305T100000Z-aaaa", run_rec())
    transport.note_auth_failure(p, 401, "rejected")
    fields = heartbeat.health_fields(p)
    assert fields["spool"]["count"] == 1 and fields["logging_auth_failed"]["http"] == 401
