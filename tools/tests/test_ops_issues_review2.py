"""Second review round for tools/ops_issues.py: no flapping, no closing on bad data, no spam, no autolinks."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ops_issues as oi  # noqa: E402
from test_ops_issues import NOW, issue, sig  # noqa: E402

GOOD_DQ = {"skipped_malformed": 0, "records_loaded": 40, "newest_record_age_h": 1.5, "signal_errors": [], "extra_signals_error": None}


def report(signals=None, **dq):
    return {"signals": signals if signals is not None else [], "data_quality": dict(GOOD_DQ, **dq)}


# ---- 3: a report only closes issues when it can prove an all-clear ---------------------------------------------------------------
@pytest.mark.parametrize("doc,why", [
    ({}, "no signals list"),
    ({"signals": []}, "no data_quality"),
    (report(records_loaded=0), "only 0 record"),
    (report(records_loaded=2), "only 2 record"),
    (report(newest_record_age_h=200), "newest record"),
    (report(newest_record_age_h=None), "newest record"),
    (report(skipped_malformed=500), "malformed"),
    (report(signal_errors=["latest-run signals for x: OverflowError"]), "signal builder failed"),
    (report(extra_signals_error="boom"), "observability"),
    ("not a dict", "no signals list"),
])
def test_untrustworthy_reports_never_close_issues(doc, why):
    ok, reason = oi.report_trust(doc)
    assert not ok and why in reason


def test_a_healthy_report_is_trusted():
    assert oi.report_trust(report()) == (True, "ok")


# ---- 4: canary / manual issues are never auto-closed by the analytics sync -------------------------------------------------------
def test_canary_issue_is_not_closed_by_the_signal_sync():
    canary = issue(9, "canary-install", labels=("ops-signal", "severity:high", "signal:canary"))
    assert oi.plan([], [canary], NOW, may_close=True) == []
    unlabelled = issue(10, "manual-thing", labels=("ops-signal",))
    assert oi.plan([], [unlabelled], NOW, may_close=True) == []
    # ... but the canary's own `resolve` path can close it
    assert oi.plan([], [canary], NOW, may_close=True, closable_types=None)[0]["op"] == "mark_clearing"


# ---- 3/7: two consecutive clear checks before closing, and a returning signal cancels the countdown -------------------------------
def test_signal_that_returns_removes_the_clearing_label():
    marked = issue(7, "abc12345", labels=("ops-signal", "signal:silent", oi.CLEARING_LABEL))
    acts = oi.plan([sig("abc12345")], [marked], NOW, may_close=True)
    assert "unmark_clearing" in [a["op"] for a in acts] and not any(a["op"] in ("close", "mark_clearing") for a in acts)


# ---- 6: telemetry-only automations, hosts, caps, digest --------------------------------------------------------------------------
def test_signals_for_unregistered_automations_never_open_issues():
    ghost = oi.normalize_signal(dict(sig("ghost123"), registered=False))
    assert ghost["registered"] is False
    assert oi.plan([ghost], [], NOW) == []
    real = oi.normalize_signal(dict(sig("real1234"), registered=True))
    assert [a["op"] for a in oi.plan([real], [], NOW)] == ["create"]


@pytest.mark.parametrize("host", ["evil host\n# x", "<script>", "x" * 200, "a b", ""])
def test_hostile_hosts_are_dropped_from_signals(host):
    assert oi.normalize_signal(dict(sig("abc12345"), host=host))["host"] is None
    assert oi.normalize_signal(dict(sig("abc12345"), host="VDI-01.corp"))["host"] == "VDI-01.corp"


def test_open_issue_cap_rolls_the_overflow_into_one_digest_issue():
    existing = [issue(i, f"open{i:04d}") for i in range(1, 11)]                      # 10 already open
    new = [oi.normalize_signal(sig(f"new{i:05d}")) for i in range(8)]
    acts = oi.plan(new, existing, NOW, max_new=5, max_open=12)
    assert len([a for a in acts if a["op"] == "create"]) == 2                          # only the 2 free slots are used
    digest = [a for a in acts if a["op"] == "digest"]
    assert len(digest) == 1 and digest[0]["total"] == 6 and digest[0]["number"] is None


def test_digest_is_updated_in_place_and_closes_when_the_overflow_is_gone():
    digest_issue = issue(50, oi.DIGEST_FP, labels=("ops-signal", "signal:digest"))
    existing = [issue(i, f"open{i:04d}") for i in range(1, 11)]
    new = [oi.normalize_signal(sig(f"new{i:05d}")) for i in range(4)]
    acts = oi.plan(new, existing + [digest_issue], NOW, max_new=5, max_open=10)
    assert [a for a in acts if a["op"] == "digest"][0]["number"] == 50
    assert not any(a["op"] == "close" and a["number"] == 50 for a in acts)
    calm = oi.plan([], [digest_issue], NOW, max_open=10)
    assert [a["op"] for a in calm] == ["close"] and calm[0]["number"] == 50


# ---- 25: wontfix is honoured --------------------------------------------------------------------------------------------------------
def test_wontfix_blocks_reopening_and_re_raising():
    closed = issue(3, "abc12345", state="closed", closed="2026-10-15T00:00:00Z", labels=("ops-signal", "signal:silent", "wontfix"))
    assert oi.plan([sig("abc12345")], [closed], NOW) == []


def test_close_changes_state_before_commenting():
    calls = []

    class Fake:
        repo = "o/r"

        def set_state(self, n, state, reason="completed"):
            calls.append(("state", n))

        def comment(self, n, text):
            calls.append(("comment", n))

    oi.apply(Fake(), [{"op": "close", "number": 4, "fp": "x", "note": "n", "reason": "completed"}])
    assert calls == [("state", 4), ("comment", 4)]


# ---- 16: sanitiser: autolinks and cross references ------------------------------------------------------------------------------------
@pytest.mark.parametrize("payload", ["www.evil.example/login", "see owner/repo#5 now", "GH-12 fixed", "x#7", "visit WWW.evil.example"])
def test_sanitize_breaks_autolinks_and_references(payload):
    out = oi.sanitize(payload, 200)
    assert "www." not in out.lower() and "GH-12" not in out and "#5" not in out and "#7" not in out
    assert oi.ZW in out


# ---- listing is complete or it refuses -----------------------------------------------------------------------------------------------
def test_listing_refuses_to_act_on_a_partial_result():
    class Always(oi.GitHub):
        def request(self, method, path, body=None):
            return 200, [{"number": 1, "body": "x", "labels": []}] * 100          # every page is full: it never ends

    with pytest.raises(RuntimeError):
        Always("o/r", "t").list_ops_issues(max_pages=3)


def test_listing_reads_every_page():
    pages = {1: [{"number": i, "body": ""} for i in range(100)], 2: [{"number": 200 + i, "body": ""} for i in range(30)]}

    class Paged(oi.GitHub):
        def request(self, method, path, body=None):
            page = int(path.rsplit("page=", 1)[1])
            return 200, pages.get(page, [])

    assert len(Paged("o/r", "t").list_ops_issues()) == 130
