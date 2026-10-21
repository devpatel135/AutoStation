from __future__ import annotations

import json
import os
import shutil

import pytest

from wrapper_core import lifecycle, mockllm, runctx, selfheal, transport
from wrapper_core.paths import Paths
from wrapper_core.runner import run_automation
from wrapper_core import runner as runner_mod

from conftest import HELLO, read_artifacts


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(runner_mod, "_sleep", lambda s: None)


def setup_reg(registry, code=HELLO, name="auto-a", **kw):
    registry.add_component("wrapper-core", "0.1.0")
    registry.add_component("execution-framework", "0.1.0")
    registry.add_automation(name, "1.0.0", code, **kw)
    return registry.save()


def run(name="auto-a", slot="daily", **kw):
    runctx.set_current(None)
    return run_automation(name, slot, **kw)


def test_happy_path_installs_runs_and_logs(registry, logs_dir):
    setup_reg(registry)
    out = run()
    rec = out.record
    assert out.exit_code == 0 and rec["status"] == "success"
    assert rec["items"] == {"total": 3, "success": 3, "skipped": 0, "failed": 0}
    assert rec["params"] == {"mode": "test"} and rec["result"] == {"processed": 3}
    assert rec["automation_version"] == "1.0.0" and rec["ring"] == "pilot"
    assert rec["synopsis_source"] == "llm" and rec["synopsis"].startswith("Mock synopsis")
    assert any(e["event"] == "start" and e["data"]["mode"] == "test" for e in rec["events"])
    assert "installed" in json.dumps(rec["self_heal"]) and any("auto-a" in i for i in rec["self_heal"]["installed"])
    # artefacts reached the transport with the SPEC layout
    runs = read_artifacts(logs_dir, "run")
    assert len(runs) == 1 and runs[0]["run_id"] == rec["run_id"]
    phases = {h["phase"] for h in read_artifacts(logs_dir, "heartbeat")}
    assert {"run_start", "run_end"} <= phases
    assert (logs_dir / "ingest" / "pilot" / "run" / "auto-a" / f"{rec['run_id']}.json").exists()


def test_payload_never_sees_secrets_but_can_use_llm_via_broker(registry, monkeypatch):
    code = '''
import os, wrapper_core as wrapper
@wrapper.entrypoint
def main():
    out = wrapper.llm_call("classify", "doc", schema={"vendor": "string"}, caller="payload")
    wrapper.set_result(key=os.environ.get("AUTOSTATION_LLM_KEY"), broker=bool(os.environ.get("AUTOMAGIX_BROKER_URL")), out=out)
if __name__ == "__main__":
    raise SystemExit(main())
'''
    setup_reg(registry, code)
    monkeypatch.setenv("AUTOSTATION_LLM_KEY", "sk-super-secret")
    rec = run().record
    assert rec["status"] == "success"
    assert rec["result"] == {"key": None, "broker": True, "out": {"vendor": "mock"}}
    assert any(e["event"] == "llm_call" and e["data"]["caller"] == "payload" for e in rec["events"])


def test_kill_switch_halts_before_payload(registry, logs_dir):
    setup_reg(registry)
    registry.manifest["automations"]["auto-a"].update(status="paused", paused_reason="bad upstream data")
    registry.save()
    out = run()
    assert out.record["status"] == "halted_paused" and out.exit_code == 0
    assert not out.record["events"] or all(e["event"] != "start" for e in out.record["events"])
    status = Paths().status_file("auto-a").read_text(encoding="utf-8")
    assert "bad upstream data" in status
    assert any(a["event"] == "paused_seen" for a in read_artifacts(logs_dir, "lifecycle"))
    # resume clears the notice
    registry.manifest["automations"]["auto-a"].update(status="active", paused_reason="")
    registry.save()
    assert run().record["status"] == "success" and not Paths().status_file("auto-a").exists()


def test_fleet_pause(registry):
    setup_reg(registry)
    registry.manifest["fleet"] = {"paused": True, "paused_reason": "incident 42"}
    registry.save()
    assert run().record["status"] == "halted_paused"


