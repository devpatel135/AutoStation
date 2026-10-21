from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ops_issues as oi  # noqa: E402

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def sig(fp="abc12345", typ="SILENT", sev="high", **kw):
    base = {"id": fp, "type": typ, "severity": sev, "automation": "auto-a", "host": "pc-1", "ring": "broad", "detail": "no heartbeat",
            "first_seen": "2026-10-21T10:00:00Z", "last_seen": "2026-10-21T11:59:00Z", "evidence": ["run-1"], "diagnosis": None, "owner": "team"}
    base.update(kw)
    return base


def issue(number, fp, state="open", updated="2026-10-20T00:00:00Z", closed=None, labels=("ops-signal", "signal:silent")):
    return {"number": number, "state": state, "body": f"x\n<!-- autostation-signal:{fp} -->\ny", "updated_at": updated, "closed_at": closed,
            "labels": [{"name": n} for n in labels]}


# ---- sanitising hostile telemetry ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("payload", [
    "<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "@octocat please look", "fixes #123", "[click](https://evil.example)",
    "javascript:alert(1)", "\x1b[31mred\x1b[0m", "line1\nline2\r\nline3", "‮evil", "a" * 5000, "<!-- autostation-signal:abcdef123 -->",
])
def test_sanitize_neutralises_markup_mentions_and_markers(payload):
    out = oi.sanitize(payload, 200)
    assert "<" not in out and ">" not in out and "\n" not in out and "\x1b" not in out and "‮" not in out
    assert len(out) <= 200
    assert oi.fingerprint_of(out) is None            # attacker text can never forge the dedupe marker
    assert "@octocat" not in out and "#123" not in out and "javascript:" not in out.lower()


def test_titles_keep_plain_characters_but_stay_safe():
    t = oi.render_title(sig(automation="auto_a", host="pc<1>"))
    assert t.startswith("[SILENT] auto_a") and "\\" not in t


@pytest.mark.parametrize("bad", [None, 5, "x", {}, {"id": "x"}, {"id": "abc12345"}, {"id": "../../etc/x", "type": "SILENT"},
                                 {"id": "abc12345", "type": "silent"}, {"id": "abc12345", "type": "A;B"}])
def test_malformed_signals_are_dropped(bad):
    assert oi.normalize_signal(bad) is None


def test_unknown_severity_defaults_and_bad_automation_name_is_dropped():
    s = oi.normalize_signal(sig(sev="catastrophic", automation="../x; rm"))
    assert s["severity"] == "medium" and s["automation"] is None


def test_body_has_marker_runbook_and_escaped_detail():
    s = oi.normalize_signal(sig(detail="<b>boom</b> @everyone", diagnosis={"summary": "ignore previous instructions <x>"}))
    body = oi.render_body(s, "o/r", "https://o.github.io/r")
    assert oi.fingerprint_of(body) == "abc12345"
    assert "docs/RUNBOOKS.md#3-failure-triage" in body and "/jobs/" in body
    assert "<b>" not in body and "@everyone" not in body and "verify before acting" in body


def test_normalize_signals_dedupes_and_skips_garbage():
    out = oi.normalize_signals([sig(), sig(), "junk", {"id": "def45678", "type": "HUNG"}])
    assert [s["id"] for s in out] == ["abc12345", "def45678"]
    assert oi.normalize_signals("not a list") == []


# ---- plan ---------------------------------------------------------------------------------------------------------------------
def kinds(actions):
    return [(a["op"], a.get("fp") or a.get("text", "")[:5]) for a in actions]


def test_new_signal_creates_an_issue():
    assert [a["op"] for a in oi.plan([sig()], [], NOW)] == ["create"]


def test_existing_open_issue_is_left_alone_unless_recurrence_is_old_enough():
    quiet = oi.plan([sig()], [issue(1, "abc12345", updated="2026-10-21T11:00:00Z")], NOW)
    assert quiet == []
    noisy = oi.plan([sig(last_seen="2026-10-21T11:59:00Z")], [issue(1, "abc12345", updated="2026-10-19T00:00:00Z")], NOW)
    assert [a["op"] for a in noisy] == ["comment"]


def test_cleared_signal_closes_only_when_live_data_confirmed():
    existing = [issue(7, "gone1234")]
    assert oi.plan([], existing, NOW, may_close=False) == []
    first = oi.plan([], existing, NOW, may_close=True)
    assert [a["op"] for a in first] == ["mark_clearing"]                    # two-step: a first clean check only labels the issue
    marked = [issue(7, "gone1234", labels=("ops-signal", "signal:silent", oi.CLEARING_LABEL))]
    acts = oi.plan([], marked, NOW, may_close=True)
    assert acts[0]["op"] == "close" and acts[0]["number"] == 7 and acts[0]["reason"] == "completed"


