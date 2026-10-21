from __future__ import annotations

import json

import pytest

from wrapper_core import testing
from wrapper_core.cli import main as cli_main

PAYLOAD = '''
import wrapper_core as wrapper

@wrapper.entrypoint
def main():
    mode = wrapper.param("mode", "none")
    wrapper.log_event("start", {"mode": mode})
    if mode == "boom":
        raise ValueError("bad data in row 3")
    for i in range(int(wrapper.param("n", "2"))):
        wrapper.log_item(f"I-{i}", "success", duration_ms=5)
    wrapper.log_item("I-skip", "skipped")
    wrapper.set_result(processed=int(wrapper.param("n", "2")), mode=mode, asset=wrapper.assets().get("greeting", "none"))

if __name__ == "__main__":
    raise SystemExit(main())
'''


@pytest.fixture
def payload(tmp_path):
    d = tmp_path / "payload"
    d.mkdir()
    (d / "automation.yaml").write_text("name: fixture-auto\nversion: 1.0.0\nruntime: python\nentrypoint: main.py\ntimeout_s: 60\n", encoding="utf-8")
    (d / "main.py").write_text(PAYLOAD, encoding="utf-8")
    return d


def test_run_payload_and_assertions(payload):
    run = testing.run_payload(payload, {"mode": "demo", "n": "3"})
    testing.assert_run(run, status="success", exit_code=0, result={"processed": 3, "mode": "demo"}, result_keys=["asset"],
                       items={"success": 3, "skipped": 1, "failed": 0}, events=[{"event": "start", "data": {"mode": "demo"}}, {"event": "nope", "min": 0, "max": 0}])


def test_failures_are_reported_precisely(payload):
    run = testing.run_payload(payload, {"mode": "boom"}, error_handling={"max_retries": 0})
    assert testing.check_expectations(run, {"status": "success"})
    fails = testing.check_expectations(run, {"status": ["failed", "partial"], "error_contains": "bad data", "items": {"failed": 2},
                                             "events": [{"event": "start", "min": 2}], "result": {"x": 1}, "max_duration_ms": 0})
    text = " | ".join(fails)
    assert "items['failed']" in text and "event 'start' seen 1x" in text and "result['x']" in text and "duration" in text
    with pytest.raises(AssertionError):
        testing.assert_run(run, status="success")


def test_asset_overrides_are_visible_to_the_payload(payload, monkeypatch):
    run = testing.run_payload(payload, {"asset_greeting": "hello"})
    assert run.record["result"]["asset"] == "hello"


def test_run_fixtures_reports_per_case_and_isolates_failures(payload):
    cases = [
        {"name": "happy", "params": {"mode": "demo", "n": 2}, "expect": {"status": "success", "result": {"processed": 2}}},
        {"name": "wrong expectation", "params": {"mode": "demo"}, "expect": {"result": {"processed": 99}}},
        {"name": "boom", "params": {"mode": "boom"}, "error_handling": {"max_retries": 0}, "expect": {"status": ["failed", "partial"]}},
        {"name": "dangerous env", "env": {"PATH": "x"}, "expect": {"status": "success"}},
        {"name": "default expectation", "params": {"mode": "demo"}},
    ]
    res = {r["name"]: r for r in testing.run_fixtures(payload, cases)}
    assert res["happy"]["ok"] and res["boom"]["ok"] and res["default expectation"]["ok"]
    assert not res["wrong expectation"]["ok"] and "processed" in res["wrong expectation"]["failures"][0]
    assert not res["dangerous env"]["ok"] and "not allowed" in res["dangerous env"]["failures"][0]


def test_test_env_is_restricted_to_safe_prefixes():
    assert testing._env_ok("AMX_ALLOW_TEST_HOSTS") and testing._env_ok("TEST_FLAG") and testing._env_ok("AUTOMATION_PARAM_X")
    for bad in ("PATH", "AUTOMAGIX_HOME", "AUTOSTATION_LLM_KEY", "amx_lower", "AMX_" + "x" * 80, "PYTHONPATH"):
        assert not testing._env_ok(bad)


def test_load_fixtures_formats_and_limits(tmp_path):
    j = tmp_path / "f.json"
    j.write_text(json.dumps({"cases": [{"name": "a"}]}), encoding="utf-8")
    assert testing.load_fixtures(j) == [{"name": "a"}]
    y = tmp_path / "f.yaml"
    y.write_text("- name: a\n  params: {mode: demo}\n  expect: {status: success}\n- name: b\n", encoding="utf-8")
    assert [c["name"] for c in testing.load_fixtures(y)] == ["a", "b"]
    for bad in ("[]", "[1, 2]", json.dumps({"cases": "x"})):
        j.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            testing.load_fixtures(j)
    j.write_text("x" * (testing.MAX_FIXTURE_BYTES + 1), encoding="utf-8")
    with pytest.raises(ValueError):
        testing.load_fixtures(j)
    with pytest.raises(ValueError):
        testing.load_fixtures(tmp_path / "missing.json")


def test_cli(payload, tmp_path, capsys):
    f = tmp_path / "cases.json"
    f.write_text(json.dumps([{"name": "ok", "params": {"mode": "demo"}, "expect": {"status": "success"}},
                             {"name": "bad", "params": {"mode": "demo"}, "expect": {"status": "failed"}}]), encoding="utf-8")
    assert cli_main(["test", str(payload), "--fixtures", str(f)]) == 1
    out = capsys.readouterr().out
    assert "PASS  ok" in out and "FAIL  bad" in out and "1/2 case(s) passed" in out
    assert cli_main(["test", str(payload), "--fixtures", str(f), "--case", "ok"]) == 0
    capsys.readouterr()
    assert cli_main(["test", str(payload), "--fixtures", str(f), "--case", "nope"]) == 2
    assert cli_main(["test", str(payload)]) == 0                                        # default: one run that must succeed
    capsys.readouterr()
    assert cli_main(["test", str(payload), "--fixtures", str(f), "--json"]) == 1
    assert [r["name"] for r in json.loads(capsys.readouterr().out)] == ["ok", "bad"]
    assert cli_main(["test", str(tmp_path)]) == 2                                       # no automation.yaml