def test_maintenance_window_skips(registry):
    setup_reg(registry)
    registry.manifest["automations"]["auto-a"]["schedule"]["maintenance_windows"] = [{"days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}]
    registry.save()
    assert run().record["status"] == "skipped_maintenance"
    assert run(ignore_maintenance=True).record["status"] == "success"


def test_network_error_retried_at_process_level(registry):
    code = '''
import os, wrapper_core as wrapper
@wrapper.entrypoint
def main():
    n = int(open("counter.txt").read()) if os.path.exists("counter.txt") else 0
    open("counter.txt", "w").write(str(n + 1))
    if n < 2:
        raise ConnectionError("VPN dropped")
    wrapper.log_item("I1", "success")
if __name__ == "__main__":
    raise SystemExit(main())
'''
    setup_reg(registry, code)
    rec = run().record
    assert rec["status"] == "success"
    retries = [e["data"] for e in rec["events"] if e["event"] == "retry_event"]
    assert [r["category"] for r in retries] == ["NETWORK_ERROR", "NETWORK_ERROR"]
    assert retries[-1]["outcome"] == "recovered"


def test_logic_error_fails_without_retry(registry):
    code = '''
import wrapper_core as wrapper
@wrapper.entrypoint
def main():
    {}["missing"]
if __name__ == "__main__":
    raise SystemExit(main())
'''
    setup_reg(registry, code)
    out = run()
    rec = out.record
    assert out.exit_code == 1 and rec["status"] == "failed"
    assert rec["error"]["category"] == "LOGIC_ERROR" and rec["error"]["type"] == "KeyError"
    assert [e for e in rec["events"] if e["event"] == "retry_event"][0]["data"]["decision"] == "escalate"
    assert rec["synopsis"]


def test_uncaught_error_without_entrypoint_classified_from_stderr(registry):
    setup_reg(registry, "raise RuntimeError('HTTP 503 Service Unavailable')\n")
    rec = run().record
    assert rec["status"] == "failed" and rec["error"]["category"] == "SYSTEM_UNAVAILABLE"
    assert rec["error"]["attempts"] > 1  # backed off and retried before escalating


def test_item_failures_make_run_partial(registry):
    code = '''
import wrapper_core as wrapper
@wrapper.entrypoint
def main():
    wrapper.log_item("a", "success"); wrapper.log_item("b", "failed", error_category="data_error")
if __name__ == "__main__":
    raise SystemExit(main())
'''
    setup_reg(registry, code)
    rec = run().record
    assert rec["status"] == "partial" and rec["items"]["failed"] == 1


def test_hung_detection_and_timeout_kill(registry, logs_dir):
    code = "import time\ntime.sleep(60)\n"
    setup_reg(registry, code, descriptor={"timeout_s": 3})
    registry.manifest["automations"]["auto-a"]["schedule"]["expected_duration_ms"] = {"baseline": 500, "warn_threshold": 1.0}
    registry.save()
    out = run()
    rec = out.record
    assert rec["status"] == "hung" and out.exit_code == 2 and rec["hung_detected"]
    assert any(e["event"] == "run_hung" for e in rec["events"])
    assert any(a["event"] == "run_hung" for a in read_artifacts(logs_dir, "lifecycle"))
    assert rec["expected_finish_at"]
    assert any(h["expected_finish_at"] for h in read_artifacts(logs_dir, "heartbeat") if h["phase"] == "run_start")


def test_sensitive_values_redacted_in_llm_context(registry):
    captured = []
    mockllm.register(lambda c, i, d, s: captured.append(d) or None)
    code = '''
import wrapper_core as wrapper
@wrapper.entrypoint
def main():
    wrapper.log_event("customer", {"name": "Jane", "ssn": "123-45-6789"}, sensitive_keys=["ssn"])
if __name__ == "__main__":
    raise SystemExit(main())
'''
    setup_reg(registry, code)
    rec = run().record
    synopsis_ctx = [c for c in captured if "events" in c]
    assert synopsis_ctx and all("123-45-6789" not in c for c in synopsis_ctx)
    # ...but the raw run log (access-controlled) keeps it
    assert any(e["data"].get("ssn") == "123-45-6789" for e in rec["events"])


def test_llm_disabled_for_over_classified_automation_falls_back_to_deterministic(registry):
    setup_reg(registry, data_classification="restricted", llm_allowed_classification="internal")
    rec = run().record
    assert rec["synopsis_source"] == "deterministic" and "succeeded" in rec["synopsis"]


def test_registry_outage_uses_cached_manifest(registry, tmp_path):
    setup_reg(registry)
    assert run().record["status"] == "success"
    shutil.rmtree(registry.root)
    rec = run().record
    assert rec["status"] == "success" and rec["self_heal"]["offline"] is True
    assert any("unreachable" in e for e in rec["self_heal"]["errors"])


def test_bad_sha256_blocks_install(registry):
    setup_reg(registry)
    registry.manifest["automations"]["auto-a"]["versions"]["1.0.0"]["sha256"] = "0" * 64
    registry.save()
    out = run()
    assert out.record["status"] == "self_heal_failed" and out.exit_code == 1
    assert "sha256 mismatch" in json.dumps(out.record["self_heal"])


def test_transport_failure_spools_then_flushes(registry, tmp_path, monkeypatch):
    setup_reg(registry)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("AUTOMAGIX_LOG_REMOTE", str(blocker))  # cannot mkdir under a file
    rec = run().record
    assert rec["status"] == "success"  # logging never fails the run
    assert len(list(Paths().spool.glob("*.json"))) >= 2
    good = tmp_path / "good-logs"
    monkeypatch.setenv("AUTOMAGIX_LOG_REMOTE", str(good))
    sent = transport.flush_spool(Paths())
    assert sent >= 2 and not list(Paths().spool.glob("*.json"))
    assert read_artifacts(good, "run")


def test_poll_slot_runs_once_per_trigger(registry):
    setup_reg(registry)
    registry.manifest["automations"]["auto-a"]["schedule"]["slots"].append({"id": "poll", "type": "poll", "every_minutes": 5, "params": {"mode": "poll"}})
    registry.save()
    (registry.root / "triggers.json").write_text(json.dumps({"triggers": [
        {"id": "t1", "automation": "auto-a", "slot": "poll", "params": {"mode": "from-trigger"}},
        {"id": "t2", "automation": "someone-else", "params": {}}]}))
    out = run(slot="poll")
    assert out.record["params"] == {"mode": "from-trigger"} and out.record["trigger_id"] == "t1"
    assert run(slot="poll").record is None  # consumed -> quiet no-op, no log noise


def test_concurrent_run_is_refused(registry):
    setup_reg(registry)
    p = Paths().ensure()
    p.lock_file("auto-a").write_text("123 0")
    assert run().record is None
    p.lock_file("auto-a").unlink()
    assert run().record["status"] == "success"


def test_wrapper_core_update_is_staged_then_applied(registry):
    setup_reg(registry)
    registry.add_component("wrapper-core", "0.2.0", {"wrapper_core/x.py": "# new"})
    registry.save()
    p = Paths().ensure()
    heal = selfheal.run(p, {"ring": "pilot", "registry_url": str(registry.root)}, "auto-a")
    assert any("wrapper-core staged 0.2.0" in i for i in heal.installed)
    assert (p.root / "wrapper-core.pending" / "VERSION").read_text().strip() == "0.2.0"
    assert selfheal.apply_pending(p) == ["wrapper-core 0.2.0"]
    assert (p.wrapper_core / "VERSION").read_text().strip() == "0.2.0" and not (p.root / "wrapper-core.pending").exists()
    assert selfheal.installed_component_version(p, "wrapper-core") == "0.2.0"


def test_zip_slip_rejected(tmp_path):
    import zipfile
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../escape.txt", "x")
    with pytest.raises(selfheal.HealError):
        selfheal.safe_extract(z, tmp_path / "out")


def test_dry_run_sends_nothing(registry, logs_dir, capsys):
    setup_reg(registry)
    rec = run(dry_run=True).record
    assert rec["status"] == "success"
    assert not logs_dir.exists() or not read_artifacts(logs_dir, "run")
    assert '"status": "success"' in capsys.readouterr().out


def test_install_and_uninstall_refcount(registry, logs_dir, tmp_path):
    setup_reg(registry, name="auto-a")
    registry.add_automation("auto-b", "1.0.0", HELLO)
    registry.save()
    from wrapper_core.scheduler import Scheduler
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    sched = Scheduler(Paths().ensure(), runner=lambda args, input_text=None: calls.append(args) or R())
    assert lifecycle.install_automation("auto-a", scheduler=sched)["ok"]
    assert lifecycle.install_automation("auto-b", scheduler=sched)["ok"]
    assert (tmp_path / "startup" / "AUTOMAGIX-maintain.cmd").exists()
    assert any(c[:2] == ["schtasks", "/Create"] for c in calls)
    p = Paths()
    p.secrets.mkdir(exist_ok=True)
    (p.secrets / "logging_key.dpapi").write_bytes(b"blob")

    rep = lifecycle.uninstall_automation("auto-a", scheduler=sched)
    assert not p.automation_dir("auto-a").exists() and p.automation_dir("auto-b").exists()
    assert (p.secrets / "logging_key.dpapi").exists() and p.framework.exists()  # still used by auto-b
    assert any("shared components" in k for k in rep["kept"])
    assert any(a["event"] == "decommissioned" for a in read_artifacts(logs_dir, "lifecycle"))

    rep = lifecycle.uninstall_automation("auto-b", scheduler=sched)
    assert not (p.secrets / "logging_key.dpapi").exists() and not p.framework.exists()
    assert not (tmp_path / "startup" / "AUTOMAGIX-maintain.cmd").exists()


def test_token_is_only_sent_to_the_registry_origin(monkeypatch):
    seen = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"x"

    def fake_urlopen(req, timeout=0):
        seen.append((req.full_url, req.get_header("Private-token")))
        return FakeResp()

    monkeypatch.setattr(selfheal.urllib.request, "urlopen", fake_urlopen)
    base = "https://registry.example.internal/pages"
    selfheal.fetch_bytes(base + "/manifest.yaml", "tok", trusted_base=base)
    selfheal.fetch_bytes("https://evil.example.com/pkg.zip", "tok", trusted_base=base)
    assert seen[0][1] == "tok" and seen[1][1] is None


def test_names_cannot_escape_directories():
    from wrapper_core.paths import check_name
    for bad in ("..", "a/b", "a\b", "a..b", "", "x" * 101, "-rf"):
        with pytest.raises(ValueError):
            check_name(bad)
    assert check_name("automation-hello_world.v2") == "automation-hello_world.v2"
