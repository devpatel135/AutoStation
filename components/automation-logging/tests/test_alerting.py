from __future__ import annotations

import io
import json
from datetime import timedelta

import pytest

from automation_logging import alerting
from automation_logging.alerting import AlertEngine, Context, build_alert, render_text
from automation_logging.common import read_json

from factories import NOW, sig


class FakeHttp:
    def __init__(self, responder=None):
        self.calls, self.responder = [], responder

    def request(self, method, url, headers=None, body=None, timeout=30):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "body": json.loads(body) if body else None})
        if self.responder:
            return self.responder(method, url, body)
        return 200, b"{}"


class FakeSmtp:
    sent = []

    def __init__(self, host, port):
        self.host, self.port, self.tls, self.login_args = host, port, False, None

    def starttls(self, context=None):
        self.tls = True

    def login(self, u, p):
        self.login_args = (u, p)

    def send_message(self, msg):
        FakeSmtp.sent.append(msg)

    def quit(self):
        pass


def engine(tmp_path, channels=None, routing=None, state=None, http=None, env=None, **cfg):
    config = json.loads(json.dumps(alerting.DEFAULT_CONFIG))
    config["channels"] = channels or [{"name": "outbox", "type": "file", "path": "out.jsonl"}]
    config["routing"] = routing or {"critical": ["outbox"], "high": ["outbox"], "medium": ["outbox"], "low": []}
    config.update(cfg)
    ctx = Context(http=http or FakeHttp(), smtp_factory=FakeSmtp, env=env or {}, state_dir=tmp_path, stdout=io.StringIO())
    return AlertEngine(config, state if state is not None else {}, ctx, log_base_url="https://logs.example/archive")


def outbox(tmp_path):
    p = tmp_path / "out.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# ---------------------------------------------------------------------------------------------- lifecycle
def test_open_dedupe_reminder_resolve_cycle(tmp_path):
    e = engine(tmp_path)
    s = sig(sid="sig1")
    r1 = e.process([s], NOW)
    assert [a["kind"] for a in outbox(tmp_path)] == ["open"] and r1["suppressed"] == []
    r2 = e.process([s], NOW + timedelta(hours=1))
    assert r2["suppressed"] == [{"id": "sig1", "reason": "dedupe"}] and len(outbox(tmp_path)) == 1
    e.process([s], NOW + timedelta(hours=25))
    assert [a["kind"] for a in outbox(tmp_path)] == ["open", "reminder"] and e.state["sig1"]["count"] == 2
    r4 = e.process([], NOW + timedelta(hours=26))
    assert r4["resolved"] == ["sig1"] and outbox(tmp_path)[-1]["kind"] == "resolved" and e.state["sig1"]["resolved"] is True
    assert e.process([], NOW + timedelta(hours=27))["resolved"] == []        # not resolved twice


def test_flap_suppression_then_reopen_after_dedupe_window(tmp_path):
    e = engine(tmp_path)
    s = sig(sid="sig1")
    e.process([s], NOW)
    e.process([], NOW + timedelta(hours=1))                                   # resolved
    r = e.process([s], NOW + timedelta(hours=2))                              # flaps back within the window
    assert r["suppressed"] == [{"id": "sig1", "reason": "flap-suppressed"}] and e.state["sig1"]["resolved"] is False
    assert [a["kind"] for a in outbox(tmp_path)] == ["open", "resolved"]
    e.process([], NOW + timedelta(hours=3))
    e.process([s], NOW + timedelta(hours=60))                                 # long after: a genuinely new alert
    assert outbox(tmp_path)[-1]["kind"] == "open"


def test_severity_escalation_sends_immediately(tmp_path):
    e = engine(tmp_path)
    e.process([sig(sid="x", severity="medium")], NOW)
    e.process([sig(sid="x", severity="critical")], NOW + timedelta(minutes=5))
    assert [a["kind"] for a in outbox(tmp_path)] == ["open", "escalated"] and e.state["x"]["severity"] == "critical"


def test_quiet_hours_hold_low_severity_but_not_critical(tmp_path):
    quiet = {"timezone": "UTC", "window": "22:00-06:00", "min_severity": "critical"}
    e = engine(tmp_path, quiet_hours=quiet)
    night = NOW.replace(hour=23)
    r = e.process([sig(sid="m", severity="medium"), sig(sid="c", severity="critical")], night)
    assert [a["alert_id"] for a in outbox(tmp_path)] == ["c"] and {"id": "m", "reason": "quiet-hours"} in r["suppressed"]
    assert "m" not in e.state                                                  # untouched => fires after the window
    e.process([sig(sid="m", severity="medium"), sig(sid="c", severity="critical")], NOW.replace(hour=7))
    assert "m" in [a["alert_id"] for a in outbox(tmp_path)]
    assert alerting.in_quiet_hours(quiet, NOW.replace(hour=3)) and not alerting.in_quiet_hours(quiet, NOW)


