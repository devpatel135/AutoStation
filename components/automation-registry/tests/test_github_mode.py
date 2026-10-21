"""GitHub flavour: ops-action through the GitHub REST API (fake HTTP layer) and the GitHub-mode portal/admin links."""
from __future__ import annotations

import base64
import json
import urllib.parse
from pathlib import Path

import pytest

from registry import ci_ops, store
from registry.ci_ops import GitHub
from registry.ops import OpError
from registry.portal import admin, build
from registry.portal.common import installer_rel, rel_href
from wrapper_core import miniyaml

ENV = {"GITHUB_REPOSITORY": "devpatel135/AutoStation", "GITHUB_TOKEN": "tok", "GITHUB_ACTOR": "alice",
       "GITHUB_API_URL": "https://api.github.test", "MANIFEST_PATH": "components/automation-registry/manifest.yaml"}


class FakeGitHub:
    """A tiny in-memory GitHub: branches (name -> {path: content}), pull requests, workflow dispatches."""

    def __init__(self, main_files: dict[str, str] | None = None):
        self.branches = {"main": dict(main_files or {})}
        self.calls, self.prs, self.dispatches, self.labels = [], [], [], []

    def __call__(self, method, url, token, body=None):
        assert token == "tok"
        self.calls.append((method, url, body))
        path = urllib.parse.urlparse(url).path.split("/repos/devpatel135/AutoStation", 1)[1]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if method == "GET" and path.startswith("/contents/"):
            fp = urllib.parse.unquote(path[len("/contents/"):])
            ref = query["ref"][0]
            files = self.branches.get(ref, {})
            if fp not in files:
                return 404, {"message": "Not Found"}
            return 200, {"content": base64.b64encode(files[fp].encode()).decode(), "sha": "sha-" + str(len(files[fp]))}
        if method == "GET" and path.startswith("/git/ref/heads/"):
            b = urllib.parse.unquote(path[len("/git/ref/heads/"):])
            return (200, {"object": {"sha": "head-" + b}}) if b in self.branches else (404, {"message": "Not Found"})
        if method == "POST" and path == "/git/refs":
            name = body["ref"][len("refs/heads/"):]
            src = body["sha"][len("head-"):]
            self.branches[name] = dict(self.branches[src])
            return 201, {}
        if method == "PUT" and path.startswith("/contents/"):
            fp = urllib.parse.unquote(path[len("/contents/"):])
            self.branches[body["branch"]][fp] = base64.b64decode(body["content"]).decode()
            return 200, {"commit": {"sha": "c1"}}
        if method == "POST" and path == "/pulls":
            self.prs.append(body)
            return 201, {"number": len(self.prs), "html_url": f"https://github.test/pull/{len(self.prs)}"}
        if method == "POST" and path.endswith("/labels"):
            self.labels.append(body["labels"])
            return 200, {}
        if method == "PUT" and "/merge" in path:
            return 200, {"merged": True}
        if method == "POST" and "/dispatches" in path:
            self.dispatches.append((path, body))
            return 204, ""
        return 500, {"unexpected": path}


@pytest.fixture
def repo(tmp_path, manifest):
    """A working tree + the same files on the fake 'main'."""
    mp = tmp_path / "components" / "automation-registry" / "manifest.yaml"
    mp.parent.mkdir(parents=True)
    text = miniyaml.dumps(manifest)
    mp.write_text(text, encoding="utf-8")
    fake = FakeGitHub({ENV["MANIFEST_PATH"]: text})
    return mp, fake, GitHub(ENV, http=fake)


