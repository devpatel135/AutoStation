from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

from conftest import ROOT, run_cli
from registry import checks, ci_ops, jsonschema_lite, store
from registry.ci_ops import GitLab
from registry.ops import OpError
from wrapper_core import manifest as wc_manifest
from wrapper_core import miniyaml

ENV = {"CI_API_V4_URL": "https://gl.example/api/v4", "CI_PROJECT_ID": "42", "CI_DEFAULT_BRANCH": "main", "OPS_BOT_TOKEN": "tok",
       "GITLAB_USER_LOGIN": "alice", "CI_PIPELINE_URL": "https://gl.example/p/1"}


class FakeGitLab:
    """Records API calls; serves files from `files` keyed (path, ref)."""

    def __init__(self, files=None):
        self.calls, self.files = [], files or {}

    def __call__(self, method, url, token, body=None):
        assert token == "tok"
        self.calls.append((method, url, body))
        path = urllib.parse.urlparse(url).path
        if method == "GET" and "/repository/files/" in url:
            fp = urllib.parse.unquote(path.split("/repository/files/")[1].split("/raw")[0])
            ref = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["ref"][0]
            return (200, self.files[(fp, ref)]) if (fp, ref) in self.files else (404, {"message": "404 File Not Found"})
        if method == "POST" and path.endswith("/repository/commits"):
            return 201, {"id": "abc"}
        if method == "POST" and path.endswith("/merge_requests"):
            return 201, {"iid": 7, "web_url": "https://gl.example/mr/7"}
        if method == "PUT" and "/merge" in path:
            return 200, {"state": "merged"}
        if method == "POST" and path.endswith("/pipeline"):
            return 201, {"id": 99}
        return 500, {}

    def of(self, method, suffix):
        return [c for c in self.calls if c[0] == method and urllib.parse.urlparse(c[1]).path.endswith(suffix)]


def env(**kw):
    return {**ENV, **kw}


@pytest.fixture
def api_and_fake():
    fake = FakeGitLab()
    return GitLab(ENV, http=fake), fake


# ---- ops-action: manifest actions go through a branch + MR -----------------------------------------------------
def test_pause_creates_branch_commit_and_mr(mpath, api_and_fake):
    api, fake = api_and_fake
    res = ci_ops.run_action(env(ACTION="pause", TARGET="auto-a", REASON="bad data", BRANCH="ops/pause-auto-a-x1"), mpath, api=api)
    assert res["changed"] and res["branch"] == "ops/pause-auto-a-x1" and res["mr_url"] == "https://gl.example/mr/7"
    commit = fake.of("POST", "/repository/commits")[0][2]
    assert commit["branch"] == "ops/pause-auto-a-x1" and commit["start_branch"] == "main"
    files = {a["file_path"]: a for a in commit["actions"]}
    assert set(files) == {"manifest.yaml", "history/CHANGELOG.md"}
    new = miniyaml.loads(files["manifest.yaml"]["content"])
    assert new["automations"]["auto-a"]["status"] == "paused" and new["automations"]["auto-a"]["paused_reason"] == "bad data"
    assert "`pause` **auto-a** - bad data (by alice)" in files["history/CHANGELOG.md"]["content"]
    mr = fake.of("POST", "/merge_requests")[0][2]
    assert mr["source_branch"] == "ops/pause-auto-a-x1" and mr["target_branch"] == "main" and "ops-action" in mr["labels"]
    assert "Requested by: alice" in mr["description"] and "bad data" in mr["description"]
    assert not fake.of("PUT", "/merge")                        # NOT merged: the approval is the gate
    assert not fake.of("POST", "/pipeline")
    assert mpath.read_text(encoding="utf-8") == store.dump_manifest(miniyaml.loads(mpath.read_text(encoding="utf-8")))  # working tree untouched


def test_default_branch_name_and_branch_validation(mpath, api_and_fake):
    api, fake = api_and_fake
    res = ci_ops.run_action(env(ACTION="promote", TARGET="auto-a", PARAMS='{"to":"pilot"}'), mpath, api=api)
    assert res["branch"].startswith("ops/promote-auto-a-")
    with pytest.raises(OpError, match="BRANCH"):
        ci_ops.run_action(env(ACTION="promote", TARGET="auto-a", PARAMS='{"to":"pilot"}', BRANCH="main"), mpath, api=api)


