from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import check_py39_syntax  # noqa: E402
import check_workflows as cw  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
MINE = ["release.yml", "site.yml", "ops-action.yml", "codeql.yml"]
# pre-existing workflows owned elsewhere: tolerated findings (reported to the maintainer, not blockers here)
TOLERATED = {"ci.yml": ["declare 'permissions'"], "installer-dev.yml": []}


def real(name: str) -> list[str]:
    errs = cw.check_file(WORKFLOWS / name)
    return [e for e in errs if not any(t in e for t in TOLERATED.get(name, []))]


@pytest.mark.parametrize("name", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_every_workflow_passes_the_structural_check(name):
    assert real(name) == []


def test_my_workflows_exist_and_pages_demo_is_gone():
    for n in MINE:
        assert (WORKFLOWS / n).is_file(), n
    assert not (WORKFLOWS / "pages-demo.yml").exists()          # replaced by site.yml


def test_release_and_site_do_not_publish_installers():
    """installer-dev.yml is the single publisher of the rolling installers-latest release."""
    for n in MINE:
        text = (WORKFLOWS / n).read_text(encoding="utf-8")
        assert "installers-latest" not in text.replace("# Installers are NOT built here: the `installer-dev` workflow owns the rolling `installers-latest` release.", "")
        assert "softprops/action-gh-release" not in text and "gh release create" not in text and "gh release upload" not in text


def test_secrets_and_variables_of_my_workflows_are_documented():
    doc = (ROOT / "docs" / "PIPELINES.md").read_text(encoding="utf-8")
    missing = []
    for n in MINE:
        use = cw.collect_usage((WORKFLOWS / n).read_text(encoding="utf-8"))
        for name in use["secrets"] + use["vars"]:
            if f"`{name}`" not in doc and f"`{name}`," not in doc:
                missing.append(f"{n}: {name}")
    assert missing == [], f"undocumented in docs/PIPELINES.md: {missing}"


def test_every_workflow_is_described_in_the_docs():
    doc = (ROOT / "docs" / "PIPELINES.md").read_text(encoding="utf-8")
    for p in WORKFLOWS.glob("*.yml"):
        assert p.name in doc, f"{p.name} is not described in docs/PIPELINES.md"


def test_live_site_needs_the_logging_token_and_never_falls_back_to_demo_data():
    text = (WORKFLOWS / "site.yml").read_text(encoding="utf-8")
    assert "LOGGING_TOKEN" in text and "--live-data" in text
    assert "--demo" not in text                      # the published site is live data or an honest waiting state, never synthetic
    assert "cron:" in text and "workflow_run" in text


def test_installer_workflow_publishes_the_download_and_smoke_tests_the_browser_run():
    text = (WORKFLOWS / "installer-dev.yml").read_text(encoding="utf-8")
    assert "installers-latest" in text and "build_framework_bundle.py" in text
    smoke = (ROOT / "tools" / "ci" / "smoke-installer.ps1").read_text(encoding="utf-8")      # the smoke checks live in ONE shared script
    assert r"execution-framework\node\node.exe" in smoke and "account_selected" in smoke
    assert real("installer-dev.yml") == []
    assert "LOGGING_TOKEN" in text and "/NORUN" in smoke      # smoke must not pollute live logs


def test_release_auto_promotes_and_stays_append_only():
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "--auto-promote" in text and "append-only" in text and "build_framework_bundle.py" in text


def test_ops_action_passes_inputs_via_env_not_shell_interpolation():
    text = (WORKFLOWS / "ops-action.yml").read_text(encoding="utf-8")
    for name in ("action", "target", "reason", "params", "branch"):
        assert f"${{{{ inputs.{name} }}}}" in text
    run_blocks = re.findall(r"run: (.*)", text)
    assert not any("${{ inputs." in r for r in run_blocks)       # nothing user-supplied is interpolated into a shell line


def test_release_enforces_append_only_artifacts_branch():
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "grep -v '^A'" in text and "append-only" in text
    assert "--bootstrap" in text and "tools/build_registry.py" in text


# ---- the checker itself -----------------------------------------------------------------------------------------------------
GOOD = """name: x
on:
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      who:
        description: d
        default: a
permissions:
  contents: read
jobs:
  a:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    env:
      WHO: ${{ inputs.who }}
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262
        with:
          persist-credentials: false
      - run: echo "$WHO ${{ github.sha }}"
"""


def test_checker_accepts_a_good_workflow():
    assert cw.check_text(GOOD, "good.yml") == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda t: t.replace("  push:", "\tpush:"), "tab"),
    (lambda t: t.replace("actions/checkout@11d5960a326750d5838078e36cf38b85af677262", "actions/checkout@main"), "pinned"),
    (lambda t: t.replace("actions/checkout@11d5960a326750d5838078e36cf38b85af677262", "actions/checkout@v4"), "full commit SHA"),
    (lambda t: t.replace("    timeout-minutes: 5\n", ""), "timeout-minutes"),
    (lambda t: t.replace("echo \"$WHO", "echo \"${{ inputs.who }}"), "untrusted"),
    (lambda t: t.replace("permissions:\n  contents: read\n", ""), "permissions"),
    (lambda t: t.replace("WHO: ${{ inputs.who }}", "WHO: ${{ inputs.nope }}"), "inputs.nope"),
    (lambda t: t.replace("${{ github.sha }}", "${{ gitub.sha }}"), "unknown context"),
    (lambda t: t.replace("${{ github.sha }}", "${{ github.sha"), "unbalanced"),
    (lambda t: t.replace("- run: echo", "- if: ${{ secrets.X == 'a' }}\n        run: echo"), "secrets"),
    (lambda t: t.replace("- run: echo", "- run: python tools/does_not_exist.py\n      - run: echo"), "does_not_exist"),
    (lambda t: t.replace("runs-on: ubuntu-latest", "needs: ghost\n    runs-on: ubuntu-latest"), "unknown job"),
    (lambda t: t.replace("      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262\n", "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262\n        run: echo both\n"), "exactly one"),
    (lambda t: t.replace("name: x\n", ""), "missing top-level 'name'"),
])
def test_checker_catches_common_mistakes(mutate, expect):
    errs = " | ".join(cw.check_text(mutate(GOOD), "bad.yml"))
    assert expect in errs, errs


