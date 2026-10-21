from __future__ import annotations

import copy
import json

import pytest

from conftest import automation_zip, make_zip, run_cli, sha
from registry import checks, ops, store
from registry.ops import OpError
from wrapper_core import manifest as wc_manifest
from wrapper_core import miniyaml


def load(p):
    return miniyaml.loads(p.read_text(encoding="utf-8"))


# ---- validation ------------------------------------------------------------------------------------
def test_synthetic_manifest_is_valid(manifest):
    rep = checks.validate_all(manifest)
    assert rep.errors == [], rep.errors


def test_schema_and_semantic_failures_reported(manifest):
    a = manifest["automations"]["auto-a"]
    a["owner"] = "not-an-email"
    a["schedule"]["slots"].append({"id": "daily", "type": "daily", "time": "10:00"})
    a["versions"]["1.0.0"]["sha256"] = "zzz"
    a["catalog"]["tags"] = ["Bad Tag"]
    a["catalog"]["links"] = ["javascript:alert(1)"]
    a["catalog"]["data_classification"] = "restricted"
    manifest["fleet"] = {"paused": True, "paused_reason": ""}
    errs = "\n".join(checks.validate_all(manifest).errors)
    for needle in ("not an email", "duplicate slot id", "sha256", "tags", "must be http(s)", "disagrees", "fleet: paused"):
        assert needle in errs, needle


def test_placeholder_sha_only_with_flag(manifest):
    manifest["automations"]["auto-b"]["versions"]["1.0.0"]["sha256"] = store.PLACEHOLDER_SHA
    assert any("placeholder" in e for e in checks.validate_all(manifest).errors)
    assert checks.validate_all(manifest, allow_placeholders=True).errors == []


def test_ring_gaps_allowed_for_automations_not_components(manifest):
    del manifest["automations"]["auto-a"]["rings"]["broad"]
    assert checks.validate_all(manifest).errors == []           # "not released to broad yet"
    del manifest["components"]["wrapper-core"]["rings"]["broad"]
    assert any("component wrapper-core" in e for e in checks.validate_all(manifest).errors)


def test_trigger_rules_need_poll_slot(manifest):
    b = manifest["automations"]["auto-b"]
    b["triggers"] = [{"on_automation": "auto-a", "on_status": "failed"}]
    assert any("no poll slot" in e for e in checks.validate_all(manifest).errors)
    b["schedule"]["slots"].append({"id": "poll", "type": "poll", "every_minutes": 10, "params": {}})
    assert checks.validate_all(manifest).errors == []
    b["triggers"][0]["on_status"] = "weird"
    assert checks.validate_all(manifest).errors


def test_warnings_and_strict(manifest):
    manifest["automations"]["auto-b"]["unexpected"] = 1
    rep = checks.validate_all(manifest)
    assert rep.ok and any("unknown key 'unexpected'" in w for w in rep.warnings) and any("no catalog block" in w for w in rep.warnings)
    assert not checks.validate_all(manifest, strict=True).ok


def test_llm_gate_warning(manifest):
    manifest["automations"]["auto-a"]["data_classification"] = "confidential"
    manifest["automations"]["auto-a"]["catalog"]["data_classification"] = "confidential"
    assert any("llm_call()s will be disabled" in w for w in checks.validate_all(manifest).warnings)


def test_artifact_verification(tmp_path, manifest):
    art = tmp_path / "art"
    z = automation_zip(art / "artifacts/automations/auto-b-1.0.0.zip", "auto-b", "1.0.0")
    manifest["automations"]["auto-b"]["versions"]["1.0.0"]["sha256"] = sha("wrong")
    rep = checks.validate_all(manifest, artifacts_dir=art)
    assert any("sha256 mismatch" in e for e in rep.errors) and any("not found" in e for e in rep.errors)
    import hashlib
    manifest["automations"]["auto-b"]["versions"]["1.0.0"]["sha256"] = hashlib.sha256(z.read_bytes()).hexdigest()
    errs = [e for e in checks.validate_all(manifest, artifacts_dir=art).errors if "auto-b" in e]
    assert errs == []


