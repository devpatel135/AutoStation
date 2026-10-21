"""Regression tests for the log-pipeline hardening: the logs repo is written with a credential that ships in a PUBLIC installer,
so every artefact is attacker-authored and every dashboard is public static HTML."""
from __future__ import annotations

import copy
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

import pytest
from wrapper_core import mockllm

from automation_logging import alerting, analytics, dashboard, diagnose, ingest, program, triggers, validate
from automation_logging.alerting import AlertEngine, Context, build_alert, sanitize
from automation_logging.common import read_json
from automation_logging.synthetic import write_archive

from factories import NOW, T, automation_entry, data_of, manifest, manifest_dict, run_record, signals_of, sig
from test_alerting import FakeHttp, FakeSmtp, engine
from test_diagnose import Capture, diagnosed, failing_run
from test_ingest import GIT, git, push_like_endpoint

REJECT_NAME = re.compile(r"^[0-9a-f]{16}\.(txt|reason)$")


def put(src, ring, kind, name, art_id, payload):
    d = src / "ingest" / ring / kind / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{art_id}.json").write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def good_run():
    return run_record("auto-a", T(0, 9), host="VDI-1")


# ============================================================================================== 1. ingest poison pill
def test_hostile_ring_kind_and_name_are_rejected_and_never_touch_the_archive(tmp_path):
    src, arch = tmp_path / "logs", tmp_path / "archive"
    r = good_run()
    put(src, "evil", "run", "auto-a", "r1", r)                      # unknown ring
    put(src, "pilot", "bogus", "auto-a", "r2", r)                   # unknown kind
    put(src, "pilot", "run", ".hidden", "r3", r)                    # dot-name
    put(src, "pilot", "run", "bad name", "r4", r)                   # characters outside the allow-list
    put(src, "pilot", "run", "auto-b", "r5", r)                     # name != automation inside the record
    stats = ingest.ingest_dir(src, arch)
    assert stats["archived"] == 0 and stats["rejected"] == 5
    assert sorted(p.name for p in arch.iterdir()) == ["_rejected"]
    assert all(REJECT_NAME.match(p.name) for p in (arch / "_rejected").iterdir())


def test_oversized_artefact_is_rejected_before_json_parsing(tmp_path, monkeypatch):
    src, arch = tmp_path / "logs", tmp_path / "archive"
    put(src, "pilot", "run", "auto-a", "big", "x" * (validate.MAX_BYTES["run"] + 50))
    monkeypatch.setattr(ingest.json, "loads", lambda *a, **k: pytest.fail("json.loads called on an oversized artefact"))
    stats = ingest.ingest_dir(src, arch)
    assert stats["rejected"] == 1 and stats["archived"] == 0
    assert "limit" in next((arch / "_rejected").glob("*.reason")).read_text()


def _mut(**kw):
    r = good_run()
    r.update(kw)
    return r


@pytest.mark.parametrize("bad", [
    _mut(status="pwned"), _mut(status=["failed"]), _mut(events="not-a-list"), _mut(events=["str"]), _mut(events=[{"event": 5}]),
    _mut(items=[1, 2]), _mut(items={"total": "many"}), _mut(items={"total": -1}), _mut(duration_ms="abc"), _mut(duration_ms=float("nan")),
    _mut(started_at="yesterday"), _mut(started_at=12345), _mut(error="boom"), _mut(error={"message": {"x": 1}}), _mut(ring="mars"),
    _mut(host=["a"]), _mut(synopsis={"a": 1}), _mut(exit_code="0"), _mut(run_id="../../etc/passwd"),
])
def test_type_and_enum_confusion_never_reaches_the_archive(tmp_path, bad):
    src, arch = tmp_path / "logs", tmp_path / "archive"
    put(src, "pilot", "run", "auto-a", "poison", json.dumps(bad))      # json.dumps writes NaN literally; json.loads accepts it
    stats = ingest.ingest_dir(src, arch)
    assert stats["archived"] == 0 and stats["rejected"] == 1
    assert not (arch / "run").exists()