def test_failed_delivery_is_retried_next_run_and_other_channels_still_fire(tmp_path):
    http = FakeHttp(lambda *a: (500, b"boom"))
    chans = [{"name": "outbox", "type": "file", "path": "out.jsonl"}, {"name": "hook", "type": "webhook", "url": "https://hooks.example/x"}]
    e = engine(tmp_path, channels=chans, routing={"high": ["hook"], "critical": ["hook"], "medium": ["hook"]}, http=http)
    r = e.process([sig(sid="s")], NOW)
    assert r["errors"] and r["errors"][0]["channel"] == "hook" and "s" not in e.state
    e.ctx.http = FakeHttp()                                                    # channel recovers
    e.process([sig(sid="s")], NOW + timedelta(minutes=15))
    assert e.state["s"]["count"] == 1
    e2 = engine(tmp_path, channels=chans, routing={"high": ["outbox", "hook"]}, http=FakeHttp(lambda *a: (500, b"x")), state={})
    r = e2.process([sig(sid="t")], NOW)
    assert len(r["errors"]) == 1 and "t" in e2.state                           # outbox accepted it => counts as delivered


def test_dry_run_changes_nothing(tmp_path):
    http = FakeHttp()
    e = engine(tmp_path, channels=[{"name": "hook", "type": "webhook", "url": "https://h.example"}], routing={"high": ["hook"]}, http=http)
    r = e.process([sig(sid="d")], NOW, dry_run=True)
    assert r["dry_run"] and r["would_send"][0]["channels"] == ["hook"] and http.calls == [] and e.state == {}


def test_owner_override_and_no_route(tmp_path):
    chans = [{"name": "outbox", "type": "file", "path": "out.jsonl"}, {"name": "ownerbox", "type": "file", "path": "owner.jsonl"}]
    e = engine(tmp_path, channels=chans, owners={"auto-a": {"channels": ["ownerbox"], "email": "lead@example.com"}})
    e.process([sig(sid="o")], NOW)
    rec = json.loads((tmp_path / "owner.jsonl").read_text().splitlines()[0])
    assert rec["owner"] == "lead@example.com" and not (tmp_path / "out.jsonl").exists()
    r = e.process([sig(sid="low", severity="low", automation="other")], NOW)
    assert {"id": "low", "reason": "no-route", "kind": "open"} in r["suppressed"]


# ---------------------------------------------------------------------------------------------- content
def test_alert_carries_everything_an_operator_needs():
    s = sig(sid="a1", diagnosis={"root_cause": "VPN dropped", "category": "NETWORK_ERROR", "suggested_action": "check VPN", "confidence": "high"},
            retry_history=[{"attempt": 1, "category": "NETWORK_ERROR", "decision": "retry", "source": "deterministic", "outcome": "failed_again"}],
            log_path="run/auto-a/2026/10/14/r1.json", error_message="timed out")
    a = build_alert(s, "open", NOW, "https://logs.example/archive/")
    assert a["automation"] == "auto-a" and a["signal_type"] == "FAILED" and a["error_category"] == "NETWORK_ERROR"
    assert a["diagnosis"]["root_cause"] == "VPN dropped" and a["retry_history"] and a["synopsis"] == "It broke."
    assert a["log_link"] == "https://logs.example/archive/run/auto-a/2026/10/14/r1.json"
    text = render_text(a)
    assert "VPN dropped" in text and "Retry history" in text and "Run synopsis: It broke." in text and a["alert_id"] in text


# ---------------------------------------------------------------------------------------------- channels
def test_teams_slack_and_generic_webhook_payloads(tmp_path):
    for fmt, key in (("teams", "MessageCard"), ("slack", "blocks"), ("json", "alert")):
        http = FakeHttp()
        e = engine(tmp_path, channels=[{"name": "h", "type": "webhook", "format": fmt, "url_env": "HOOK"}], routing={"high": ["h"]},
                   http=http, env={"HOOK": "https://hooks.example/abc"})
        e.process([sig(sid="w" + fmt)], NOW)
        body = http.calls[0]["body"]
        assert http.calls[0]["url"] == "https://hooks.example/abc" and (key in body or key == "MessageCard" and body["@type"] == key)
    assert body["alert"]["title"].startswith("[AUTOSTATION HIGH]")