def test_wrapper_core_ignores_catalog_and_unknown_keys(manifest):
    text = miniyaml.dumps(manifest)
    parsed = wc_manifest.parse(text)
    assert parsed["automations"]["auto-a"]["catalog"]["tags"] == ["finance", "ap"]
    assert wc_manifest.paused(parsed, "auto-a") == (False, "")
    assert wc_manifest.target_version(parsed["automations"]["auto-a"], "pilot") == "1.1.0"


def test_cli_validate_exit_codes(mpath, capsys):
    assert run_cli("validate", "--manifest", str(mpath)) == 0
    m = load(mpath)
    m["automations"]["auto-a"]["owner"] = "bad"
    mpath.write_text(miniyaml.dumps(m), encoding="utf-8")
    assert run_cli("validate", "--manifest", str(mpath)) == 1
    out = capsys.readouterr().out
    assert "ERROR" in out and "not an email" in out
    assert run_cli("--manifest", str(mpath), "validate", "--schema-only") == 0   # global --manifest before the subcommand


# ---- round trip --------------------------------------------------------------------------------------
def test_dump_is_byte_stable_and_roundtrips(manifest):
    t1 = store.dump_manifest(manifest)
    m2 = miniyaml.loads(t1)
    assert m2 == manifest
    assert store.dump_manifest(m2) == t1
    assert '"09:00"' in t1 and '"1.0.0":' in t1  # times and versions are always quoted


# ---- promote / rollback ----------------------------------------------------------------------------
def test_promote_flow_enforces_order(mpath, capsys):
    assert run_cli("promote", "auto-a", "--to", "pilot", "--manifest", str(mpath)) == 0
    assert load(mpath)["automations"]["auto-a"]["rings"] == {"dev": "1.2.0", "pilot": "1.2.0", "broad": "1.0.0"}
    assert "Suggested MR title: registry: promote auto-a 1.2.0 to pilot" in capsys.readouterr().out
    assert run_cli("promote", "auto-a", "--to", "broad", "--manifest", str(mpath)) == 0
    assert load(mpath)["automations"]["auto-a"]["rings"]["broad"] == "1.2.0"
    assert run_cli("promote", "auto-a", "--to", "broad", "--manifest", str(mpath)) == 0  # idempotent
    assert "nothing to do" in capsys.readouterr().out


def test_cannot_skip_ring_or_promote_newer_than_previous(manifest):
    with pytest.raises(OpError, match="newer than what ring pilot runs"):
        ops.promote(manifest, "auto-a", "broad", "1.2.0")
    with pytest.raises(OpError):
        ops.promote(manifest, "auto-a", "dev")
    with pytest.raises(OpError, match="not a registered"):
        ops.promote(manifest, "auto-a", "pilot", "9.9.9")
    del manifest["automations"]["auto-b"]["rings"]["dev"]
    with pytest.raises(OpError, match="no version in ring dev"):
        ops.promote(manifest, "auto-b", "pilot")


def test_promote_component(mpath):
    assert run_cli("promote", "wrapper-core", "--to", "pilot", "--manifest", str(mpath)) == 0
    assert load(mpath)["components"]["wrapper-core"]["rings"]["pilot"] == "0.2.0"


def test_rollback_caps_later_rings(manifest):
    manifest["automations"]["auto-a"]["rings"] = {"dev": "1.2.0", "pilot": "1.2.0", "broad": "1.1.0"}
    res = ops.rollback(manifest, "auto-a", "pilot", "1.0.0")
    assert manifest["automations"]["auto-a"]["rings"] == {"dev": "1.2.0", "pilot": "1.0.0", "broad": "1.0.0"}
    assert "ROLLBACK" in res.mr_title and len(res.summary) >= 3
    with pytest.raises(OpError, match="nothing to roll back"):
        ops.rollback(manifest, "auto-a", "pilot", "1.0.0")
    with pytest.raises(OpError, match="not a registered"):
        ops.rollback(manifest, "auto-a", "dev", "0.0.1")