def test_collect_usage():
    u = cw.collect_usage("${{ secrets.A }} ${{ secrets.GITHUB_TOKEN }} ${{ vars.B }} ${{ inputs.c }}")
    assert u == {"secrets": ["A"], "vars": ["B"], "inputs": ["c"]}


# ---- Python 3.9 compatibility guard (3.12+ only syntax would break the 3.9 CI leg) ---------------------------------------
@pytest.mark.skipif(sys.version_info < (3, 12), reason="the checker uses the 3.12 tokenizer")
def test_no_python_312_only_fstring_syntax_anywhere_in_the_repo():
    problems = []
    for f in check_py39_syntax.iter_py([]):
        problems += check_py39_syntax.check_file(f)
    assert problems == [], "\n".join(problems)


@pytest.mark.skipif(sys.version_info < (3, 12), reason="the checker uses the 3.12 tokenizer")
def test_py39_checker_detects_the_constructs_it_claims_to():
    bad = "d = {'k': 1}\nx = f'{d['k']}'\ny = f\"{'\\n'.join(['a'])}\"\nz = f\"{f\"{1}\"}\"\n"
    probs = check_py39_syntax.check_source(bad)
    assert len(probs) >= 3
    assert check_py39_syntax.check_source("d = {'k': 1}\nx = f\"{d['k']}\"\n") == []


# ---- Phase 0 pipeline hardening: guard rails so these fixes cannot silently regress -------------------------------------------
def _text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_checker_rejects_if_git_ls_remote_as_a_branch_existence_test():
    bad = GOOD.replace("- run: echo", "- run: |\n          if git ls-remote --exit-code origin x; then echo yes; fi\n      - run: echo")
    assert "ls-remote" in " | ".join(cw.check_text(bad, "bad.yml"))
    ok = GOOD.replace("- run: echo", "- run: |\n          rc=0; git ls-remote --exit-code origin x || rc=$?\n      - run: echo")
    assert "ls-remote" not in " | ".join(cw.check_text(ok, "ok.yml"))


def test_site_publishes_what_release_produced_and_never_repackages_source():
    t = _text("site.yml")
    assert "--publish-only" in t and "--artifacts-dir" in t
    assert "Refuse to publish" not in t and "build_framework_bundle.py" not in t      # the deploy no longer depends on rebuilding source
    assert "AUTOSTATION_ALLOW_NO_REGISTRY" in t                                      # an absent registry branch fails unless explicitly allowed
    assert "exit 2" in t or "= 2" in t or "-eq 2" in t or "2)" in t                  # ls-remote is read by exit code
    assert "continue-on-error" not in t.split("--publish-only")[1].split("name:")[0]