def test_email_channel(tmp_path):
    FakeSmtp.sent = []
    env = {"SMTP_HOST": "smtp.example", "SMTP_PORT": "587", "SMTP_FROM": "autostation@example.com", "SMTP_STARTTLS": "1",
           "SMTP_USER": "u", "SMTP_PASSWORD": "p"}
    e = engine(tmp_path, channels=[{"name": "mail", "type": "email", "to": ["ops@example.com"]}], routing={"high": ["mail"]}, env=env)
    e.process([sig(sid="e1")], NOW)
    msg = FakeSmtp.sent[0]
    assert "ops@example.com" in msg["To"] and "owner@example.com" in msg["To"] and msg["From"] == "autostation@example.com"
    assert msg["Subject"].startswith("[AUTOSTATION HIGH]") and "Alert id: e1" in msg.get_content()
    r = engine(tmp_path, channels=[{"name": "mail", "type": "email"}], routing={"high": ["mail"]}, env={}).process([sig(sid="e2")], NOW)
    assert "SMTP_HOST" in r["errors"][0]["error"]


def test_jira_creates_then_comments_with_fingerprint_dedupe(tmp_path):
    state = {"open": False}

    def responder(method, url, body):
        if method == "GET":
            return 200, json.dumps({"issues": [{"key": "OPS-7"}] if state["open"] else []}).encode()
        if url.endswith("/issue"):
            state["open"] = True
            return 201, b'{"key": "OPS-7"}'
        return 201, b"{}"

    http = FakeHttp(responder)
    env = {"JIRA_URL": "https://jira.example/", "JIRA_TOKEN": "tok", "JIRA_PROJECT": "OPS"}
    e = engine(tmp_path, channels=[{"name": "jira", "type": "jira"}], routing={"high": ["jira"]}, http=http, env=env)
    e.process([sig(sid="j1")], NOW)
    create = [c for c in http.calls if c["method"] == "POST"][0]
    assert create["url"].endswith("/rest/api/2/issue") and create["headers"]["Authorization"] == "Bearer tok"
    assert "autostation-j1" in create["body"]["fields"]["labels"] and create["body"]["fields"]["project"] == {"key": "OPS"}
    search = [c for c in http.calls if c["method"] == "GET"][0]["url"]
    assert "autostation-j1" in search and "statusCategory" in search
    e.process([sig(sid="j1")], NOW + timedelta(hours=30))                    # reminder => comment on the existing issue
    assert http.calls[-1]["url"].endswith("/issue/OPS-7/comment") and "REMINDER" in http.calls[-1]["body"]["body"]
    assert sum(1 for c in http.calls if c["method"] == "POST" and c["url"].endswith("/issue")) == 1


def test_gitlab_issue_create_note_and_close(tmp_path):
    opened = {"v": False}

    def responder(method, url, body):
        if method == "GET":
            return 200, json.dumps([{"iid": 5}] if opened["v"] else []).encode()
        if method == "POST" and url.endswith("/issues"):
            opened["v"] = True
            return 201, b'{"iid": 5}'
        return 200, b"{}"

    http = FakeHttp(responder)
    env = {"GITLAB_API_URL": "https://gl.example/api/v4", "GITLAB_ALERT_PROJECT_ID": "42", "GITLAB_ALERT_TOKEN": "t"}
    e = engine(tmp_path, channels=[{"name": "gl", "type": "gitlab-issue"}], routing={"high": ["gl"]}, http=http, env=env)
    e.process([sig(sid="g1")], NOW)
    assert http.calls[-1]["url"].endswith("/projects/42/issues") and http.calls[-1]["headers"]["PRIVATE-TOKEN"] == "t"
    e.process([], NOW + timedelta(hours=1))                                   # resolved => note + close
    assert [c["method"] for c in http.calls[-2:]] == ["POST", "PUT"] and http.calls[-1]["body"] == {"state_event": "close"}


def test_stdout_channel_and_unknown_type(tmp_path):
    e = engine(tmp_path, channels=[{"name": "o", "type": "stdout"}], routing={"high": ["o"]})
    e.process([sig(sid="p")], NOW)
    assert json.loads(e.ctx.stdout.getvalue().splitlines()[0])["alert_id"] == "p"
    with pytest.raises(ValueError):
        engine(tmp_path, channels=[{"name": "x", "type": "carrier-pigeon"}])


def test_run_persists_state_and_load_config(tmp_path):
    cfg = tmp_path / "alerting.yaml"
    cfg.write_text("channels:\n  - {name: outbox, type: file, path: o.jsonl}\nrouting:\n  high: [outbox]\n  critical: [outbox]\nre_alert_interval_h: 6\n")
    ctx = Context(state_dir=tmp_path, env={})
    alerting.run([sig(sid="z")], cfg, tmp_path / "alerts.json", NOW, ctx=ctx)
    assert read_json(tmp_path / "alerts.json")["z"]["count"] == 1
    assert alerting.load_config(cfg)["re_alert_interval_h"] == 6
    alerting.run([sig(sid="z")], cfg, tmp_path / "alerts.json", NOW + timedelta(hours=7), ctx=ctx)
    assert read_json(tmp_path / "alerts.json")["z"]["count"] == 2             # 6h interval honoured