def test_valid_records_still_pass_strict_validation(tmp_path):
    src, arch = tmp_path / "logs", tmp_path / "archive"
    r = run_record("auto-a", T(0, 9), host="VDI-1", items=[("i1", "success", 5, None), ("i2", "skipped", 5, "dup"), ("i3", "failed", 9, "DATA_ERROR")],
                   status="partial", retries=[{"attempt": 1, "category": "NETWORK_ERROR", "decision": "retry", "source": "deterministic"}])
    put(src, "pilot", "run", "auto-a", r["run_id"], r)
    assert ingest.ingest_dir(src, arch)["archived"] == 1


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_git_poison_refs_are_quarantined_by_hash_and_deleted_not_refetched(tmp_path):
    bare = tmp_path / "logging.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare")
    r = good_run()
    push_like_endpoint(bare, tmp_path, "pilot", "run", "auto-a", r["run_id"], r)
    push_like_endpoint(bare, tmp_path, "pilot", "run", "auto-a", "garbage", "{nope")
    push_like_endpoint(bare, tmp_path, "evil", "run", "auto-a", "badring", r)
    push_like_endpoint(bare, tmp_path, "pilot", "run", "evil;name", "x", r)
    # a ref with the wrong number of segments
    repo = tmp_path / "short"
    repo.mkdir()
    (repo / "f.json").write_text("{}")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "x")
    git(repo, "push", "-q", str(bare), "HEAD:refs/heads/ingest/pilot/shortshape")
    arch = tmp_path / "archive"
    stats = ingest.ingest_git(str(bare), arch)
    assert stats["archived"] == 1 and stats["rejected"] == 4 and stats["errors"] == []
    assert git(bare, "for-each-ref", "--format=%(refname)").stdout.split() == []                 # every ref, poison included, is gone
    assert all(REJECT_NAME.match(p.name) for p in (arch / "_rejected").iterdir())
    again = ingest.ingest_git(str(bare), arch)
    assert again["archived"] == 0 and again["rejected"] == 0                                    # nothing re-fetched forever


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_git_max_refs_defers_the_rest(tmp_path):
    bare = tmp_path / "logging.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare")
    for i in range(3):
        rec = run_record("auto-a", T(0, 9 + i), host="VDI-1")
        push_like_endpoint(bare, tmp_path, "pilot", "run", "auto-a", rec["run_id"], rec)
    arch = tmp_path / "archive"
    first = ingest.ingest_git(str(bare), arch, max_refs=2)
    assert first["archived"] == 2 and first["deferred_refs"] == 1 and first["errors"] == []
    assert len(git(bare, "for-each-ref", "--format=%(refname)").stdout.split()) == 1
    assert ingest.ingest_git(str(bare), arch, max_refs=2)["archived"] == 1


def test_git_token_travels_in_the_environment_not_argv():
    prefix, env = ingest._git_env("https://git.example/logs.git", "S3CR3T-TOKEN")
    assert prefix == [] and "S3CR3T-TOKEN" not in " ".join(prefix)
    assert env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"] == "PRIVATE-TOKEN: S3CR3T-TOKEN"
    assert "GIT_CONFIG_COUNT" not in ingest._git_env("/local/path", "S3CR3T-TOKEN")[1]


def test_load_archive_skips_malformed_records_and_counts_them(tmp_path):
    arch = tmp_path / "archive"
    write_archive(arch, [good_run()])
    day = arch / "run" / "auto-a" / "2026" / "10" / "14"
    (day / "forged.json").write_text(json.dumps(dict(good_run(), run_id="forged", status={"x": 1})))
    (day / "junk.json").write_text("{truncated")
    (day / "mismatch.json").write_text(json.dumps(dict(good_run(), run_id="mismatch", automation="other")))
    data = analytics.load_archive(arch, NOW)
    assert len(data.runs) == 1 and data.skipped_malformed == 3
    report, _ = analytics.compute(data, manifest(), NOW, None, {})                # never raises
    assert report["data_quality"]["skipped_malformed"] == 3
    html = dashboard.render(report, [])
    assert "3 malformed log record(s) were skipped" in html


def test_trigger_events_from_telemetry_are_restricted_and_a_bad_run_cannot_stop_evaluation():
    mf = manifest_dict({"auto-a": automation_entry(), "auto-b": automation_entry()})
    ev = lambda params, slot="poll": [{"ts": "2026-10-14T09:00:01.000Z", "event": "trigger", "data": {"target": "auto-b", "params": params, "slot": slot}}]  # noqa: E731
    nested = run_record("auto-a", T(0, 9), run_id="r-nested", extra_events=ev({"a": {"b": 1}}))
    long_ = run_record("auto-a", T(0, 9, 1), run_id="r-long", extra_events=ev({"a": "x" * 500}))
    slot = run_record("auto-a", T(0, 9, 2), run_id="r-slot", extra_events=ev({}, "../../x"))
    fine = run_record("auto-a", T(0, 9, 3), run_id="r-fine", extra_events=ev({"n": 1}))
    broken = dict(run_record("auto-a", T(0, 9, 4), run_id="r-broken"), status=None)
    broken["events"] = 7
    doc, stats = triggers.compute(None, data_of(nested, long_, slot, fine, broken), mf, NOW)
    assert [t["source"] for t in doc["triggers"]] == ["event:auto-a:r-fine"]
    assert stats["rejected_events"] == 3 and stats["skipped_malformed"] >= 0