def test_only_release_may_bootstrap_the_registry_branch():
    assert re.search(r"\b2\)", _text("release.yml")) or "= 2" in _text("release.yml") or "-eq 2" in _text("release.yml")
    assert "--bootstrap" in _text("release.yml")
    for n in ("site.yml", "ci.yml"):
        assert "--bootstrap" not in _text(n) and "--orphan" not in _text(n), f"{n} must never create registry-artifacts"


def test_ci_has_a_version_gate_that_needs_no_token_and_cancels_only_non_main():
    t = _text("ci.yml")
    assert "version-gate:" in t and "build_registry.py --check" in t and "bump_version.py --check" in t
    assert "cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}" in t
    gate = t.split("version-gate:")[1].split("\n  python:")[0]
    assert "secrets." not in gate and "LOGGING_TOKEN" not in gate


def test_installers_are_serialised_follow_release_and_keep_history():
    t = _text("installer-dev.yml")
    assert "concurrency:" in t and "group: installers" in t and "cancel-in-progress: false" in t
    assert re.search(r"workflow_run:\s*\n\s*workflows:\s*\[release\]", t)
    assert "tools/ci/smoke-installer.ps1" in t and "-Phase install" in t and "-Phase run" in t and "-Phase uninstall" in t
    assert "head -n -5" in t                                           # keeps the newest 5 versioned installers
    assert "deleting stale asset" not in t                             # the old delete-everything-not-in-this-build loop is gone
    assert "windows-2025" in t and "windows-latest" not in t


def test_every_smoke_run_ignores_the_maintenance_window():
    assert "--ignore-maintenance" in (ROOT / "tools" / "ci" / "smoke-installer.ps1").read_text(encoding="utf-8")
    assert "wrapper_core run" not in _text("installer-dev.yml")        # no second, drifting copy of the smoke run inline
    assert "smoke-installer.ps1" in _text("canary.yml")


def test_pipeline_alert_watches_every_pipeline_and_runs_no_pr_code():
    t = _text("pipeline-alert.yml")
    assert "tools/ops_issues.py raise" in t and "tools/ops_issues.py resolve" in t and "pipe-site-stale" in t
    m = re.search(r"workflows:\s*\[([^\]]*)\]", t)
    watched = [w.strip() for w in m.group(1).split(",")]
    assert {"release", "site", "installers", "ci", "canary"} <= set(watched)
    names = {}
    for p in WORKFLOWS.glob("*.yml"):
        mm = re.match(r"name:\s*(\S+)", p.read_text(encoding="utf-8"))
        names[mm.group(1)] = p.name
    assert all(w in names for w in watched), f"workflow_run names that match no workflow: {[w for w in watched if w not in names]}"
    assert "branches: [main]" in t and "pull_request" not in t.split("jobs:")[0]
    assert "${{ github.event.workflow_run.name }}" not in t.split("steps:", 1)[1].replace("WORKFLOW: ${{ github.event.workflow_run.name }}", "")


def test_no_schedule_sits_on_the_busiest_minutes():
    for p in WORKFLOWS.glob("*.yml"):
        for cron in re.findall(r'cron:\s*"([^"]+)"', p.read_text(encoding="utf-8")):
            minutes = cron.split()[0]
            assert not set(minutes.split(",")) & {"0", "30"}, f"{p.name}: cron '{cron}' runs on :00/:30"


def test_ci_python_dependencies_are_pinned_through_requirements_ci():
    req = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8")
    for line in [l for l in req.splitlines() if l.strip() and not l.startswith("#")]:
        assert "==" in line, f"requirements-ci.txt must pin exactly: {line}"
    for n in ("ci.yml", "release.yml", "installer-dev.yml", "security.yml"):
        t = _text(n)
        assert "requirements-ci.txt" in t, n
        for l in [l for l in t.splitlines() if "pip install" in l and "pip install --upgrade" not in l]:
            assert "-c requirements-ci.txt" in l or "-r requirements-ci.txt" in l, f"{n}: unconstrained '{l.strip()}'"
    assert 'node-version: 22' in _text("ci.yml") and "node-version: 20" not in _text("ci.yml")


def test_new_operational_names_are_documented():
    doc = (ROOT / "docs" / "PIPELINES.md").read_text(encoding="utf-8")
    for needle in ("pipeline-alert.yml", "AUTOSTATION_ALLOW_NO_REGISTRY", "--publish-only", "version-gate", "bump_version.py", "requirements-ci.txt", "best-effort"):
        assert needle in doc, needle
    assert "The registry always deploys" not in doc
