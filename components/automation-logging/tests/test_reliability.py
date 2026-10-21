"""Reliability / error / remediation analytics: every expectation below is worked out by hand in the comment next to it."""
from __future__ import annotations

import json
import math
from datetime import timedelta

import pytest
from factories import NOW, T, by_type, data_of, manifest, run_record, signals_of

from automation_logging import analytics, reliability as rel
from automation_logging.validate import validate_record

ERR = {"category": "NETWORK_ERROR", "type": "RuntimeError", "message": "timeout after 30s calling https://x.example/a/1"}


def run(status="success", hours_ago=1.0, **kw):
    return run_record(kw.pop("automation", "auto-a"), NOW - timedelta(hours=hours_ago), status=status,
                      error=ERR if status in ("failed", "hung", "self_heal_failed") and "error" not in kw else kw.pop("error", None), **kw)


# ------------------------------------------------------------------------------------------------------ budget / burn
def test_error_budget_hand_computed():
    # 100 runs, 3 failed, SLO 95%: allowed = 0.05 * 100 = 5 failures, consumed = 3/5 = 0.6, remaining = 0.4
    runs = [run("failed" if i < 3 else "success", hours_ago=2 + i * 0.1) for i in range(100)]
    b = rel.error_budget(runs, NOW, 0.95, 30)
    assert (b["runs"], b["failed"], b["allowed_failures"], b["consumed"], b["remaining"]) == (100, 3, 5.0, 0.6, 0.4)
    # 20 runs, 2 failed: allowed = 1, consumed = 2, remaining = -1 (exhausted)
    runs = [run("failed" if i < 2 else "success", hours_ago=2 + i) for i in range(20)]
    assert rel.error_budget(runs, NOW, 0.95, 30)["remaining"] == -1.0
    assert rel.error_budget([], NOW, 0.95, 30)["remaining"] is None


def test_paused_and_maintenance_runs_are_not_counted():
    runs = [run("success", 5), run("halted_paused", 4), run("skipped_maintenance", 3), run("failed", 2, slot="adhoc")]
    assert rel.error_budget(runs, NOW, 0.95, 30)["runs"] == 1


def test_burn_rate():
    # last 24 h: 4 runs, 2 failed -> failure rate 0.5; burn = 0.5 / (1 - 0.95) = 10
    runs = [run("failed", 1), run("success", 2), run("failed", 3), run("success", 4), run("success", 40)]
    assert rel.burn_rate(runs, NOW, 24, 0.95, 3) == 10.0
    assert rel.burn_rate(runs[:2], NOW, 24, 0.95, 3) is None          # fewer than min_runs


def test_slo_override_is_bounds_checked():
    cfg = rel.DEFAULTS
    assert rel.slo_for({"catalog": {"slo": {"target": 99.5, "window_days": 7}}}, cfg) == {"target": 0.995, "window_days": 7}
    assert rel.slo_for({"slo": {"target": 0.1, "window_days": 100000}}, cfg) == {"target": 0.95, "window_days": 30}
    assert rel.slo_for({"slo": {"target": "x"}}, cfg)["target"] == 0.95 and rel.slo_for(None, cfg)["window_days"] == 30


# ------------------------------------------------------------------------------------------------------ MTTR / MTBF
def test_mttr_and_mtbf_hand_computed():
    # one machine, hourly: ok@0 fail@1 fail@2 ok@4 ok@5 fail@8 ok@9   (hours after t0)
    t0 = 20
    seq = [("success", 0), ("failed", 1), ("failed", 2), ("success", 4), ("success", 5), ("failed", 8), ("success", 9)]
    runs = [run(s, hours_ago=t0 - h) for s, h in seq]
    eps = rel.episodes(runs)
    assert [(e["runs"], e["end"] is not None) for e in eps] == [(2, True), (1, True)]
    m = rel.mttr_mtbf(eps)
    # episode 1: start 1h, recovered 4h -> 3 h = 10800 s ; episode 2: start 8h, recovered 9h -> 1 h = 3600 s ; MTTR = 7200 s
    # MTBF = episode2.start - episode1.recovery = 8h - 4h = 4 h = 14400 s
    assert (m["mttr_s"], m["mtbf_s"], m["episodes"], m["open"]) == (7200.0, 14400.0, 2, 0)


def test_open_episode_has_no_mttr_and_machines_do_not_hide_each_other():
    runs = [run("failed", 5, host="A"), run("success", 4, host="B"), run("failed", 3, host="A"), run("success", 2, host="B")]
    eps = rel.episodes(runs)
    assert len(eps) == 1 and eps[0]["host"] == "A" and eps[0]["end"] is None
    m = rel.mttr_mtbf(eps)
    assert m["mttr_s"] is None and m["open"] == 1