def test_pause_opens_a_pull_request_and_never_touches_main(repo):
    mp, fake, api = repo
    res = ci_ops.run_action({**ENV, "ACTION": "pause", "TARGET": "auto-a", "REASON": "bad data", "BRANCH": "ops/pause-auto-a-x1"}, mp, api=api)
    assert res["changed"] and res["mr_url"] == "https://github.test/pull/1" and res["branch"] == "ops/pause-auto-a-x1"
    main_before = miniyaml.loads(fake.branches["main"][ENV["MANIFEST_PATH"]])
    assert main_before["automations"]["auto-a"]["status"] == "active"           # main untouched: the PR approval is the gate
    branch = fake.branches["ops/pause-auto-a-x1"]
    new = miniyaml.loads(branch[ENV["MANIFEST_PATH"]])
    assert new["automations"]["auto-a"]["status"] == "paused" and new["automations"]["auto-a"]["paused_reason"] == "bad data"
    assert "components/automation-registry/history/CHANGELOG.md" in branch
    assert "(by alice)" in branch["components/automation-registry/history/CHANGELOG.md"]
    pr = fake.prs[0]
    assert pr["head"] == "ops/pause-auto-a-x1" and pr["base"] == "main" and "alice" in pr["body"]
    assert ["ops-action", "pause"] in fake.labels
    assert not [c for c in fake.calls if c[0] == "PUT" and "/merge" in c[1]]
    assert not fake.dispatches


def test_emergency_pause_merges_only_for_listed_users(repo):
    mp, fake, api = repo
    ci_ops.run_action({**ENV, "ACTION": "fleet-pause", "REASON": "incident", "EMERGENCY": "1", "EMERGENCY_PAUSE_USERS": "bob, alice"}, mp, api=api)
    assert [c for c in fake.calls if c[0] == "PUT" and "/pulls/1/merge" in c[1]]
    mp2, fake2, api2 = repo[0], FakeGitHub({ENV["MANIFEST_PATH"]: repo[1].branches["main"][ENV["MANIFEST_PATH"]]}), None
    api2 = GitHub(ENV, http=fake2)
    ci_ops.run_action({**ENV, "ACTION": "fleet-pause", "REASON": "incident", "EMERGENCY": "1", "EMERGENCY_PAUSE_USERS": "bob"}, mp2, api=api2)
    assert not [c for c in fake2.calls if c[0] == "PUT" and "/merge" in c[1]]


def test_trigger_add_goes_to_the_triggers_branch_and_republishes_pages(repo):
    mp, fake, api = repo
    res = ci_ops.run_action({**ENV, "ACTION": "trigger-add", "TARGET": "auto-a", "PARAMS": '{"slot":"poll","params":{"date_range":"yesterday"}}'},
                            mp, api=api)
    assert res["changed"] and "trigger" in res
    assert "triggers" in fake.branches                                          # created from main
    doc = json.loads(fake.branches["triggers"]["components/automation-registry/triggers.json"])
    assert doc["triggers"][0]["automation"] == "auto-a" and doc["triggers"][0]["params"] == {"date_range": "yesterday"}
    assert not fake.prs                                                         # no manifest change, no PR
    assert fake.dispatches and fake.dispatches[0][0].endswith("/actions/workflows/site.yml/dispatches")
    # a second trigger appends to the existing branch file (no re-creation)
    ci_ops.run_action({**ENV, "ACTION": "trigger-add", "TARGET": "auto-a", "PARAMS": '{"slot":"poll"}'}, mp, api=api)
    doc = json.loads(fake.branches["triggers"]["components/automation-registry/triggers.json"])
    assert len(doc["triggers"]) == 2


def test_github_rejects_bad_branch_and_invalid_input(repo):
    mp, fake, api = repo
    with pytest.raises(OpError, match="BRANCH"):
        ci_ops.run_action({**ENV, "ACTION": "fleet-pause", "REASON": "x", "BRANCH": "main"}, mp, api=api)
    with pytest.raises(OpError, match="reason is required"):
        ci_ops.run_action({**ENV, "ACTION": "pause", "TARGET": "auto-a", "REASON": ""}, mp, api=api)
    assert not fake.prs


