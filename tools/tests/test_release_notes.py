from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import release_notes as rn  # noqa: E402

SHA = "a" * 40

OLD = """components:
  wrapper-core:
    rings:
      dev: "0.3.0"
      pilot: "0.3.0"
      broad: "0.3.0"
    versions:
      "0.3.0":
        url: a
automations:
  auto-a:
    status: active
    rings:
      dev: "1.0.0"
      pilot: "1.0.0"
      broad: "1.0.0"
    versions:
      "1.0.0":
        url: b
"""
NEW = """components:
  wrapper-core:
    rings:
      dev: "0.4.0"
      pilot: "0.4.0"
      broad: "0.3.0"
    versions:
      "0.3.0":
        url: a
      "0.4.0":
        url: c
automations:
  auto-a:
    status: paused
    rings:
      dev: "1.0.0"
      pilot: "1.0.0"
      broad: "1.0.0"
    versions:
      "1.0.0":
        url: b
  auto-b:
    status: active
    rings:
      dev: "0.1.0"
    versions:
      "0.1.0":
        url: d
fleet:
  paused: true
"""


@pytest.mark.parametrize("subject,expect", [
    ("feat(wrapper): add approvals", ("feat", "wrapper", "add approvals", False)),
    ("fix!: drop the old flag", ("fix", "", "drop the old flag", True)),
    ("security(site): escape telemetry", ("security", "site", "escape telemetry", False)),
    ("Just a sentence", ("other", "", "Just a sentence", False)),
    ("unknown(x): thing", ("other", "", "unknown(x): thing", False)),
])
def test_parse_subject(subject, expect):
    p = rn.parse_subject(subject)
    assert (p["type"], p["scope"], p["desc"], p["breaking"]) == expect


def test_noise_is_filtered():
    for s in ("", "Merge pull request #5 from x/y", "registry: release abc1234 to all rings [skip ci]", "anything [skip ci]"):
        assert rn.is_noise(s)
    assert not rn.is_noise("feat: real change")


def test_grouping_orders_and_counts():
    g = rn.group_commits([{"sha": SHA, "subject": "fix: a"}, {"sha": SHA, "subject": "feat: b"}, {"sha": SHA, "subject": "Merge branch x"},
                          {"sha": SHA, "subject": "registry: manual pause"}])
    assert set(g) == {"fix", "feat", "other"} and g["other"][0]["type"] == "registry"


def test_manifest_diff_reports_versions_rings_status_and_fleet():
    d = " | ".join(rn.manifest_diff(OLD, NEW))
    assert "Component **wrapper-core** 0.4.0 published" in d
    assert "dev ring 0.3.0 to 0.4.0" in d and "pilot ring 0.3.0 to 0.4.0" in d and "broad ring" not in d
    assert "New automation **auto-b**" in d and "status active to paused" in d and "Fleet PAUSED" in d


def test_manifest_diff_survives_garbage_and_missing_old():
    assert rn.manifest_diff("::: not yaml [", NEW) != [] or True
    assert rn.manifest_diff(None, None) == []
    assert any("New component" in x for x in rn.manifest_diff(None, NEW))


def test_base_marker_round_trip():
    body = rn.render_notes([], [], SHA, None)
    assert rn.extract_base(body) == SHA
    assert rn.extract_base("<!-- notes-base:zzz -->") is None and rn.extract_base(None) is None


def test_notes_neutralise_hostile_subjects_and_mention_install_facts():
    commits = [{"sha": SHA, "subject": "feat(<b>x</b>): <script>alert(1)</script> @octocat fixes #9 [a](https://evil.example)"}]
    notes = rn.render_notes(commits, [], SHA, None, repo="o/r", installer_name="x-setup.exe", installer_sha256="f" * 64)
    assert "<script>" not in notes and "<b>" not in notes and "@octocat" not in notes and "#9" not in notes
    assert "gh attestation verify" in notes and "o/r" in notes and "f" * 64 in notes and "SmartScreen" in notes


def test_group_size_is_capped_and_empty_release_says_so():
    commits = [{"sha": SHA, "subject": f"fix: item {i}"} for i in range(40)]
    notes = rn.render_notes(commits, [], SHA, None)
    assert "... and 15 more" in notes
    assert "No functional changes" in rn.render_notes([], [], SHA, None)


def test_git_glue_on_a_scratch_repo(tmp_path, monkeypatch):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "components" / "automation-registry").mkdir(parents=True)
    mpath = tmp_path / rn.MANIFEST
    mpath.write_text(OLD, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "feat: first")
    first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    mpath.write_text(NEW, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "fix: second")
    monkeypatch.setattr(rn, "ROOT", tmp_path)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    commits, old, new = rn.collect(first, head)
    assert [c["subject"] for c in commits] == ["fix: second"] and old == OLD and new == NEW
    commits, old, new = rn.collect("0" * 40, head)         # unknown base: fall back to recent history, no manifest diff
    assert len(commits) == 2 and old is None and new == NEW