def test_streaks():
    runs = [run("failed", 9), run("failed", 8), run("failed", 7), run("success", 6), run("failed", 5), run("failed", 4)]
    assert rel.streaks(runs) == {"current_failures": 2, "max_failures": 3}


# ------------------------------------------------------------------------------------------------------------ flakiness
def test_flakiness():
    alt = [run("failed" if i % 2 else "success", hours_ago=20 - i) for i in range(8)]
    f = rel.flakiness(alt)
    assert (f["flips"], f["score"], f["flaky"]) == (7, 1.0, True)                           # 7 flips / 7 gaps
    steady = [run("success", 20 - i) for i in range(8)]
    assert rel.flakiness(steady)["flaky"] is False
    one_flip = [run("success", 20 - i) for i in range(5)] + [run("failed", 14), run("failed", 13)]
    assert rel.flakiness(one_flip)["flaky"] is False                                        # a single regression is not flakiness


# ------------------------------------------------------------------------------------------------------ duration anomaly
def test_duration_anomaly_hand_computed():
    base = [1000, 1010, 990, 1005, 995, 1000, 1002, 998, 1001, 999]
    # sorted base: 990 995 998 999 1000 1000 1001 1002 1005 1010 -> median 1000; |dev| sorted 0 0 1 1 2 2 5 5 10 10 -> MAD 2;
    # scale = max(MAD, 5% of median, 1) = 50 ; 5000 ms -> z = 0.6745 * 4000 / 50 = 53.96 (>= 3.5, 5x the median) -> slow anomaly
    runs = [run(hours_ago=40 - i, duration_ms=d) for i, d in enumerate(base)] + [run(hours_ago=1, duration_ms=5000, run_id="slow-one")]
    an = rel.duration_anomalies(runs)
    assert [a["run_id"] for a in an] == ["slow-one"] and an[0]["z"] == pytest.approx(53.96, abs=0.01) and an[0]["direction"] == "slow"
    ok = runs[:-1] + [run(hours_ago=1, duration_ms=1100)]                                    # z = 0.6745 * 100 / 50 = 1.35: normal
    assert rel.duration_anomalies(ok) == []
    assert rel.duration_anomalies(runs[:5]) == []                                           # not enough baseline


def test_on_time_rate():
    t = NOW.replace(hour=9, minute=0)
    exp = [("daily", t - timedelta(days=2)), ("daily", t - timedelta(days=1)), ("daily", t)]
    runs = [run_record("auto-a", t - timedelta(days=2) + timedelta(minutes=1)),          # on time
            run_record("auto-a", t - timedelta(days=1) + timedelta(hours=3))]            # 3 h late (grace 15 min) ; today's never ran
    r = rel.on_time(exp, runs, timedelta(minutes=15), NOW)
    assert (r["expected"], r["on_time"], r["late"], r["missed"]) == (3, 1, 1, 1) and r["rate"] == pytest.approx(1 / 3, abs=1e-4)


# ------------------------------------------------------------------------------------------------------------ taxonomy
def test_error_signature_normalises_noise_but_keeps_meaning():
    a = rel.error_signature("NETWORK_ERROR", "Timeout after 30s at C:\\Users\\alice\\x.js:12 calling https://a.example/r/17 (id 0xdeadbeef)")
    b = rel.error_signature("NETWORK_ERROR", "Timeout after 45s at C:\\Users\\bob\\y.js:99 calling https://b.example/r/23 (id 0xcafe)")
    c = rel.error_signature("NETWORK_ERROR", "Connection refused by peer")
    d = rel.error_signature("AUTH_EXPIRED", "Timeout after 30s at C:\\Users\\alice\\x.js:12 calling https://a.example/r/17 (id 0xdeadbeef)")
    assert a == b and a != c and a != d


def test_taxonomy_counts_and_first_last_seen():
    runs = [run("failed", 30, host="A"), run("failed", 20, host="B"), run("success", 10),
            run("failed", 5, error={"category": "AUTH_EXPIRED", "message": "session expired"})]
    tx = rel.error_taxonomy(runs, NOW)
    assert tx["categories"] == {"NETWORK_ERROR": 2, "AUTH_EXPIRED": 1} and tx["failures"] == 3 and tx["signature_total"] == 2
    top = tx["signatures"][0]
    assert top["count"] == 2 and top["hosts"] == ["A", "B"] and top["first_seen"] < top["last_seen"] and sum(top["trend"]) == 2


# ------------------------------------------------------------------------------------------------------- regressions
def test_regression_after_release_and_the_old_problem_that_is_not_one():
    old = [run("success", 100 - i, version="1.0.0") for i in range(4)]
    bad = [run("failed", 40, version="1.1.0"), run("failed", 30, version="1.1.0")]        # new version + new error within 48 h
    rg = rel.regressions(old + bad)
    assert len(rg) == 1 and rg[0]["version"] == "1.1.0" and rg[0]["previous_version"] == "1.0.0" and rg[0]["count"] == 2
    seen_before = [run("failed", 90, version="1.0.0")]                                     # same error already on 1.0.0
    assert rel.regressions(old + seen_before + bad) == []
    assert rel.regressions([run("failed", 40, version="1.0.0")]) == []                     # first version ever: no baseline