@pytest.mark.parametrize("e,msg", [
    ({"ACTION": "rm -rf", "TARGET": "auto-a"}, "unknown action"),
    ({"ACTION": "pause", "TARGET": "../../etc", "REASON": "x"}, "invalid target"),
    ({"ACTION": "pause", "TARGET": "auto-a", "REASON": ""}, "reason is required"),
    ({"ACTION": "pause", "TARGET": "auto-a", "REASON": "x" * 400}, "too long"),
    ({"ACTION": "promote", "TARGET": "auto-a", "PARAMS": "not json"}, "JSON object"),
    ({"ACTION": "promote", "TARGET": "auto-a", "PARAMS": "[1]"}, "JSON object"),
    ({"ACTION": "promote", "TARGET": "auto-a", "PARAMS": '{"to":"pilot","evil":1}'}, "does not accept"),
    ({"ACTION": "promote", "TARGET": "auto-a", "PARAMS": "{}"}, "needs PARAMS"),
    ({"ACTION": "rollback", "TARGET": "auto-a", "PARAMS": '{"ring":"dev","to":"1.0.0"}'}, "reason is required"),
    ({"ACTION": "pause", "TARGET": "ghost", "REASON": "x"}, "unknown component/automation"),
])
def test_ops_action_rejects_bad_input(mpath, e, msg):
    with pytest.raises(OpError, match=msg):
        ci_ops.run_action(env(**e), mpath, api=None, dry_run=True)


def test_ops_action_local_mode_writes_files(mpath):
    res = ci_ops.run_action({"ACTION": "fleet-pause", "REASON": "incident"}, mpath)
    assert res["changed"] and res["target"] == "fleet"
    assert wc_manifest.paused(wc_manifest.load(mpath), "auto-a") == (True, "incident")
    assert store.read_history(mpath)[-1]["action"] == "fleet-pause"


def test_ops_action_noop_does_not_commit(mpath, api_and_fake):
    api, fake = api_and_fake
    res = ci_ops.run_action(env(ACTION="resume", TARGET="auto-a", REASON="x"), mpath, api=api)
    assert res["changed"] is False and not fake.of("POST", "/repository/commits")


def test_resulting_invalid_manifest_is_refused(mpath, api_and_fake):
    api, fake = api_and_fake
    m = miniyaml.loads(mpath.read_text(encoding="utf-8"))
    m["automations"]["auto-b"]["owner"] = "broken"
    mpath.write_text(miniyaml.dumps(m), encoding="utf-8")
    with pytest.raises(OpError, match="would be invalid"):
        ci_ops.run_action(env(ACTION="pause", TARGET="auto-a", REASON="x"), mpath, api=api)
    assert not fake.of("POST", "/repository/commits")


def test_decommission_commits_audit_note(mpath, api_and_fake):
    api, fake = api_and_fake
    run_cli("pause", "auto-b", "--reason", "retiring", "--manifest", str(mpath))
    ci_ops.run_action(env(ACTION="decommission", TARGET="auto-b", REASON="process retired"), mpath, api=api)
    paths = {a["file_path"] for a in fake.of("POST", "/repository/commits")[0][2]["actions"]}
    assert "history/decommissioned/auto-b.md" in paths


# ---- emergency pause ------------------------------------------------------------------------------------------------
def test_emergency_pause_merges_only_for_authorised_users(mpath):
    for user, merged in (("alice", True), ("mallory", False)):
        fake = FakeGitLab()
        res = ci_ops.run_action(env(ACTION="pause", TARGET="auto-a", REASON="bleeding", EMERGENCY="1", GITLAB_USER_LOGIN=user,
                                    EMERGENCY_PAUSE_USERS="alice, bob"), mpath, api=GitLab(ENV, http=fake))
        assert bool(fake.of("PUT", "/merge")) is merged and ("emergency" in fake.of("POST", "/merge_requests")[0][2]["labels"]) is merged
    fake = FakeGitLab()   # EMERGENCY never applies to non-kill-switch actions
    ci_ops.run_action(env(ACTION="promote", TARGET="auto-a", PARAMS='{"to":"pilot"}', EMERGENCY="1", EMERGENCY_PAUSE_USERS="alice"), mpath, api=GitLab(ENV, http=fake))
    assert not fake.of("PUT", "/merge")


# ---- trigger-add: protected `triggers` branch, no MR ------------------------------------------------------------------
def test_trigger_add_commits_to_triggers_branch_and_republishes(mpath):
    fake = FakeGitLab()
    res = ci_ops.run_action(env(ACTION="trigger-add", TARGET="auto-a", PARAMS='{"slot":"poll","params":{"date_range":"today"}}'),
                            mpath, api=GitLab(ENV, http=fake))
    commit = fake.of("POST", "/repository/commits")[0][2]
    assert commit["branch"] == "triggers" and commit["start_branch"] == "main"       # branch created on first use
    doc = json.loads(commit["actions"][0]["content"])
    assert doc["triggers"][0]["params"] == {"date_range": "today"} and doc["triggers"][0]["requested_by"] == "alice"
    assert doc["triggers"][0]["source"] == "admin-panel"
    assert not fake.of("POST", "/merge_requests")                                     # no MR, no manifest change
    assert fake.of("POST", "/pipeline")[0][2]["variables"] == [{"key": "PAGES_ONLY", "value": "1"}]
    # second trigger appends to the existing file
    existing = {("triggers.json", "triggers"): json.dumps(doc)}
    fake2 = FakeGitLab(existing)
    ci_ops.run_action(env(ACTION="trigger-add", TARGET="auto-a"), mpath, api=GitLab(ENV, http=fake2))
    c2 = fake2.of("POST", "/repository/commits")[0][2]
    assert c2["actions"][0]["action"] == "update" and "start_branch" not in c2
    assert len(json.loads(c2["actions"][0]["content"])["triggers"]) == 2


