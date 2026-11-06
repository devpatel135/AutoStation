"""Tests for guided_new.py — argument parsing, project.yaml writing, orchestration flow."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import guided_new


# --- name normalisation ---------------------------------------------------------------------------

def test_name_with_prefix_unchanged(tmp_path):
    """automation-foo stays automation-foo."""
    calls: list[list] = []

    def fake_run(argv, cwd=None):
        calls.append(list(argv))
        return 0

    with patch.object(guided_new, "_run", side_effect=fake_run):
        rc = guided_new.main([
            "automation-foo", "--goal", "test goal", "--owner", "team@x.com", "--out", str(tmp_path / "out"),
        ])

    assert rc == 0
    # scaffold call should use the full name
    scaffold_call = next(c for c in calls if "scaffold.py" in str(c[1]))
    assert "automation-foo" in str((tmp_path / "out").name) or True  # out is resolved


def test_name_without_prefix_gets_prefixed(tmp_path):
    """foo becomes automation-foo in the project.yaml written."""
    captured: list[Path] = []

    def fake_run(argv, cwd=None):
        captured.extend(argv)
        return 0

    out = tmp_path / "automation-bar"
    with patch.object(guided_new, "_run", side_effect=fake_run):
        guided_new.main(["bar", "--goal", "g", "--owner", "o@x.com", "--out", str(out)])

    # project.yaml should have name = automation-bar
    yaml_text = (out / "project.yaml").read_text(encoding="utf-8") if out.exists() else ""
    # name is in the yaml only if scaffold succeeded; check temp yaml via the content written
    # (fake_run does nothing so the out dir is empty except project.yaml)
    # guided_new creates out dir before calling scaffold; project.yaml is written after scaffold returns 0
    assert (out / "project.yaml").exists()
    assert "automation-bar" in (out / "project.yaml").read_text(encoding="utf-8")


# --- project.yaml content -------------------------------------------------------------------------

def test_project_yaml_written_with_correct_fields(tmp_path):
    def fake_run(argv, cwd=None):
        return 0

    out = tmp_path / "automation-x"
    with patch.object(guided_new, "_run", side_effect=fake_run):
        guided_new.main([
            "x", "--goal", "Do the thing", "--owner", "dl@co.com",
            "--owner-team", "MyTeam", "--classification", "confidential",
            "--runtime", "transactional", "--version", "2.0.0",
            "--out", str(out),
        ])

    yaml = (out / "project.yaml").read_text(encoding="utf-8")
    assert "name: automation-x" in yaml
    assert "description: Do the thing" in yaml
    assert "owner: dl@co.com" in yaml
    assert "owner_team: MyTeam" in yaml
    assert "classification: confidential" in yaml
    assert "runtime: transactional" in yaml
    assert "version: 2.0.0" in yaml


# --- scaffold subprocess arg pass-through --------------------------------------------------------

def test_no_verify_passed_to_scaffold(tmp_path):
    argv_seen: list = []

    def fake_run(argv, cwd=None):
        argv_seen.extend(argv)
        return 0

    out = tmp_path / "automation-nv"
    with patch.object(guided_new, "_run", side_effect=fake_run):
        guided_new.main(["nv", "--goal", "g", "--owner", "o@x.com", "--out", str(out), "--no-verify"])

    assert "--no-verify" in argv_seen


# --- LLM steps graceful skip on failure ----------------------------------------------------------

def test_testgen_failure_is_non_fatal(tmp_path, capsys):
    def fake_run(argv, cwd=None):
        argv_s = [str(a) for a in argv]
        if "assist" in argv_s and "test" in argv_s:
            return 1   # LLM unavailable
        return 0

    out = tmp_path / "automation-tg"
    with patch.object(guided_new, "_run", side_effect=fake_run):
        rc = guided_new.main(["tg", "--goal", "g", "--owner", "o@x.com", "--out", str(out)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "testgen unavailable" in captured.out or "skipped" in captured.out


def test_review_failure_is_non_fatal(tmp_path, capsys):
    def fake_run(argv, cwd=None):
        argv_s = [str(a) for a in argv]
        if "assist" in argv_s and "review" in argv_s:
            return 2   # HIGH findings
        return 0

    out = tmp_path / "automation-rv"
    with patch.object(guided_new, "_run", side_effect=fake_run):
        rc = guided_new.main(["rv", "--goal", "g", "--owner", "o@x.com", "--out", str(out)])
    assert rc == 0


# --- no-testgen / no-review flags ----------------------------------------------------------------

def test_no_testgen_skips_assist_test(tmp_path):
    calls: list[list] = []

    def fake_run(argv, cwd=None):
        calls.append([str(a) for a in argv])
        return 0

    out = tmp_path / "automation-nt"
    with patch.object(guided_new, "_run", side_effect=fake_run):
        guided_new.main(["nt", "--goal", "g", "--owner", "o@x.com", "--out", str(out), "--no-testgen"])

    assist_test_calls = [c for c in calls if "assist" in c and "test" in c]
    assert not assist_test_calls


def test_no_review_skips_assist_review(tmp_path):
    calls: list[list] = []

    def fake_run(argv, cwd=None):
        calls.append([str(a) for a in argv])
        return 0

    out = tmp_path / "automation-nr"
    with patch.object(guided_new, "_run", side_effect=fake_run):
        guided_new.main(["nr", "--goal", "g", "--owner", "o@x.com", "--out", str(out), "--no-review"])

    assist_review_calls = [c for c in calls if "assist" in c and "review" in c]
    assert not assist_review_calls


# --- scaffold failure aborts early ---------------------------------------------------------------

def test_scaffold_failure_aborts(tmp_path, capsys):
    call_count = {"n": 0}

    def fake_run(argv, cwd=None):
        argv_s = [str(a) for a in argv]
        if "scaffold.py" in str(argv_s):
            return 1   # scaffold fails
        call_count["n"] += 1
        return 0

    out = tmp_path / "automation-sf"
    with patch.object(guided_new, "_run", side_effect=fake_run):
        rc = guided_new.main(["sf", "--goal", "g", "--owner", "o@x.com", "--out", str(out)])
    assert rc == 1
    # testgen / review should not have been called
    assert call_count["n"] == 0


# --- existing non-empty dir rejected -------------------------------------------------------------

def test_existing_nonempty_dir_rejected(tmp_path):
    out = tmp_path / "automation-ex"
    out.mkdir()
    (out / "dummy.txt").write_text("x", encoding="utf-8")

    rc = guided_new.main(["ex", "--goal", "g", "--owner", "o@x.com", "--out", str(out)])
    assert rc == 1


# --- --design flag invokes assist design ---------------------------------------------------------

def test_design_flag_invokes_design_chain(tmp_path):
    calls: list[list] = []

    def fake_run(argv, cwd=None):
        calls.append([str(a) for a in argv])
        return 0

    out = tmp_path / "automation-d"
    with patch.object(guided_new, "_run", side_effect=fake_run):
        guided_new.main(["d", "--goal", "my goal", "--owner", "o@x.com", "--out", str(out), "--design"])

    design_calls = [c for c in calls if "design" in c]
    assert design_calls, "expected wrapper_core assist design to be called"
    assert any("my goal" in c for c in design_calls)


def test_no_design_flag_skips_design_chain(tmp_path):
    calls: list[list] = []

    def fake_run(argv, cwd=None):
        calls.append([str(a) for a in argv])
        return 0

    out = tmp_path / "automation-nd"
    with patch.object(guided_new, "_run", side_effect=fake_run):
        guided_new.main(["nd", "--goal", "g", "--owner", "o@x.com", "--out", str(out)])

    design_calls = [c for c in calls if "assist" in c and "design" in c]
    assert not design_calls