def test_rollback_requires_reason_and_writes_history(mpath):
    assert run_cli("rollback", "auto-a", "--ring", "dev", "--to", "1.0.0", "--manifest", str(mpath)) == 2
    assert run_cli("rollback", "auto-a", "--ring", "dev", "--to", "1.0.0", "--reason", "regression in 1.2", "--manifest", str(mpath)) == 0
    assert load(mpath)["automations"]["auto-a"]["rings"] == {"dev": "1.0.0", "pilot": "1.0.0", "broad": "1.0.0"}
    hist = store.read_history(mpath)
    assert hist[-1]["action"] == "rollback" and "regression in 1.2" in hist[-1]["detail"] and hist[-1]["target"] == "auto-a"


# ---- kill-switch -----------------------------------------------------------------------------------
def test_pause_resume_cycle(mpath):
    assert run_cli("pause", "auto-a", "--manifest", str(mpath)) == 2   # reason required
    assert run_cli("pause", "auto-a", "--reason", "bad  upstream\ndata", "--manifest", str(mpath)) == 0
    a = load(mpath)["automations"]["auto-a"]
    assert a["status"] == "paused" and a["paused_reason"] == "bad upstream data"   # whitespace normalised
    assert wc_manifest.paused(wc_manifest.load(mpath), "auto-a") == (True, "bad upstream data")
    assert run_cli("resume", "auto-a", "--reason", "fixed", "--manifest", str(mpath)) == 0
    a = load(mpath)["automations"]["auto-a"]
    assert a["status"] == "active" and a["paused_reason"] == ""


def test_fleet_pause_resume(mpath):
    assert run_cli("fleet-pause", "--reason", "incident 7", "--manifest", str(mpath)) == 0
    assert wc_manifest.paused(wc_manifest.load(mpath), "auto-b") == (True, "incident 7")
    assert run_cli("fleet-resume", "--reason", "all clear", "--manifest", str(mpath)) == 0
    assert load(mpath)["fleet"] == {"paused": False, "paused_reason": ""}


def test_reason_length_limit(manifest):
    with pytest.raises(OpError, match="too long"):
        ops.pause(manifest, "auto-a", "x" * 301)


def test_dry_run_writes_nothing(mpath):
    before = mpath.read_text(encoding="utf-8")
    assert run_cli("pause", "auto-a", "--reason", "x", "--dry-run", "--manifest", str(mpath)) == 0
    assert mpath.read_text(encoding="utf-8") == before and not (mpath.parent / "history").exists()


def test_invalid_result_is_never_written(mpath, manifest):
    # corrupt the on-disk manifest so that ANY write would produce an invalid file
    m = load(mpath)
    m["automations"]["auto-b"]["owner"] = "bad"
    mpath.write_text(miniyaml.dumps(m), encoding="utf-8")
    before = mpath.read_text(encoding="utf-8")
    assert run_cli("pause", "auto-a", "--reason", "x", "--manifest", str(mpath)) == 1
    assert mpath.read_text(encoding="utf-8") == before


# ---- decommission ----------------------------------------------------------------------------------
def test_decommission_requires_pause_and_cleans_rules(mpath, tmp_path):
    m = load(mpath)
    b = m["automations"]["auto-b"]
    b["schedule"]["slots"].append({"id": "poll", "type": "poll", "every_minutes": 5, "params": {}})
    b["triggers"] = [{"on_automation": "auto-a", "on_status": "failed", "slot": "poll"}]
    mpath.write_text(miniyaml.dumps(m), encoding="utf-8")
    assert run_cli("decommission", "auto-a", "--reason", "retired", "--manifest", str(mpath)) == 2   # not paused
    run_cli("pause", "auto-a", "--reason", "retiring", "--manifest", str(mpath))
    assert run_cli("decommission", "auto-a", "--reason", "process retired", "--manifest", str(mpath)) == 0
    out = load(mpath)
    assert "auto-a" not in out["automations"] and "triggers" not in out["automations"]["auto-b"]
    note = (tmp_path / "history" / "decommissioned" / "auto-a.md").read_text(encoding="utf-8")
    assert "process retired" in note
    assert store.read_history(mpath)[-1]["action"] == "decommission"