# ------------------------------------------------------------------------------------------------------------ machines
def test_machine_correlation_flags_the_sick_machine():
    runs = []
    for i in range(4):
        runs.append(run("failed", 10 - i, host="SICK", automation="auto-a"))
        runs.append(run("failed", 10 - i - 0.5, host="SICK", automation="auto-b"))
        runs.append(run("success", 10 - i - 0.2, host="FINE", automation="auto-a"))
    mc = rel.machine_correlation(runs, NOW)
    sick = next(h for h in mc["hosts"] if h["host"] == "SICK")
    fine = next(h for h in mc["hosts"] if h["host"] == "FINE")
    assert sick["unhealthy"] is True and sick["fail_rate"] == 1.0 and fine["unhealthy"] is False


# ------------------------------------------------------------------------------------------------------- remediation
def attempt(playbook, outcome, category="NETWORK_ERROR", detail="did a thing"):
    return {"ts": "2026-10-14T10:00:00Z", "event": "remediation_attempt",
            "data": {"playbook": playbook, "category": category, "action": "x", "outcome": outcome, "detail": detail, "duration_ms": 5, "attempt": 1}}


def test_remediation_stats_hand_computed():
    runs = [run("success", 5, extra_events=[attempt("restart-browser", "fixed"), attempt("restart-browser", "fixed"), attempt("restart-browser", "not_fixed")]),
            run("failed", 4, extra_events=[attempt("refresh-session", "escalated", "AUTH_EXPIRED"), attempt("refresh-session", "recommended", "AUTH_EXPIRED", "sign in once")]),
            ]
    runs[1]["remediation"] = {"category": "AUTH_EXPIRED", "fixed": False, "playbooks": ["refresh-session"]}
    st = rel.remediation_stats(runs)
    rb = next(p for p in st["playbooks"] if p["playbook"] == "restart-browser")
    assert (rb["attempts"], rb["fixed"], rb["not_fixed"], rb["fix_rate"]) == (3, 2, 1, round(2 / 3, 4))     # 2 fixed of 3 tried
    rs = next(p for p in st["playbooks"] if p["playbook"] == "refresh-session")
    assert rs["escalated"] == 1 and rs["recommended"] == 1 and rs["fix_rate"] is None                    # nothing was tried to completion
    assert st["unfixed_runs"] == 1 and st["needs_human"][0]["category"] == "AUTH_EXPIRED" and "Refresh" in st["needs_human"][0]["runbook"]
    assert st["recommended"][0]["detail"] == "sign in once"


def test_remediation_ignores_unknown_outcomes():
    st = rel.remediation_stats([run("success", 2, extra_events=[attempt("p", "exploded"), {"event": "remediation_attempt", "data": "nope"}])])
    assert st["playbooks"] == []


# ---------------------------------------------------------------------------------------------------- manual steps
def test_manual_step_streak_clears_on_a_normal_run():
    manual = {"ts": "2026-10-14T01:00:00Z", "event": "manual_step_required", "data": {"reason": "signin"}}
    runs = [run("success", 50, extra_events=[manual]), run("success", 40, extra_events=[manual]), run("success", 30, result={"state": "manual_step_required"})]
    st = rel.manual_step_streaks(runs)
    assert len(st) == 1 and st[0]["runs"] == 3
    assert rel.manual_step_streaks(runs + [run("success", 2)]) == []


# ------------------------------------------------------------------------------------------------ signals end to end
def types_of(runs, mf=None, **cfg):
    sigs, _, _ = signals_of(data_of(*runs), mf or manifest(), **cfg)
    return sigs


def test_error_budget_burn_signal_fast_and_resolves():
    runs = [run("success", 100 - i) for i in range(10)] + [run("failed", 5), run("failed", 4), run("success", 3), run("failed", 2)]
    s = by_type(types_of(runs), "ERROR_BUDGET_BURN")
    assert len(s) == 1 and s[0]["severity"] == "high" and s[0]["automation"] == "auto-a" and s[0]["evidence"]
    healthy = [run("success", 100 - i) for i in range(14)]
    assert by_type(types_of(healthy), "ERROR_BUDGET_BURN") == []


def test_flaky_signal():
    runs = [run("failed" if i % 2 else "success", hours_ago=30 - i) for i in range(10)]
    assert len(by_type(types_of(runs), "FLAKY")) == 1