# ============================================================================================== 2. alerting
HOSTILE = "boom <!channel> <!here> @everyone `code` ```fence``` [click](http://evil.example) <@U123> <http://evil|safe>\x1b[31m‮\x00"


def test_sanitize_neutralises_markup_mentions_and_control_characters():
    out = sanitize(HOSTILE)
    for bad in ("<!channel>", "<!here>", "@everyone", "`", "](", "<@", "<http", "\x1b", "‮", "\x00", "<", ">"):
        assert bad not in out, bad
    assert "boom" in out


def test_hostile_signal_text_is_inert_in_every_channel(tmp_path):
    s = sig(sid="h1", detail=HOSTILE, host=HOSTILE, synopsis=HOSTILE, error_message=HOSTILE,
            diagnosis={"root_cause": HOSTILE, "suggested_action": HOSTILE, "confidence": "high", "category": "UNKNOWN"})
    a = build_alert(s, "open", NOW, "https://logs.example/archive")
    for bad in ("<!channel>", "<!here>", "@everyone", "](http", "<@U123>", "\x1b"):
        assert bad not in json.dumps(a) and bad not in alerting.render_text(a), bad
    for fmt in ("slack", "teams", "json"):
        http = FakeHttp()
        e = engine(tmp_path, channels=[{"name": "h", "type": "webhook", "format": fmt, "url": "https://hooks.example/x"}], routing={"high": ["h"]}, http=http)
        e.process([s], NOW)
        sent = json.dumps(http.calls[0]["body"])
        assert "<!channel>" not in sent and "@everyone" not in sent and "```fence" not in sent
    slack = alerting.WebhookChannel({"name": "h", "format": "slack"}, Context(env={})).payload(a)
    assert slack["blocks"][0]["text"]["text"].count("```") == 2                      # only our own fence, no breakout


def test_jira_jql_quote_injection_is_escaped(tmp_path):
    http = FakeHttp()
    env = {"JIRA_URL": "https://jira.example", "JIRA_TOKEN": "t", "JIRA_PROJECT": 'OPS" OR project = "SECRET'}
    e = engine(tmp_path, channels=[{"name": "jira", "type": "jira"}], routing={"high": ["jira"]}, http=http, env=env)
    e.process([sig(sid='x" OR labels = "y')], NOW)
    jql = urllib.parse.unquote(next(c for c in http.calls if c["method"] == "GET")["url"])
    assert 'project = "OPS\\" OR project = \\"SECRET"' in jql
    assert '" OR labels = "y' not in jql                                           # the alert id cannot close the quote either


def test_email_refuses_credentials_without_tls_and_verifies_certificates(tmp_path):
    FakeSmtp.sent = []
    env = {"SMTP_HOST": "smtp.example", "SMTP_USER": "u", "SMTP_PASSWORD": "p"}
    r = engine(tmp_path, channels=[{"name": "mail", "type": "email", "to": ["o@example.com"]}], routing={"high": ["mail"]}, env=env).process([sig(sid="m1")], NOW)
    assert "TLS" in r["errors"][0]["error"] and FakeSmtp.sent == []
    seen = {}

    class Spy(FakeSmtp):
        def starttls(self, context=None):
            seen["ctx"] = context
            super().starttls(context)

    ctx = Context(http=FakeHttp(), smtp_factory=Spy, env=dict(env, SMTP_STARTTLS="1"), state_dir=tmp_path)
    ch = alerting.EmailChannel({"name": "mail", "to": ["o@example.com"]}, ctx)
    ch.send(build_alert(sig(sid="m2"), "open", NOW))
    assert isinstance(seen["ctx"], ssl.SSLContext) and seen["ctx"].verify_mode == ssl.CERT_REQUIRED and seen["ctx"].check_hostname