# ---- add-version -----------------------------------------------------------------------------------
def test_add_version_registers_dev_only_and_copies(mpath, tmp_path):
    z = automation_zip(tmp_path / "build" / "pkg.zip", "auto-a", "1.3.0", {"wrapper_core": ">=0.1.0,<1.0.0", "execution_framework": ">=0.1.0,<1.0.0"})
    assert run_cli("add-version", "automation", "auto-a", str(z), "--manifest", str(mpath)) == 0
    m = load(mpath)
    a = m["automations"]["auto-a"]
    assert a["rings"] == {"dev": "1.3.0", "pilot": "1.1.0", "broad": "1.0.0"}
    rec = a["versions"]["1.3.0"]
    import hashlib
    assert rec["sha256"] == hashlib.sha256(z.read_bytes()).hexdigest() and rec["url"] == "artifacts/automations/auto-a-1.3.0.zip"
    assert rec["requires"]["wrapper_core"] == ">=0.1.0,<1.0.0"
    assert (tmp_path / rec["url"]).read_bytes() == z.read_bytes()
    assert list(a["versions"]) == ["1.0.0", "1.1.0", "1.2.0", "1.3.0"]
    # verifies against the artifacts it just copied
    assert not [e for e in checks.validate_all(m, artifacts_dir=tmp_path).errors if "1.3.0" in e]


def test_add_version_is_immutable_and_idempotent(mpath, tmp_path):
    z = automation_zip(tmp_path / "pkg.zip", "auto-a", "1.3.0")
    assert run_cli("add-version", "automation", "auto-a", str(z), "--manifest", str(mpath)) == 0
    assert run_cli("add-version", "automation", "auto-a", str(z), "--manifest", str(mpath)) == 0       # same bytes: no-op
    z2 = make_zip(tmp_path / "pkg2.zip", {"automation.yaml": miniyaml.dumps({"name": "auto-a", "version": "1.3.0", "entrypoint": "x.py"})})
    assert run_cli("add-version", "automation", "auto-a", str(z2), "--manifest", str(mpath)) == 2      # different bytes: refused


def test_add_version_package_mismatch_and_unknown(mpath, tmp_path):
    z = automation_zip(tmp_path / "p.zip", "other", "1.0.0")
    assert run_cli("add-version", "automation", "auto-a", str(z), "--manifest", str(mpath)) == 2
    z = automation_zip(tmp_path / "q.zip", "ghost", "1.0.0")
    assert run_cli("add-version", "automation", "ghost", str(z), "--manifest", str(mpath)) == 2


def test_add_older_version_than_pilot_is_refused_by_ring_order(mpath, tmp_path):
    z = automation_zip(tmp_path / "p.zip", "auto-a", "0.9.0")
    assert run_cli("add-version", "automation", "auto-a", str(z), "--manifest", str(mpath)) == 1   # dev would lag pilot
    assert "0.9.0" not in load(mpath)["automations"]["auto-a"]["versions"]


def test_add_component_version(mpath, tmp_path):
    z = make_zip(tmp_path / "wc.zip", {"VERSION": "0.3.0\n", "wrapper_core/__init__.py": ""})
    assert run_cli("add-version", "component", "wrapper-core", str(z), "--manifest", str(mpath)) == 0
    c = load(mpath)["components"]["wrapper-core"]
    assert c["rings"]["dev"] == "0.3.0" and c["versions"]["0.3.0"]["url"] == "artifacts/components/wrapper-core-0.3.0.zip"


# ---- new-automation --------------------------------------------------------------------------------
def test_new_automation_skeleton_is_valid_catalog_complete_and_paused(mpath):
    assert run_cli("new-automation", "auto-c", "--owner", "c@example.com", "--description", "Does C", "--system", "Portal Y",
                   "--time", "07:15", "--timezone", "America/Chicago", "--manifest", str(mpath)) == 0
    m = load(mpath)
    c = m["automations"]["auto-c"]
    assert c["status"] == "paused" and "first release" in c["paused_reason"] and c["versions"] == {} and c["rings"] == {}
    assert c["schedule"]["slots"][0]["time"] == "07:15" and c["catalog"]["summary"] == "Does C" and c["catalog"]["systems"] == ["Portal Y"]
    rep = checks.validate_all(m)
    assert rep.errors == [] and not [w for w in rep.warnings if "auto-c" in w and "catalog" in w and "TODO" not in w]
    wc_manifest.parse(mpath.read_text(encoding="utf-8"))


