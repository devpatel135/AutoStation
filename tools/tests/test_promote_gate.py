from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import promote_gate as pg  # noqa: E402

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def iso(dt):
    return pg.iso(dt)


def manifest(dev="1.1.0", pilot="1.0.0", broad="1.0.0", status="active", fleet_paused=False):
    vs = {v: {"url": f"artifacts/automations/automation-x-{v}.zip", "sha256": "0" * 64} for v in {dev, pilot, broad}}
    return {"fleet": {"paused": fleet_paused}, "rings": ["dev", "pilot", "broad"],
            "components": {"wrapper-core": {"rings": {"dev": "0.7.0", "pilot": "0.7.0", "broad": "0.7.0"}, "versions": {"0.7.0": {"url": "u", "sha256": "0" * 64}}}},
            "automations": {"automation-x": {"status": status, "paused_reason": "ops" if status == "paused" else "",
                                             "rings": {"dev": dev, "pilot": pilot, "broad": broad}, "versions": vs}}}


def report(signals=(), age_h=0.5, newest_h=1.0, loaded=500):
    return {"generated_at": iso(NOW - age_h * H), "signals": list(signals),
            "data_quality": {"records_loaded": loaded, "skipped_malformed": 0, "newest_record_age_h": newest_h, "signal_errors": []}}


def state_seen(**seen):
    st = pg.empty_state()
    st["first_seen"] = {k.replace("__", "|").replace("_", "."): v for k, v in seen.items()}
    return st


def seen_for(name, ring, ver, hours_ago):
    return {f"{name}|{ring}|{ver}": iso(NOW - hours_ago * H)}


def run(m, st=None, rep="default", issues=(), **kw):
    st = st or pg.empty_state()
    rep = report() if rep == "default" else rep
    return pg.plan(m, st, rep, list(issues), NOW, **kw)


def names(res, key="promotions"):
    return [(p["name"], p["to"]) for p in res[key]]


def test_first_sight_starts_the_soak_clock_and_waits():
    res = run(manifest())
    assert not res["promotions"]
    assert names(res, "waiting") == [("automation-x", "pilot")]
    assert res["state"]["first_seen"]["automation-x|dev|1.1.0"] == iso(NOW)


def test_dev_to_pilot_after_the_soak_when_healthy():
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 7)
    res = run(manifest(), st)
    assert names(res) == [("automation-x", "pilot")] and res["promotions"][0]["version"] == "1.1.0"


def test_soak_not_yet_over_waits():
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 5)
    res = run(manifest(), st)
    assert not res["promotions"] and "soaking" in res["waiting"][0]["reason"]


def test_broad_needs_the_longer_soak_in_pilot():
    m = manifest(dev="1.1.0", pilot="1.1.0", broad="1.0.0")
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "pilot", "1.1.0", 23)
    assert not run(m, st)["promotions"]
    st["first_seen"] = seen_for("automation-x", "pilot", "1.1.0", 25)
    assert names(run(m, st)) == [("automation-x", "broad")]


def test_only_one_ring_per_run_per_package_furthest_first():
    m = manifest(dev="1.2.0", pilot="1.1.0", broad="1.0.0")
    st = pg.empty_state()
    st["first_seen"] = {**seen_for("automation-x", "dev", "1.2.0", 30), **seen_for("automation-x", "pilot", "1.1.0", 30)}
    res = run(m, st)
    assert [(p["to"], p["version"]) for p in res["promotions"] if p["name"] == "automation-x"] == [("broad", "1.1.0")]


def test_a_rollback_that_left_dev_older_than_pilot_is_not_promoted():
    assert pg.candidate({"rings": {"dev": "1.0.0", "pilot": "1.1.0", "broad": "1.1.0"}}) is None


@pytest.mark.parametrize("typ", ["FAILED", "HUNG", "REGRESSION_AFTER_RELEASE", "ERROR_BUDGET_BURN"])
def test_health_signals_for_the_automation_block(typ):
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 30)
    res = run(manifest(), st, report([{"type": typ, "automation": "automation-x", "severity": "high"}]))
    assert not res["promotions"] and typ in res["blocked"][0]["reason"]


def test_a_regression_on_another_version_does_not_block_this_one_but_the_same_version_does():
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 30)
    other = {"type": "REGRESSION_AFTER_RELEASE", "automation": "automation-x", "version": "1.0.0"}
    same = {"type": "REGRESSION_AFTER_RELEASE", "automation": "automation-x", "version": "1.1.0"}
    assert names(run(manifest(), st, report([other])))
    assert not run(manifest(), st, report([same]))["promotions"]