def test_regression_signal_and_alert_text_is_sanitised():
    from automation_logging import alerting
    runs = [run("success", 100 - i, version="1.0.0") for i in range(4)] + [
        run("failed", 3, version="1.1.0", error={"category": "LOGIC_ERROR", "message": "boom <script>alert(1)</script>\n@everyone"})]
    s = by_type(types_of(runs), "REGRESSION_AFTER_RELEASE")
    assert len(s) == 1 and s[0]["severity"] == "high"
    a = alerting.build_alert(s[0], "open", NOW)
    assert "<script" not in json.dumps(a) and "\n@everyone" not in a["detail"]


def test_persistent_manual_step_signal():
    manual = {"ts": "2026-10-13T01:00:00Z", "event": "manual_step_required", "data": {}}
    runs = [run("success", 40, extra_events=[manual]), run("success", 20, extra_events=[manual]), run("success", 1, extra_events=[manual])]
    s = by_type(types_of(runs), "PERSISTENT_MANUAL_STEP")
    assert len(s) == 1 and s[0]["host"] == "VDI-0001"
    assert by_type(types_of(runs[:1] + [run("success", 1)]), "PERSISTENT_MANUAL_STEP") == []


def test_machine_unhealthy_and_remediation_failing_signals():
    runs = []
    for i in range(4):
        runs.append(run("failed", 10 - i, host="SICK", automation="auto-a"))
        runs.append(run("failed", 10 - i - 0.5, host="SICK", automation="auto-b"))
    runs.append(run("success", 3, host="FINE"))
    assert len(by_type(types_of(runs), "MACHINE_UNHEALTHY")) == 1
    bad = [run("failed", 9 - i, extra_events=[attempt("restart-browser", "not_fixed")]) for i in range(4)]
    s = by_type(types_of(bad), "REMEDIATION_FAILING")
    assert len(s) == 1 and s[0]["playbook"] == "restart-browser"


def test_paused_automation_raises_no_observability_signals():
    runs = [run("failed", 5), run("failed", 4), run("success", 3), run("failed", 2)]
    s = types_of(runs, manifest(status="paused"))
    assert not [x for x in s if x["type"] in ("ERROR_BUDGET_BURN", "FLAKY", "REGRESSION_AFTER_RELEASE")]


def test_signal_failure_never_breaks_compute(monkeypatch):
    monkeypatch.setattr(rel, "extra_signals", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    sigs, _, st = signals_of(data_of(run("failed", 2)), manifest())
    assert by_type(sigs, "FAILED") and "boom" in st["extra_signals_error"]


# --------------------------------------------------------------------------------------------- hostile input / validation
def good(**kw):
    r = run("success", 2, **kw)
    return r


@pytest.mark.parametrize("mutate,expect", [
    (lambda r: r["events"].append({"ts": "2026-10-14T10:00:00Z", "event": "remediation_attempt", "data": {"outcome": "pwned"}}), "outcome"),
    (lambda r: r["events"].append({"ts": "2026-10-14T10:00:00Z", "event": "remediation_attempt", "data": {"outcome": "fixed", "detail": "x" * 100000}}), "detail"),
    (lambda r: r["events"].append({"ts": "2026-10-14T10:00:00Z", "event": "remediation_attempt", "data": {"outcome": "fixed", "attempt": float("nan")}}), "attempt"),
    (lambda r: r["events"].append({"ts": "2026-10-14T10:00:00Z", "event": "remediation_attempt", "data": {"outcome": "fixed", "duration_ms": -5}}), "duration_ms"),
    (lambda r: r.update(remediation="nope"), "remediation"),
    (lambda r: r.update(remediation={"fixed": "yes"}), "fixed"),
    (lambda r: r.update(remediation={"playbooks": ["a"] * 1000}), "playbooks"),
    (lambda r: r.update(remediation={"playbooks": [{"x": 1}]}), "playbooks"),
])
def test_validate_rejects_hostile_remediation_records(mutate, expect):
    r = good()
    mutate(r)
    why = validate_record(r, "run")
    assert why and expect in why


def test_validate_accepts_well_formed_remediation():
    r = good()
    r["events"].append({"ts": "2026-10-14T10:00:00Z", "event": "remediation_attempt", "data": attempt("p", "fixed")["data"]})
    r["remediation"] = {"category": "NETWORK_ERROR", "fixed": True, "playbooks": ["p"]}
    assert validate_record(r, "run") is None


def test_reliability_functions_survive_hostile_values():
    r = run("failed", 3, error={"category": "X" * 10000, "message": "\x00\x1b[31m" + "y" * 100000})
    r["duration_ms"] = 10 ** 14
    tx = rel.error_taxonomy([r], NOW)
    assert len(tx["signatures"][0]["sample"]) <= 300 and "\x00" not in tx["signatures"][0]["sample"]
    assert math.isfinite(rel.error_budget([r], NOW, 0.95, 30)["success_rate"] or 0)
    assert rel.remediation_stats([r])["playbooks"] == []
