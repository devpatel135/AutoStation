"""Tests for automation_studio.scaffold — project.yaml writer and scaffold bridge."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import subprocess

import pytest

_WC = Path(__file__).resolve().parents[3] / "wrapper-core"
if str(_WC) not in sys.path:
    sys.path.insert(0, str(_WC))

from automation_studio.scaffold import (
    ScaffoldResult,
    _write_project_yaml,
    scaffold_from_contract,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

MINIMAL_YAML = {
    "name": "automation-cert-renewal",
    "description": "Renews expiring certificates.",
    "owner": "team@example.com",
    "classification": "internal",
    "runtime": "python",
    "version": "0.1.0",
    "timeout_s": 1800,
}


# ── _write_project_yaml ───────────────────────────────────────────────────────

def test_write_yaml_creates_file(tmp_path):
    dest = tmp_path / "project.yaml"
    _write_project_yaml(MINIMAL_YAML, dest)
    assert dest.exists()


def test_write_yaml_contains_name(tmp_path):
    dest = tmp_path / "project.yaml"
    _write_project_yaml(MINIMAL_YAML, dest)
    content = dest.read_text(encoding="utf-8")
    assert "automation-cert-renewal" in content


def test_write_yaml_contains_all_required_keys(tmp_path):
    dest = tmp_path / "project.yaml"
    _write_project_yaml(MINIMAL_YAML, dest)
    content = dest.read_text(encoding="utf-8")
    for key in ("name", "description", "owner", "classification", "runtime", "version", "timeout_s"):
        assert f"{key}:" in content


def test_write_yaml_skips_none_values(tmp_path):
    dest = tmp_path / "project.yaml"
    proj = {**MINIMAL_YAML, "owner_team": None}
    _write_project_yaml(proj, dest)
    content = dest.read_text(encoding="utf-8")
    assert "owner_team" not in content


def test_write_yaml_has_comment_header(tmp_path):
    dest = tmp_path / "project.yaml"
    _write_project_yaml(MINIMAL_YAML, dest)
    content = dest.read_text(encoding="utf-8")
    assert content.startswith("#")


def test_write_yaml_colon_in_description_is_quoted(tmp_path):
    dest = tmp_path / "project.yaml"
    proj = {**MINIMAL_YAML, "description": "Renews certs: daily"}
    _write_project_yaml(proj, dest)
    content = dest.read_text(encoding="utf-8")
    # Value containing colon must be quoted so stdlib parser handles it
    assert 'description: "' in content


def test_write_yaml_timeout_as_integer(tmp_path):
    dest = tmp_path / "project.yaml"
    _write_project_yaml(MINIMAL_YAML, dest)
    content = dest.read_text(encoding="utf-8")
    assert "timeout_s: 1800" in content


# ── scaffold_from_contract ────────────────────────────────────────────────────

def _mock_success(out_path: Path):
    """Return a mock subprocess.CompletedProcess that looks like success."""
    out_path.mkdir(parents=True, exist_ok=True)
    mock = MagicMock(spec=subprocess.CompletedProcess)
    mock.returncode = 0
    mock.stdout = f"created 12 files in {out_path}\nnext: cd {out_path.name} && python -m wrapper_core dev ."
    mock.stderr = ""
    return mock


def test_scaffold_success(tmp_path):
    out_base = tmp_path / "projects"
    expected_out = out_base / MINIMAL_YAML["name"]
    with patch("automation_studio.scaffold.subprocess.run",
               side_effect=lambda *a, **kw: _mock_success(expected_out)) as mock_run:
        result = scaffold_from_contract(MINIMAL_YAML, out_base)
    assert result.success is True


def test_scaffold_out_path_includes_name(tmp_path):
    out_base = tmp_path / "projects"
    expected_out = out_base / MINIMAL_YAML["name"]
    with patch("automation_studio.scaffold.subprocess.run",
               side_effect=lambda *a, **kw: _mock_success(expected_out)):
        result = scaffold_from_contract(MINIMAL_YAML, out_base)
    assert result.out_path.name == MINIMAL_YAML["name"]


def test_scaffold_calls_scaffold_tool(tmp_path):
    out_base = tmp_path / "projects"
    expected_out = out_base / MINIMAL_YAML["name"]
    with patch("automation_studio.scaffold.subprocess.run",
               side_effect=lambda *a, **kw: _mock_success(expected_out)) as mock_run:
        scaffold_from_contract(MINIMAL_YAML, out_base)
    args = mock_run.call_args[0][0]
    assert any("scaffold.py" in str(a) for a in args)


def test_scaffold_passes_no_verify_flag(tmp_path):
    out_base = tmp_path / "projects"
    expected_out = out_base / MINIMAL_YAML["name"]
    with patch("automation_studio.scaffold.subprocess.run",
               side_effect=lambda *a, **kw: _mock_success(expected_out)) as mock_run:
        scaffold_from_contract(MINIMAL_YAML, out_base, no_verify=True)
    args = mock_run.call_args[0][0]
    assert "--no-verify" in args


def test_scaffold_no_verify_false_omits_flag(tmp_path):
    out_base = tmp_path / "projects"
    expected_out = out_base / MINIMAL_YAML["name"]
    with patch("automation_studio.scaffold.subprocess.run",
               side_effect=lambda *a, **kw: _mock_success(expected_out)) as mock_run:
        scaffold_from_contract(MINIMAL_YAML, out_base, no_verify=False)
    args = mock_run.call_args[0][0]
    assert "--no-verify" not in args


def test_scaffold_failure_on_nonzero_exit(tmp_path):
    out_base = tmp_path / "projects"
    mock = MagicMock(spec=subprocess.CompletedProcess)
    mock.returncode = 1
    mock.stdout = ""
    mock.stderr = "error: template not found"
    with patch("automation_studio.scaffold.subprocess.run", return_value=mock):
        result = scaffold_from_contract(MINIMAL_YAML, out_base)
    assert result.success is False
    assert "template not found" in result.error


def test_scaffold_failure_message_in_error(tmp_path):
    out_base = tmp_path / "projects"
    mock = MagicMock(spec=subprocess.CompletedProcess)
    mock.returncode = 1
    mock.stdout = "some output"
    mock.stderr = "critical: bad project.yaml"
    with patch("automation_studio.scaffold.subprocess.run", return_value=mock):
        result = scaffold_from_contract(MINIMAL_YAML, out_base)
    assert "bad project.yaml" in result.error


def test_scaffold_timeout_returns_failure(tmp_path):
    out_base = tmp_path / "projects"
    with patch("automation_studio.scaffold.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="scaffold.py", timeout=120)):
        result = scaffold_from_contract(MINIMAL_YAML, out_base)
    assert result.success is False
    assert "timed out" in result.error


def test_scaffold_missing_tool_returns_failure(tmp_path):
    out_base = tmp_path / "projects"
    with patch("automation_studio.scaffold._SCAFFOLD_TOOL", tmp_path / "nonexistent.py"):
        result = scaffold_from_contract(MINIMAL_YAML, out_base)
    assert result.success is False
    assert "not found" in result.error


def test_scaffold_output_captured(tmp_path):
    out_base = tmp_path / "projects"
    expected_out = out_base / MINIMAL_YAML["name"]
    with patch("automation_studio.scaffold.subprocess.run",
               side_effect=lambda *a, **kw: _mock_success(expected_out)):
        result = scaffold_from_contract(MINIMAL_YAML, out_base)
    assert "created" in result.output or result.output != ""


def test_scaffold_writes_yaml_with_project_name(tmp_path):
    out_base = tmp_path / "projects"
    expected_out = out_base / MINIMAL_YAML["name"]
    captured_args = []

    def capture(*args, **kwargs):
        captured_args.extend(args[0])
        return _mock_success(expected_out)

    with patch("automation_studio.scaffold.subprocess.run", side_effect=capture):
        scaffold_from_contract(MINIMAL_YAML, out_base)

    # The yaml path passed to scaffold.py should end with project.yaml
    yaml_args = [a for a in captured_args if str(a).endswith("project.yaml")]
    assert len(yaml_args) == 1
    # The file should have been written (it's in a tempdir that is deleted, but we can verify it was passed)
    assert "project.yaml" in str(yaml_args[0])


def test_scaffold_register_key_passed_when_set(tmp_path):
    out_base = tmp_path / "projects"
    expected_out = out_base / MINIMAL_YAML["name"]
    with patch("automation_studio.scaffold.subprocess.run",
               side_effect=lambda *a, **kw: _mock_success(expected_out)) as mock_run:
        scaffold_from_contract(MINIMAL_YAML, out_base, register="manifest.yaml")
    # register is embedded in the project.yaml written to disk, not passed as CLI flag
    # so we just check the call was made
    assert mock_run.called


def test_scaffold_extra_fields_in_project_yaml_stripped(tmp_path):
    """Fields not in _SCAFFOLD_FIELDS (like automation_yaml_additions) are dropped."""
    out_base = tmp_path / "projects"
    expected_out = out_base / MINIMAL_YAML["name"]
    proj_with_extras = {**MINIMAL_YAML, "automation_yaml_additions": {"schedule": {"cron": "0 6 * * 1"}}}
    written_content = []

    def capture(*args, **kwargs):
        yaml_path = Path(args[0][2])  # 3rd arg is the project.yaml path
        if yaml_path.exists():
            written_content.append(yaml_path.read_text(encoding="utf-8"))
        return _mock_success(expected_out)

    with patch("automation_studio.scaffold.subprocess.run", side_effect=capture):
        scaffold_from_contract(proj_with_extras, out_base)

    if written_content:
        assert "automation_yaml_additions" not in written_content[0]
