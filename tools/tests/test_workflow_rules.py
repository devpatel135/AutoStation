"""The workflow checker's supply-chain rules: third-party actions, credential persistence, secrets and untrusted contexts."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_workflows as cw  # noqa: E402

SHA = "11d5960a326750d5838078e36cf38b85af677262"
BASE = """name: x
on:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  a:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@SHA
        with:
          persist-credentials: false
      - run: echo hello
""".replace("SHA", SHA)


def errs(text: str) -> str:
    return " | ".join(cw.check_text(text, "t.yml"))


def test_base_workflow_is_clean():
    assert cw.check_text(BASE, "t.yml") == []


def test_third_party_actions_are_rejected_even_when_pinned():
    bad = BASE.replace("- run: echo hello", "- uses: someone/else-action@" + SHA)
    assert "not GitHub-owned" in errs(bad)
    assert cw.check_text(BASE.replace("- run: echo hello", "- uses: github/codeql-action/init@" + SHA), "t.yml") == []


def test_checkout_must_not_persist_credentials_unless_the_job_pushes():
    no_flag = BASE.replace("        with:\n          persist-credentials: false\n", "")
    assert "persist-credentials" in errs(no_flag)
    pushing = no_flag.replace("echo hello", "git push origin HEAD:main")
    assert cw.check_text(pushing, "t.yml") == []
    api_pushing = no_flag.replace("echo hello", "python -m registry ops-action --git")
    assert cw.check_text(api_pushing, "t.yml") == []


def test_pull_request_target_is_banned_and_pull_request_cannot_use_secrets():
    assert "pull_request_target" in errs(BASE.replace("  push:\n    branches: [main]", "  pull_request_target:"))
    pr = BASE.replace("  push:\n    branches: [main]", "  pull_request:")
    assert cw.check_text(pr, "t.yml") == []
    with_secret = pr.replace("- run: echo hello", "- run: echo hello\n        env:\n          T: ${{ secrets.TOKEN }}")
    assert "must not use secrets" in errs(with_secret)
    token_only = pr.replace("- run: echo hello", "- run: echo hello\n        env:\n          T: ${{ secrets.GITHUB_TOKEN }}")
    assert cw.check_text(token_only, "t.yml") == []


@pytest.mark.parametrize("expr", ["github.ref_name", "github.head_ref", "github.actor", "github.event.pull_request.title", "inputs.x"])
def test_untrusted_contexts_are_refused_inside_shell_scripts(expr):
    bad = BASE.replace("echo hello", "gh issue create --title ${{ " + expr + " }}")
    assert "untrusted" in errs(bad) or "inputs.x" in errs(bad)


def test_secrets_must_not_be_interpolated_into_scripts():
    bad = BASE.replace("echo hello", "curl -H 'Authorization: Bearer ${{ secrets.TOKEN }}' https://example.test")
    assert "secret interpolated into a shell script" in errs(bad)


def test_safe_contexts_in_scripts_stay_allowed():
    ok = BASE.replace("echo hello", "echo ${{ github.sha }} ${{ github.run_id }} ${{ github.repository }}")
    assert cw.check_text(ok, "t.yml") == []


def test_every_real_workflow_obeys_the_rules():
    wf = sorted((Path(__file__).resolve().parents[2] / ".github" / "workflows").glob("*.yml"))
    assert len(wf) >= 9
    for f in wf:
        assert cw.check_file(f) == [], f.name