def test_cli_platform_auto_detects_github_and_refuses_without_token(repo, monkeypatch, capsys):
    mp, fake, api = repo
    from registry.cli import main
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "devpatel135/AutoStation")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("OPS_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ACTION", "fleet-pause")
    monkeypatch.setenv("REASON", "x")
    rc = main(["ops-action", "--git", "--manifest", str(mp), "--result", str(mp.parent / "r.json")])
    assert rc == 2 and "GITHUB_TOKEN" in capsys.readouterr().err


# ---- portal in GitHub mode ----------------------------------------------------------------------------------------
GH_CONFIG = {"platform": "github", "github_repo": "devpatel135/AutoStation", "manifest_path": "components/automation-registry/manifest.yaml",
             "ops_workflow": "ops-action.yml", "default_branch": "main", "ops_ref": "main",
             "installer_url_template": "https://github.com/{repo}/releases/download/installers-latest/{name}-setup.exe"}


def test_installer_urls_come_from_the_template():
    assert installer_rel(GH_CONFIG, "auto-a", "1.0.0") == "https://github.com/devpatel135/AutoStation/releases/download/installers-latest/auto-a-setup.exe"
    assert installer_rel({}, "auto-a", "1.0.0") == "installers/auto-a-1.0.0-setup.exe"
    assert rel_href({}, "installers/x.exe", "../") == "../installers/x.exe"
    assert rel_href(GH_CONFIG, "https://example.com/x.exe", "../") == "https://example.com/x.exe"


def test_portal_in_github_mode_links_to_release_assets_and_github(tmp_path, mpath):
    cfg = tmp_path / "portal.config.json"
    cfg.write_text(json.dumps(GH_CONFIG), encoding="utf-8")
    out = tmp_path / "pub"
    build.build_portal(mpath, out, config_path=cfg)
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "releases/download/installers-latest/auto-a-setup.exe" in index and 'href="installers/' not in index
    detail = (out / "automation" / "auto-a.html").read_text(encoding="utf-8")
    assert "../https://" not in detail and "releases/download/installers-latest/auto-a-setup.exe" in detail
    idx = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert all(v["installer"].startswith("https://github.com/") for a in idx["automations"] for v in a["installers"].values())
    adm = (out / "admin.html").read_text(encoding="utf-8")
    data = json.loads(adm.split('id="admin-data">')[1].split("</script>")[0].replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&"))
    assert data["config"]["platform"] == "github" and data["config"]["github_repo"] == "devpatel135/AutoStation"
    assert "gh workflow run" in adm and "Open the ops-action workflow" in adm


def test_unsafe_github_config_falls_back_to_gitlab_mode():
    assert admin.github_config({**GH_CONFIG, "github_repo": "x/y\"><script>"})["platform"] == "gitlab"
    assert admin.github_config({**GH_CONFIG, "manifest_path": "../../etc/passwd"})["platform"] == "gitlab"
    assert admin.github_config({**GH_CONFIG, "ops_workflow": "x.sh"})["platform"] == "gitlab"
    assert admin.github_config(GH_CONFIG)["platform"] == "github"


def test_github_links_reference_implementation():
    links = admin.build_github_links(GH_CONFIG, {"ACTION": "pause", "TARGET": "auto-a", "REASON": "bad data", "PARAMS": "{}"},
                                     "registry: pause auto-a", "ops-action pause auto-a", "ops/pause-auto-a-x1")
    assert links["workflow"] == "https://github.com/devpatel135/AutoStation/actions/workflows/ops-action.yml"
    assert links["pull_request"].startswith("https://github.com/devpatel135/AutoStation/compare/main...ops%2Fpause-auto-a-x1?quick_pull=1")
    assert links["edit"] == "https://github.com/devpatel135/AutoStation/edit/main/components/automation-registry/manifest.yaml"
    assert "-f action=\"pause\"" in links["gh_command"] and "-f branch=\"ops/pause-auto-a-x1\"" in links["gh_command"]
