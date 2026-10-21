from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_posture as cp  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def ruleset(include, rules=("deletion", "non_fast_forward"), enforcement="active", target="branch"):
    return {"enforcement": enforcement, "target": target, "conditions": {"ref_name": {"include": include}}, "rules": [{"type": t} for t in rules]}


ALL3 = [ruleset(["~DEFAULT_BRANCH"]), ruleset(["refs/heads/registry-artifacts"]), ruleset(["refs/heads/triggers"])]


def test_rulesets_pass_only_when_every_branch_is_protected_by_active_rules():
    assert [r[1] for r in cp.evaluate_rulesets(ALL3)] == ["PASS", "PASS", "PASS"]
    weak = cp.evaluate_rulesets([ruleset(["~DEFAULT_BRANCH"], rules=("deletion",)), ruleset(["refs/heads/registry-artifacts"], enforcement="disabled")])
    assert [r[1] for r in weak] == ["FAIL", "FAIL", "FAIL"]
    assert [r[1] for r in cp.evaluate_rulesets([])] == ["FAIL", "FAIL", "FAIL"]


def test_default_branch_may_be_named_explicitly_but_bypass_actors_defeat_a_ruleset():
    named = [ruleset(["refs/heads/main"]), ALL3[1], ALL3[2]]
    assert [r[1] for r in cp.evaluate_rulesets(named)] == ["PASS", "PASS", "PASS"]
    bypass = dict(ALL3[0], bypass_actors=[{"actor_id": 5, "actor_type": "RepositoryRole"}])
    res = cp.evaluate_rulesets([bypass, ALL3[1], ALL3[2]])
    assert res[0][1] == "FAIL" and "bypass" in res[0][2]


def test_repo_security_settings_are_skipped_not_passed_when_unreadable():
    assert cp.evaluate_repo({})[0][1] == "SKIP"
    on = {"security_and_analysis": {"secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "enabled"}}}
    assert cp.evaluate_repo(on)[0][1] == "PASS"
    off = {"security_and_analysis": {"secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "disabled"}}}
    assert cp.evaluate_repo(off)[0][1] == "FAIL"


def test_workflow_permission_default():
    assert cp.evaluate_workflow_permissions(None)[0][1] == "SKIP"
    assert cp.evaluate_workflow_permissions({"default_workflow_permissions": "read"})[0][1] == "PASS"
    assert cp.evaluate_workflow_permissions({"default_workflow_permissions": "write"})[0][1] == "FAIL"
    both = cp.evaluate_workflow_permissions({"default_workflow_permissions": "read", "can_approve_pull_request_reviews": True})
    assert both[1][1] == "FAIL" and "approve" in both[1][2]


def test_markdown_cells_cannot_break_the_table():
    md = cp.render_markdown([("a|b\nc", "FAIL", "x|y\r\nz")])
    assert md.count("\n| ") == 2 and "a b c" in md and "x y z" in md and "**FAIL**" in md


def test_the_real_repo_passes_the_static_checks():
    results = cp.check_files(ROOT) + cp.check_workflow_rules(ROOT)
    assert [r for r in results if r[1] == "FAIL"] == [], results


class FakeResp:
    def __init__(self, status, data):
        self.status, self._d = status, json.dumps(data).encode()

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_live_checks_with_a_fake_api():
    def opener(req, timeout=0):
        url = req.full_url
        if url.endswith("/rulesets"):
            return FakeResp(200, [{"id": 1}, {"id": 2}, {"id": 3}])
        if url.endswith("/rulesets/3"):
            return FakeResp(200, ruleset(["refs/heads/triggers"]))
        if url.endswith("/rulesets/1"):
            return FakeResp(200, ruleset(["~DEFAULT_BRANCH"]))
        if url.endswith("/rulesets/2"):
            return FakeResp(200, ruleset(["refs/heads/registry-artifacts"]))
        if url.endswith("/actions/permissions/workflow"):
            return FakeResp(200, {"default_workflow_permissions": "read"})
        return FakeResp(200, {"security_and_analysis": {"secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "enabled"}}})
    results = cp.live_checks("o/r", "token", opener)
    assert [r[1] for r in results] == ["PASS"] * 6