def test_cli_ops_action_needs_credentials_for_git_mode(mpath, monkeypatch, tmp_path):
    for k in ("CI_API_V4_URL", "CI_PROJECT_ID", "OPS_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ACTION", "pause")
    assert run_cli("ops-action", "--git", "--manifest", str(mpath), "--result", str(tmp_path / "r.json")) == 2


def test_cli_ops_action_local_roundtrip(mpath, monkeypatch, tmp_path):
    monkeypatch.setenv("ACTION", "pause")
    monkeypatch.setenv("TARGET", "auto-a")
    monkeypatch.setenv("REASON", "cli test")
    res = tmp_path / "r.json"
    assert run_cli("ops-action", "--manifest", str(mpath), "--result", str(res)) == 0
    assert json.loads(res.read_text())["changed"] is True
    monkeypatch.setenv("REASON", "")
    assert run_cli("ops-action", "--manifest", str(mpath), "--result", str(res)) == 2
    assert json.loads(res.read_text())["ok"] is False


# ---- JSON-Schema + starter manifest -------------------------------------------------------------------------------------
STARTER = ROOT / "manifest.yaml"


def test_starter_manifest_is_valid_only_with_placeholders_flag():
    m = wc_manifest.load(STARTER)
    assert checks.validate_all(m, allow_placeholders=True).errors == []
    assert any("placeholder" in e for e in checks.validate_all(m).errors)
    assert run_cli("validate", "--manifest", str(STARTER), "--allow-placeholders") == 0
    assert run_cli("validate", "--manifest", str(STARTER)) == 1


def test_starter_manifest_contents_and_roundtrip():
    text = STARTER.read_text(encoding="utf-8")
    m = wc_manifest.parse(text)
    assert set(m["automations"]) == {"automation-hello-world", "automation-invoice-router", "automation-portal-report", "automation-notifier"}
    assert set(m["components"]) == {"wrapper-core", "execution-framework"}
    assert checks.validate_all(m, allow_placeholders=True, strict=True).errors == []     # catalog-complete: not even warnings
    assert "automation-portal-report" in m["automations"] and "broad" not in m["automations"]["automation-portal-report"]["rings"]
    body = "".join(l for l in text.splitlines(keepends=True) if not l.startswith("#"))
    assert store.dump_manifest(miniyaml.loads(body)) == body                              # byte-stable: tool rewrites cause no churn
    assert "PLACEHOLDER" in text.split("schema_version")[0]


def test_starter_builds_a_portal(tmp_path):
    from registry.portal import build
    res = build.build_portal(STARTER, tmp_path / "pub")
    assert "automation/automation-notifier.html" in res["files"]
    index = (tmp_path / "pub" / "index.html").read_text(encoding="utf-8")
    assert 'Download installer v1.0.0 (broad)' in index and "PLACEHOLDER - not a real release" in index


def test_schema_file_is_wellformed_and_catches_shape_errors(manifest):
    schema = checks.load_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    assert jsonschema_lite.validate(manifest, schema) == []
    bad = json.loads(json.dumps(manifest))
    bad["automations"]["auto-a"]["schedule"]["slots"][0]["time"] = "9am"
    bad["automations"]["auto-a"]["status"] = "sleeping"
    bad["automations"]["auto-a"]["versions"]["1.0"] = {"url": "x", "sha256": "0" * 64}
    bad["components"]["wrapper-core"]["rings"]["staging"] = "0.1.0"
    bad["automations"]["Bad_Name"] = bad["automations"].pop("auto-b")
    errs = "\n".join(jsonschema_lite.validate(bad, schema))
    for needle in ("time", "sleeping", "versions", "staging", "Bad_Name"):
        assert needle in errs, needle


def test_jsonschema_lite_features():
    s = {"$defs": {"n": {"type": "integer", "minimum": 1}},
         "type": "object", "required": ["a"], "additionalProperties": False,
         "properties": {"a": {"$ref": "#/$defs/n"}, "b": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                        "c": {"type": "array", "items": {"enum": [1, 2]}, "minItems": 1}}}
    assert jsonschema_lite.validate({"a": 1, "b": None, "c": [1]}, s) == []
    errs = jsonschema_lite.validate({"a": 0, "x": 1, "c": []}, s)
    assert len(errs) == 3


def test_module_entrypoint_runs_as_subprocess(mpath):
    r = subprocess.run([sys.executable, "-m", "registry", "validate", "--manifest", str(mpath)], cwd=str(ROOT),
                       capture_output=True, text=True)   # no PYTHONPATH: wrapper_core is auto-discovered in the monorepo
    assert r.returncode == 0 and "0 error(s)" in r.stdout