def test_http_urls_are_refused_unless_explicitly_allowed(tmp_path):
    http = FakeHttp()
    e = engine(tmp_path, channels=[{"name": "h", "type": "webhook", "url": "http://hooks.example/x"}], routing={"high": ["h"]}, http=http)
    r = e.process([sig(sid="p1")], NOW)
    assert "https" in r["errors"][0]["error"] and http.calls == []
    e = engine(tmp_path, channels=[{"name": "h", "type": "webhook", "url": "http://hooks.example/x", "allow_insecure_http": True}], routing={"high": ["h"]}, http=http)
    assert e.process([sig(sid="p2")], NOW)["errors"] == [] and len(http.calls) == 1
    env = {"JIRA_URL": "http://jira.example", "JIRA_TOKEN": "t", "JIRA_PROJECT": "OPS"}
    r = engine(tmp_path, channels=[{"name": "j", "type": "jira"}], routing={"high": ["j"]}, http=FakeHttp(), env=env).process([sig(sid="p3")], NOW)
    assert "https" in r["errors"][0]["error"]


def test_redirects_to_another_host_or_down_to_http_are_refused():
    h = alerting._SameHostRedirect()
    req = urllib.request.Request("https://hooks.example/a", headers={"Authorization": "Bearer tok"})
    with pytest.raises(urllib.error.HTTPError):
        h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/steal")
    with pytest.raises(urllib.error.HTTPError):
        h.redirect_request(req, None, 302, "Found", {}, "http://hooks.example/a")
    assert h.redirect_request(req, None, 302, "Found", {}, "https://hooks.example/b").full_url == "https://hooks.example/b"


# ============================================================================================== 3. diagnose cache
def test_diagnosis_cache_expires_is_bound_to_its_source_and_marked_unverified(tmp_path):
    from datetime import timedelta
    mockllm.register(Capture(), "cap")
    (_, s1), _ = diagnosed([failing_run()], manifest(), tmp_path, now=NOW)
    cache = read_json(tmp_path / "diag.json")
    entry = next(iter(cache.values()))
    assert s1["calls"] == 1 and entry["created_at"] and len(entry["source_record_hash"]) == 64 and entry["unverified"] is True
    (sigs, s2), _ = diagnosed([failing_run()], manifest(), tmp_path, now=NOW + timedelta(hours=1))
    assert s2["cached"] == 1 and s2["calls"] == 0 and sigs[0]["diagnosis"]["unverified"] is True
    (_, s3), _ = diagnosed([failing_run()], manifest(), tmp_path, now=NOW + timedelta(hours=25))
    assert s3["calls"] == 1 and s3["cached"] == 0                                   # expired after the TTL
    for e in cache.values():                                                        # same key, different source record => miss
        e["source_record_hash"] = "0" * 64
    (tmp_path / "diag.json").write_text(json.dumps(cache))
    (_, s4), _ = diagnosed([failing_run()], manifest(), tmp_path, now=NOW + timedelta(hours=25, minutes=1))
    assert s4["calls"] == 1


def test_legacy_cache_entries_without_provenance_are_not_trusted(tmp_path):
    mockllm.register(Capture(), "cap")
    (_, s1), d = diagnosed([failing_run()], manifest(), tmp_path, now=NOW)
    cache = read_json(tmp_path / "diag.json")
    for e in cache.values():
        e.pop("source_record_hash"), e.pop("created_at")
    (tmp_path / "diag.json").write_text(json.dumps(cache))
    (_, s2), _ = diagnosed([failing_run()], manifest(), tmp_path, now=NOW)
    assert s2["calls"] == 1 and s2["cached"] == 0


# ============================================================================================== 4. CSP + labelling
def test_dashboard_and_program_carry_csp_and_the_advisory_banner(tmp_path):
    runs = [failing_run(), good_run()]
    d = data_of(*runs)
    sigs, report, _ = signals_of(d, manifest())
    sigs = [sig(sid="d1", diagnosis={"root_cause": "x", "suggested_action": "y", "confidence": "low", "unverified": True})]
    dashboard.build_site(report, d, tmp_path / "fleet")
    page = (tmp_path / "fleet" / "index.html").read_text(encoding="utf-8")
    assert dashboard.CSP_META in page and "default-src 'none'" in page and "form-action 'none'" in page and "base-uri 'none'" in page
    assert "Telemetry is submitted by installed clients and is not authenticated; treat numbers as advisory" in page
    html = dashboard.render(dict(report, signals=sigs), [])
    assert "model output over unauthenticated telemetry" in html
    out = program.build(report, d, manifest(), tmp_path / "prog", program.load_config(None), NOW)
    prog = next((tmp_path / "prog").rglob("index.html")).read_text(encoding="utf-8")
    assert dashboard.CSP_META in prog and "treat numbers as advisory" in prog and out["program"]