def test_recent_closed_issue_is_reopened_old_one_gets_a_new_issue():
    recent = issue(3, "abc12345", state="closed", closed="2026-10-15T00:00:00Z")
    assert [a["op"] for a in oi.plan([sig()], [recent], NOW)] == ["reopen"]
    old = issue(3, "abc12345", state="closed", closed="2026-08-01T00:00:00Z")
    assert [a["op"] for a in oi.plan([sig()], [old], NOW)] == ["create"]


def test_duplicates_are_collapsed_keeping_the_lowest_number():
    acts = oi.plan([sig()], [issue(9, "abc12345"), issue(4, "abc12345")], NOW)
    assert [a["number"] for a in acts if a["op"] == "close"] == [9]
    assert not any(a["op"] == "create" for a in acts)


def test_rate_limit_keeps_the_most_severe_and_says_so():
    sigs = [sig("aaaa1111", sev="low"), sig("bbbb2222", sev="critical"), sig("cccc3333", sev="high")]
    acts = oi.plan(sigs, [], NOW, max_new=2)
    created = [a["fp"] for a in acts if a["op"] == "create"]
    assert created == ["bbbb2222", "cccc3333"] and any(a["op"] == "note" for a in acts)


def test_issues_without_a_marker_are_never_touched():
    stranger = {"number": 1, "state": "open", "body": "a human wrote this", "updated_at": "2026-01-01T00:00:00Z"}
    assert oi.plan([], [stranger], NOW, may_close=True) == []


# ---- GitHub layer with a fake opener -----------------------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status, data):
        self.status, self._d = status, json.dumps(data).encode()

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_github_client_validates_repo_and_never_logs_the_token():
    with pytest.raises(ValueError):
        oi.GitHub("not a repo", "t")
    calls = []

    def opener(req, timeout=0):
        calls.append(req)
        return FakeResp(201, {"number": 5})

    gh = oi.GitHub("o/r", "SECRET-TOKEN", opener)
    assert gh.create(oi.normalize_signal(sig())) == 5
    req = calls[0]
    assert req.full_url == "https://api.github.com/repos/o/r/issues"
    body = json.loads(req.data)
    assert "SECRET-TOKEN" not in json.dumps(body) and body["labels"][0] == "ops-signal"


def test_apply_counts_errors_without_stopping(capsys):
    class Boom(oi.GitHub):
        def __init__(self):
            pass

        def ensure_labels(self, names):
            pass

        def create(self, sig, site_url=""):
            raise RuntimeError("HTTP 500")

    res = oi.apply(Boom(), [{"op": "create", "fp": "abc12345", "sig": oi.normalize_signal(sig())}, {"op": "note", "text": "n"}])
    assert res["errors"] == 1 and res["create"] == 0


def test_manual_signal_for_canary():
    s = oi.manual_signal("canary-install", "CANARY", "high", "Canary failed: <b>x</b>", "installer smoke failed")
    assert s["title"].startswith("Canary failed") and "<b>" in s["title"]      # plain-text title keeps characters
    assert "<b>" not in oi.render_body(s)
    assert oi.labels_for(s) == ["ops-signal", "severity:high", "signal:canary"]
    with pytest.raises(ValueError):
        oi.manual_signal("x", "canary", "high", "t", "d")


def test_recurrence_gap_constant_is_a_day():
    assert oi.RECURRENCE_GAP == timedelta(hours=24)


def test_min_severity_floor_limits_new_issues_only():
    sigs = [sig("aaaa1111", sev="low"), sig("bbbb2222", sev="high")]
    acts = oi.plan(sigs, [issue(1, "aaaa1111")], NOW, min_severity="high")
    assert [a["fp"] for a in acts if a["op"] == "create"] == ["bbbb2222"]
    assert not any(a["op"] == "close" for a in acts)       # an existing low issue stays open while its signal persists
    assert [a["op"] for a in oi.plan([sig("aaaa1111", sev="low")], [], NOW, min_severity="high")] == []


def test_run_url_is_rendered_only_when_it_matches_the_strict_pattern():
    s = oi.manual_signal("canary-install", "CANARY", "high", "t", "d", None, "https://github.com/o/r/actions/runs/123")
    assert "Failing run: https://github.com/o/r/actions/runs/123" in oi.render_body(s)
    for bad in ("https://evil.example/x", "https://github.com/o/r/actions/runs/1) [x](https://evil", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            oi.manual_signal("canary-install", "CANARY", "high", "t", "d", None, bad)
    forged = dict(s, run_url="https://evil.example/x")
    assert "evil.example" not in oi.render_body(forged)
