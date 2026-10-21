from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent.parent / "wrapper-core"))
sys.path.insert(0, str(ROOT))

from wrapper_core.testing import run_payload  # noqa: E402

FAST = {"max_retries": 1, "network_retry_wait_s": 0, "backoff_multiplier": 1.0}
SIGNALS = {"signals": [
    {"id": "s1", "type": "FAILED", "severity": "high", "automation": "automation-invoice-router", "ring": "pilot", "host": "VDI-1",
     "detail": "run failed", "owner": "ap-team@example.com", "evidence": ["r1"],
     "diagnosis": {"root_cause": "Target portal changed login", "suggested_action": "Re-record selectors"}},
    {"id": "s2", "type": "SLOW", "severity": "medium", "automation": "automation-hello-world", "ring": "pilot", "detail": "slow", "owner": "x@example.com"},
    {"id": "s3", "type": "MISSED", "severity": "critical", "automation": "automation-hello-world", "ring": "broad", "detail": "no run", "owner": "x@example.com"}]}


@pytest.fixture
def signals_file(tmp_path):
    f = tmp_path / "signals.json"
    f.write_text(json.dumps(SIGNALS))
    return f


def test_outbox_channel_filters_by_severity_and_is_idempotent(signals_file):
    sb = signals_file.parent / "sb"
    params = {"signals_url": str(signals_file), "channel": "outbox", "min_severity": "high"}
    r = run_payload(ROOT, params, error_handling=FAST, tmp_root=sb)
    assert r.record["status"] == "success" and r.record["result"]["sent"] == 2
    lines = (r.home / "data" / "automation-notifier" / "outbox.jsonl").read_text().splitlines()
    assert {json.loads(l)["signal"] for l in lines} == {"s1", "s3"}
    assert "Target portal changed login" in "".join(lines)
    # same sandbox -> state remembered -> nothing new, quiet run
    r2 = run_payload(ROOT, params, error_handling=FAST, tmp_root=sb)
    assert r2.record["result"]["sent"] == 0


def test_teams_webhook_receives_card(signals_file):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = run_payload(ROOT, {"signals_url": str(signals_file), "channel": "teams", "min_severity": "critical"}, error_handling=FAST,
                        env={"NOTIFIER_WEBHOOK_URL": f"http://127.0.0.1:{srv.server_port}/hook"})
    finally:
        srv.shutdown()
    assert r.record["status"] == "success" and len(got) == 1
    assert got[0]["title"] == "[CRITICAL] MISSED - automation-hello-world"


def test_missing_webhook_is_a_logic_error_not_a_retry_storm(signals_file):
    r = run_payload(ROOT, {"signals_url": str(signals_file), "channel": "teams", "min_severity": "critical"}, error_handling=FAST)
    assert r.record["status"] == "failed" and r.record["items"]["failed"] == 1
    assert r.record["error"]["category"] == "LOGIC_ERROR"


def test_email_channel_uses_smtp():
    import main as notifier
    with mock.patch("smtplib.SMTP") as smtp:
        notifier.send_email("T", "B", "ap-team@example.com", {"smtp_host": "smtp.example.internal", "mail_from": "autostation@example.com"})
    msg = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
    assert msg["To"] == "ap-team@example.com" and msg["Subject"] == "T"


def test_resolved_signals_are_marked(signals_file):
    sb = signals_file.parent / "sb2"
    run_payload(ROOT, {"signals_url": str(signals_file), "min_severity": "high"}, error_handling=FAST, tmp_root=sb)
    signals_file.write_text(json.dumps({"signals": []}))
    r = run_payload(ROOT, {"signals_url": str(signals_file), "min_severity": "high"}, error_handling=FAST, tmp_root=sb)
    assert r.record["result"]["resolved"] == 2
