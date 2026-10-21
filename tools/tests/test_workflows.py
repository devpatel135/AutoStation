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
    assert r"execution-framework\node\node.exe" in text and "account_selected" in text
    assert real("installer-dev.yml") == []
    assert "LOGGING_TOKEN" in text and "/NORUN" in text      # smoke must not pollute live logs


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
    steps:
      - uses: actions/checkout@v4
      - run: echo "${{ inputs.who }} ${{ github.sha }}"
"""


def test_checker_accepts_a_good_workflow():
    assert cw.check_text(GOOD, "good.yml") == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda t: t.replace("  push:", "\tpush:"), "tab"),
    (lambda t: t.replace("actions/checkout@v4", "actions/checkout@main"), "pinned"),
    (lambda t: t.replace("permissions:\n  contents: read\n", ""), "permissions"),
    (lambda t: t.replace("${{ inputs.who }}", "${{ inputs.nope }}"), "inputs.nope"),
    (lambda t: t.replace("${{ github.sha }}", "${{ gitub.sha }}"), "unknown context"),
    (lambda t: t.replace("${{ github.sha }}", "${{ github.sha"), "unbalanced"),
    (lambda t: t.replace("- run: echo", "- if: ${{ secrets.X == 'a' }}\n        run: echo"), "secrets"),
    (lambda t: t.replace("- run: echo", "- run: python tools/does_not_exist.py\n      - run: echo"), "does_not_exist"),
    (lambda t: t.replace("runs-on: ubuntu-latest", "needs: ghost\n    runs-on: ubuntu-latest"), "unknown job"),
    (lambda t: t.replace("      - uses: actions/checkout@v4\n", "      - uses: actions/checkout@v4\n        run: echo both\n"), "exactly one"),
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
