"""Tests for tools/scaffold.py — project.yaml loading, validation, and file emission."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scaffold as sc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def write_project(d: Path, **overrides) -> Path:
    base = {
        "name":           "automation-test-widget",
        "description":    "Test widget automation",
        "owner":          "widget-team@example.com",
        "classification": "internal",
    }
    base.update(overrides)
    lines = ["# project.yaml"]
    for k, v in base.items():
        lines.append(f"{k}: {v}")
    p = d / "project.yaml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# load_project: valid input
# ---------------------------------------------------------------------------

def test_load_project_minimal(tmp_path):
    p = write_project(tmp_path)
    proj = sc.load_project(p)
    assert proj["name"] == "automation-test-widget"
    assert proj["runtime"] == "python"            # default filled
    assert proj["version"] == "0.1.0"             # default filled
    assert proj["owner_team"] == "Automation Platform"
    assert proj["timeout_s"] == 1800              # cast to int


def test_load_project_explicit_runtime(tmp_path):
    p = write_project(tmp_path, runtime="transactional")
    proj = sc.load_project(p)
    assert proj["runtime"] == "transactional"


def test_load_project_explicit_version(tmp_path):
    p = write_project(tmp_path, version="2.0.1")
    proj = sc.load_project(p)
    assert proj["version"] == "2.0.1"


def test_load_project_custom_owner_team(tmp_path):
    p = write_project(tmp_path, **{"owner_team": "FinOps Automation"})
    proj = sc.load_project(p)
    assert proj["owner_team"] == "FinOps Automation"


def test_load_project_register_key(tmp_path):
    p = write_project(tmp_path, register="some/manifest.yaml")
    proj = sc.load_project(p)
    assert proj["register"] == "some/manifest.yaml"


# ---------------------------------------------------------------------------
# load_project: invalid input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("patch,match", [
    ({"name": "bad-name"},                   "name must match"),
    ({"name": "expense-checker"},            "name must match"),   # missing "automation-" prefix
    ({"owner": "not-an-email"},              "email"),
    ({"classification": "top-secret"},       "classification"),
    ({"runtime": "perl"},                    "runtime"),
    ({"version": "banana"},                  "version"),
    ({"version": "1.0"},                     "version"),            # not x.y.z
    ({"timeout_s": "not-a-number"},          "timeout_s"),
])
def test_load_project_validation_errors(tmp_path, patch, match):
    p = write_project(tmp_path, **patch)
    with pytest.raises(sc.ProjectError, match=match):
        sc.load_project(p)


def test_load_project_missing_required_owner(tmp_path):
    proj_text = (
        "name: automation-test-widget\n"
        "description: test\n"
        "classification: internal\n"
    )
    p = tmp_path / "project.yaml"
    p.write_text(proj_text, encoding="utf-8")
    with pytest.raises(sc.ProjectError, match="owner"):
        sc.load_project(p)


def test_load_project_unknown_key(tmp_path):
    p = write_project(tmp_path, mystery_key="oops")
    with pytest.raises(sc.ProjectError, match="unknown"):
        sc.load_project(p)


# ---------------------------------------------------------------------------
# scaffold: file emission
# ---------------------------------------------------------------------------

def test_scaffold_emits_expected_files(tmp_path):
    p = write_project(tmp_path)
    proj = sc.load_project(p)
    out = tmp_path / "out"
    files = sc.scaffold(proj, out)
    paths = {f.relative_to(out).as_posix() for f in files}
    assert "main.py" in paths
    assert "automation.yaml" in paths
    assert "tests/test_main.py" in paths


def test_scaffold_fills_name_in_main(tmp_path):
    p = write_project(tmp_path)
    proj = sc.load_project(p)
    out = tmp_path / "out"
    sc.scaffold(proj, out)
    main_text = (out / "main.py").read_text(encoding="utf-8")
    assert "automation-test-widget" in main_text


def test_scaffold_fills_queue_name_for_transactional(tmp_path):
    p = write_project(tmp_path, runtime="transactional")
    proj = sc.load_project(p)
    out = tmp_path / "out"
    sc.scaffold(proj, out)
    main_text = (out / "main.py").read_text(encoding="utf-8")
    assert "test-widget" in main_text       # queue_name strips "automation-" prefix


def test_scaffold_fills_description_in_main(tmp_path):
    p = write_project(tmp_path)
    proj = sc.load_project(p)
    out = tmp_path / "out"
    sc.scaffold(proj, out)
    main_text = (out / "main.py").read_text(encoding="utf-8")
    assert "Test widget automation" in main_text


def test_scaffold_automation_yaml_has_name(tmp_path):
    p = write_project(tmp_path)
    proj = sc.load_project(p)
    out = tmp_path / "out"
    sc.scaffold(proj, out)
    ay = (out / "automation.yaml").read_text(encoding="utf-8")
    assert "automation-test-widget" in ay


def test_scaffold_uses_wrapper_core(tmp_path):
    p = write_project(tmp_path)
    proj = sc.load_project(p)
    out = tmp_path / "out"
    sc.scaffold(proj, out)
    main_text = (out / "main.py").read_text(encoding="utf-8")
    assert "wrapper_core" in main_text


def test_scaffold_main_has_entrypoint_decorator(tmp_path):
    p = write_project(tmp_path)
    proj = sc.load_project(p)
    out = tmp_path / "out"
    sc.scaffold(proj, out)
    main_text = (out / "main.py").read_text(encoding="utf-8")
    assert "@wrapper.entrypoint" in main_text


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------

def test_main_missing_project_yaml(tmp_path):
    result = sc.main([str(tmp_path / "nonexistent.yaml")])
    assert result == 1


def test_main_scaffold_creates_files_no_verify(tmp_path):
    p = write_project(tmp_path)
    out = tmp_path / "automation-test-widget"
    result = sc.main([str(p), "--out", str(out), "--no-verify"])
    assert result == 0
    assert (out / "main.py").exists()
    assert (out / "automation.yaml").exists()
    assert (out / "tests" / "test_main.py").exists()


def test_main_refuses_nonempty_out_dir(tmp_path):
    p = write_project(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "existing.txt").write_text("hi")
    result = sc.main([str(p), "--out", str(out), "--no-verify"])
    assert result == 1


def test_main_bad_project_yaml_returns_1(tmp_path):
    bad = tmp_path / "project.yaml"
    bad.write_text("name: not-valid-name\ndescription: x\nowner: noat\nclassification: internal\n")
    out = tmp_path / "out"
    result = sc.main([str(bad), "--out", str(out), "--no-verify"])
    assert result == 1


def test_main_default_project_yaml_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_project(tmp_path)          # writes ./project.yaml
    out = tmp_path / "automation-test-widget"
    result = sc.main(["--out", str(out), "--no-verify"])
    assert result == 0
    assert (out / "main.py").exists()