def test_new_automation_validates_input(manifest):
    with pytest.raises(OpError, match="already exists"):
        ops.new_automation(manifest, "auto-a", owner="a@b.co", description="x")
    with pytest.raises(OpError, match="email"):
        ops.new_automation(manifest, "auto-z", owner="nope", description="x")
    with pytest.raises(OpError, match="lower-case"):
        ops.new_automation(manifest, "Bad_Name", owner="a@b.co", description="x")


# ---- triggers --------------------------------------------------------------------------------------
def test_trigger_add(mpath, tmp_path):
    assert run_cli("trigger-add", "auto-a", "--param", "date_range=today", "--param", "x=1", "--manifest", str(mpath)) == 0
    doc = json.loads((tmp_path / "triggers.json").read_text(encoding="utf-8"))
    t = doc["triggers"][0]
    assert t["automation"] == "auto-a" and t["slot"] == "poll" and t["params"] == {"date_range": "today", "x": "1"}
    assert t["id"].startswith("t-20261012T130000Z-") and t["requested_by"] == "tester" and t["source"] == "operator"
    assert run_cli("trigger-add", "auto-a", "--manifest", str(mpath)) == 0
    ids = [x["id"] for x in json.loads((tmp_path / "triggers.json").read_text(encoding="utf-8"))["triggers"]]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert not (tmp_path / "history").exists()  # ephemeral: no history noise, manifest untouched


def test_trigger_add_refusals(mpath):
    assert run_cli("trigger-add", "auto-b", "--manifest", str(mpath)) == 2      # no poll slot
    assert run_cli("trigger-add", "auto-a", "--slot", "daily", "--manifest", str(mpath)) == 2   # not a poll slot
    run_cli("pause", "auto-a", "--reason", "x", "--manifest", str(mpath))
    assert run_cli("trigger-add", "auto-a", "--manifest", str(mpath)) == 2      # paused


def test_trigger_pruning(manifest):
    doc = {"triggers": [{"id": "old", "created_at": "2026-01-01T00:00:00Z"}, {"id": "new", "created_at": "2026-10-11T00:00:00Z"}]}
    ops.add_trigger(doc, manifest, "auto-a", None, {})
    assert [t["id"] for t in doc["triggers"]][:1] == ["new"] and all(t["id"] != "old" for t in doc["triggers"])


# ---- every manifest the CLI writes is parseable by wrapper-core -------------------------------------
def test_all_cli_outputs_parse_with_wrapper_core(mpath, tmp_path):
    run_cli("pause", "auto-a", "--reason", "x: y # z", "--manifest", str(mpath))
    run_cli("resume", "auto-a", "--reason", "ok", "--manifest", str(mpath))
    run_cli("promote", "auto-a", "--to", "pilot", "--manifest", str(mpath))
    run_cli("new-automation", "auto-c", "--owner", "c@example.com", "--description", "C: with colon", "--manifest", str(mpath))
    parsed = wc_manifest.parse(mpath.read_text(encoding="utf-8"))
    assert parsed["automations"]["auto-c"]["description"] == "C: with colon"
    assert wc_manifest.validate(parsed) is not None


def test_compat_command(mpath, capsys):
    assert run_cli("compat", "--manifest", str(mpath)) == 0
    m = load(mpath)
    m["compatibility"] = [{"wrapper_core": ">=0.1.0,<0.2.0", "execution_framework": ">=0.1.0,<1.0.0"}]
    mpath.write_text(miniyaml.dumps(m), encoding="utf-8")
    assert run_cli("compat", "--manifest", str(mpath)) == 1
    assert "NOT SUPPORTED" in capsys.readouterr().out