def test_signals_of_other_automations_do_not_block():
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 30)
    assert names(run(manifest(), st, report([{"type": "FAILED", "automation": "automation-y"}])))


def test_machine_unhealthy_holds_back_components_only():
    m = manifest()
    m["components"]["wrapper-core"]["rings"] = {"dev": "0.8.0", "pilot": "0.7.0", "broad": "0.7.0"}
    m["components"]["wrapper-core"]["versions"]["0.8.0"] = {"url": "u", "sha256": "0" * 64}
    st = pg.empty_state()
    st["first_seen"] = {**seen_for("automation-x", "dev", "1.1.0", 30), **seen_for("wrapper-core", "dev", "0.8.0", 30)}
    res = run(m, st, report([{"type": "MACHINE_UNHEALTHY", "automation": None, "host": "pc-1"}]))
    assert names(res) == [("automation-x", "pilot")]
    assert names(res, "blocked") == [("wrapper-core", "pilot")]


@pytest.mark.parametrize("rep", [None, report(age_h=9), report(newest_h=500), report(loaded=0)])
def test_no_stale_or_empty_report_ever_promotes(rep):
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 30)
    res = run(manifest(), st, rep)
    assert not res["promotions"] and res["blocked"] and not res["healthy"]


def test_an_untrusted_report_with_a_failed_signal_builder_blocks():
    rep = report()
    rep["data_quality"]["signal_errors"] = ["boom"]
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 30)
    assert not run(manifest(), st, rep)["promotions"]


def test_open_ops_issue_naming_the_automation_blocks_and_closed_ones_do_not():
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 30)
    open_issue = {"number": 7, "state": "open", "title": "[SILENT] automation-x on pc-1", "body": ""}
    assert not run(manifest(), st, issues=[open_issue])["promotions"]
    assert names(run(manifest(), st, issues=[{**open_issue, "state": "closed"}]))
    unrelated = {"number": 8, "state": "open", "title": "[SILENT] automation-xy", "body": ""}
    assert names(run(manifest(), st, issues=[unrelated]))          # name match is whole-token


def test_an_open_pipeline_alert_blocks_everything():
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 30)
    pipe = {"number": 3, "state": "open", "title": "[PIPELINE] release failed", "body": "<!-- autostation-signal:pipe-release -->"}
    res = run(manifest(), st, issues=[pipe])
    assert not res["promotions"] and "pipeline alert" in res["blocked"][0]["reason"]


def test_paused_automation_and_paused_fleet_are_never_promoted():
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 30)
    assert "paused" in run(manifest(status="paused"), st)["blocked"][0]["reason"]
    assert "fleet is paused" in run(manifest(fleet_paused=True), st)["blocked"][0]["reason"]


def test_blocked_for_a_day_becomes_stuck_and_recovers():
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 60)
    st["blocked_since"] = {"automation-x|pilot|1.1.0": iso(NOW - 25 * H)}
    rep = report([{"type": "FAILED", "automation": "automation-x"}])
    res = run(manifest(), st, rep)
    assert res["stuck_ids"] == ["promote-stuck-automation-x"] and res["stuck"][0]["blocked_h"] >= 24
    assert res["state"]["stuck_alerts"] == ["promote-stuck-automation-x"]
    again_ok = run(manifest(), {**st, "stuck_alerts": ["promote-stuck-automation-x"]}, report())
    assert again_ok["recovered"] == ["promote-stuck-automation-x"] and not again_ok["stuck"]


def test_a_fresh_block_is_not_stuck_and_state_forgets_versions_that_left_the_rings():
    st = pg.empty_state()
    st["first_seen"] = {**seen_for("automation-x", "dev", "1.1.0", 30), "automation-x|dev|0.1.0": iso(NOW - 900 * H)}
    res = run(manifest(), st, report([{"type": "FAILED", "automation": "automation-x"}]))
    assert not res["stuck"] and "automation-x|dev|0.1.0" not in res["state"]["first_seen"]


