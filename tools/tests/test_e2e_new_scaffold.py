"""E2E smoke test: autostation new → scaffold → scaffolded tests green.

Calls the real scaffold (no mocking) so template bugs and import gaps are caught
before they reach a developer's machine.  LLM-dependent steps (design, testgen,
review) are skipped so the test stays offline and fast.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
WC_DIR = TOOLS.parent / "components" / "wrapper-core"

sys.path.insert(0, str(TOOLS))
import guided_new
import scaffold as sc


# ── helpers ───────────────────────────────────────────────────────────────────

def _base_env() -> dict:
    """Env for subprocess calls: wrapper-core on PYTHONPATH, tray/toast suppressed."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(WC_DIR), env.get("PYTHONPATH", "")]))
    env["AUTOMAGIX_NO_TRAY"] = "1"
    env["AUTOMAGIX_NO_TOAST"] = "1"
    return env


def _run_guided_new(out: Path, *, name="smoke", runtime="python") -> int:
    """Run guided_new with real scaffold but no LLM steps."""
    return guided_new.main([
        name,
        "--goal", "Smoke-test automation for E2E pipeline validation.",
        "--owner", "platform-team@example.com",
        "--runtime", runtime,
        "--out", str(out),
        "--no-verify",   # don't run pytest from within scaffold (we do it ourselves)
        "--no-testgen",  # skip LLM
        "--no-review",   # skip LLM
    ])


# ── structure ─────────────────────────────────────────────────────────────────

def test_guided_new_returns_zero(tmp_path):
    out = tmp_path / "automation-smoke"
    assert _run_guided_new(out) == 0


def test_scaffolded_directory_has_required_files(tmp_path):
    out = tmp_path / "automation-smoke"
    _run_guided_new(out)
    for rel in ("main.py", "automation.yaml", "project.yaml", "README.md"):
        assert (out / rel).exists(), f"missing {rel}"


def test_scaffolded_tests_directory_exists(tmp_path):
    out = tmp_path / "automation-smoke"
    _run_guided_new(out)
    assert (out / "tests").is_dir()
    assert any((out / "tests").glob("test_*.py")), "no test_*.py found in tests/"


def test_project_yaml_written_into_output(tmp_path):
    out = tmp_path / "automation-smoke"
    _run_guided_new(out)
    text = (out / "project.yaml").read_text(encoding="utf-8")
    assert "automation-smoke" in text
    assert "platform-team@example.com" in text


# ── content correctness ───────────────────────────────────────────────────────

def test_automation_yaml_has_required_fields(tmp_path):
    out = tmp_path / "automation-smoke"
    _run_guided_new(out)
    text = (out / "automation.yaml").read_text(encoding="utf-8")
    for key in ("name:", "entrypoint:", "runtime:"):
        assert key in text, f"automation.yaml missing '{key}'"


def test_automation_yaml_name_matches(tmp_path):
    out = tmp_path / "automation-smoke"
    _run_guided_new(out)
    assert "automation-smoke" in (out / "automation.yaml").read_text(encoding="utf-8")


def test_main_py_is_valid_python(tmp_path):
    out = tmp_path / "automation-smoke"
    _run_guided_new(out)
    src = (out / "main.py").read_text(encoding="utf-8")
    compile(src, "main.py", "exec")  # raises SyntaxError if broken


def test_main_py_imports_wrapper_core(tmp_path):
    out = tmp_path / "automation-smoke"
    _run_guided_new(out)
    assert "wrapper_core" in (out / "main.py").read_text(encoding="utf-8")


def test_main_py_has_entrypoint_decorator(tmp_path):
    out = tmp_path / "automation-smoke"
    _run_guided_new(out)
    assert "@wrapper.entrypoint" in (out / "main.py").read_text(encoding="utf-8")


# ── scaffolded tests run green ────────────────────────────────────────────────

def test_scaffolded_tests_pass(tmp_path):
    """The generated test suite must run green against the generated payload."""
    out = tmp_path / "automation-smoke"
    _run_guided_new(out)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"],
        cwd=out,
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, (
        f"Scaffolded tests failed.\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


# ── transactional runtime variant ─────────────────────────────────────────────

def test_transactional_scaffold_has_required_files(tmp_path):
    out = tmp_path / "automation-smoke-tx"
    rc = guided_new.main([
        "smoke-tx",
        "--goal", "Transactional smoke test.",
        "--owner", "platform-team@example.com",
        "--runtime", "transactional",
        "--out", str(out),
        "--no-verify", "--no-testgen", "--no-review",
    ])
    assert rc == 0
    for rel in ("main.py", "automation.yaml", "project.yaml"):
        assert (out / rel).exists(), f"transactional scaffold missing {rel}"


def test_transactional_scaffolded_tests_pass(tmp_path):
    out = tmp_path / "automation-smoke-tx"
    guided_new.main([
        "smoke-tx",
        "--goal", "Transactional smoke test.",
        "--owner", "platform-team@example.com",
        "--runtime", "transactional",
        "--out", str(out),
        "--no-verify", "--no-testgen", "--no-review",
    ])
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"],
        cwd=out,
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, (
        f"Transactional scaffolded tests failed.\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


# ── scaffold.py direct API ────────────────────────────────────────────────────

def test_scaffold_api_load_and_emit(tmp_path):
    """scaffold.load_project + scaffold.scaffold() work without going through subprocess."""
    proj_file = tmp_path / "project.yaml"
    proj_file.write_text(
        "name: automation-api-smoke\n"
        "description: API-level scaffold test\n"
        "owner: test@example.com\n"
        "classification: internal\n",
        encoding="utf-8",
    )
    proj = sc.load_project(proj_file)
    out = tmp_path / "automation-api-smoke"
    files = sc.scaffold(proj, out)
    assert files, "scaffold emitted no files"
    assert (out / "main.py").exists()
    assert (out / "automation.yaml").exists()


def test_scaffold_api_all_files_are_valid_python_or_yaml(tmp_path):
    proj_file = tmp_path / "project.yaml"
    proj_file.write_text(
        "name: automation-api-smoke\n"
        "description: Validity check\n"
        "owner: test@example.com\n"
        "classification: internal\n",
        encoding="utf-8",
    )
    proj = sc.load_project(proj_file)
    out = tmp_path / "automation-api-smoke"
    files = sc.scaffold(proj, out)
    for f in files:
        if f.suffix == ".py":
            src = f.read_text(encoding="utf-8")
            compile(src, str(f.relative_to(out)), "exec")