def test_state_file_roundtrip_and_garbage_is_ignored(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json", encoding="utf-8")
    assert pg.load_state(p) == pg.empty_state()
    st = pg.empty_state()
    st["first_seen"] = {"a|dev|1": "2026-01-01T00:00:00Z"}
    p.write_text(json.dumps(st), encoding="utf-8")
    assert pg.load_state(p)["first_seen"] == st["first_seen"]
    assert pg.load_state(tmp_path / "missing.json") == pg.empty_state()


def test_apply_promotes_through_the_registry_ops_and_writes_history(tmp_path):
    root = Path(__file__).resolve().parents[2]
    src = root / "components" / "automation-registry" / "manifest.yaml"
    m = tmp_path / "manifest.yaml"
    shutil.copy2(src, m)
    sys.path.insert(0, str(root / "components" / "automation-registry"))
    from registry import store
    doc = store.load_manifest(m)
    auto = next(iter(doc["automations"]))
    entry = doc["automations"][auto]
    cur = entry["rings"]["broad"]
    newer = "99.0.0"
    entry["versions"][newer] = {**entry["versions"][cur], "url": entry["versions"][cur]["url"].replace(cur, newer)}
    entry["rings"] = {"dev": newer, "pilot": cur, "broad": cur}
    store.save_manifest(m, doc, allow_placeholders=True)
    st = pg.empty_state()
    st["first_seen"] = {f"{auto}|dev|{newer}": iso(NOW - 30 * H)}
    for c, e in doc["components"].items():
        for r, v in e["rings"].items():
            st["first_seen"][f"{c}|{r}|{v}"] = iso(NOW - 99 * H)
    for a, e in doc["automations"].items():
        for r, v in e["rings"].items():
            st["first_seen"].setdefault(f"{a}|{r}|{v}", iso(NOW - 99 * H))
    res = pg.plan(store.load_manifest(m), st, report(), [], NOW)
    done = pg.apply_promotions(m, res["promotions"])
    assert done == [f"{auto} {newer} -> pilot"]
    assert store.load_manifest(m)["automations"][auto]["rings"] == {"dev": newer, "pilot": newer, "broad": cur}
    assert any(h["action"] == "promote" and "promote-gate" in h["detail"] for h in store.read_history(m))


def test_cli_dry_run_decides_without_writing(tmp_path, capsys):
    root = Path(__file__).resolve().parents[2]
    m = tmp_path / "manifest.yaml"
    shutil.copy2(root / "components" / "automation-registry" / "manifest.yaml", m)
    before = m.read_text(encoding="utf-8")
    rep = tmp_path / "r.json"
    rep.write_text(json.dumps(report()), encoding="utf-8")
    out = tmp_path / "d.json"
    assert pg.main(["--manifest", str(m), "--state", str(tmp_path / "state.json"), "--report", str(rep), "--out", str(out)]) == 0
    assert m.read_text(encoding="utf-8") == before and not (tmp_path / "state.json").exists()
    assert json.loads(out.read_text(encoding="utf-8"))["applied"] == []


def test_cli_refuses_an_unreadable_issues_file(tmp_path):
    root = Path(__file__).resolve().parents[2]
    m = tmp_path / "manifest.yaml"
    shutil.copy2(root / "components" / "automation-registry" / "manifest.yaml", m)
    bad = tmp_path / "issues.json"
    bad.write_text("nope", encoding="utf-8")
    assert pg.main(["--manifest", str(m), "--state", str(tmp_path / "s.json"), "--issues", str(bad)]) == 2


def test_the_gates_own_stuck_issue_does_not_keep_the_package_blocked():
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 30)
    own = {"number": 9, "state": "open", "title": "Promotion stuck: automation-x 1.1.0 cannot move to pilot",
           "body": "<!-- autostation-signal:promote-stuck-automation-x -->"}
    assert names(run(manifest(), st, issues=[own]))


def test_only_limits_the_decision_to_one_package():
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 30)
    assert not pg.plan(manifest(), st, report(), [], NOW, only="wrapper-core")["promotions"]
    assert names(pg.plan(manifest(), st, report(), [], NOW, only="automation-x"))


def test_fetch_failure_stops_the_cli(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    m = tmp_path / "manifest.yaml"
    shutil.copy2(root / "components" / "automation-registry" / "manifest.yaml", m)
    monkeypatch.setattr(pg, "fetch_open_issues", lambda repo, token: (_ for _ in ()).throw(RuntimeError("HTTP 502")))
    assert pg.main(["--manifest", str(m), "--state", str(tmp_path / "s.json"), "--fetch-issues", "o/r"]) == 2


def test_only_delivery_pipeline_alerts_block():
    st = pg.empty_state()
    st["first_seen"] = seen_for("automation-x", "dev", "1.1.0", 30)
    for pid, blocks in (("pipe-site-verify", True), ("pipe-release", True), ("pipe-site", True), ("pipe-ci", False), ("pipe-backup-registry", False),
                        ("pipe-promote", False)):
        issue = {"number": 4, "state": "open", "title": "[PIPELINE] x", "body": f"<!-- autostation-signal:{pid} -->"}
        assert bool(run(manifest(), st, issues=[issue])["promotions"]) is (not blocks), pid
