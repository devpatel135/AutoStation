from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"


def run(*args, cwd=None):
    return subprocess.run([sys.executable, *map(str, args)], cwd=cwd, capture_output=True, text=True)


def test_scaffolder_generates_a_project_whose_own_tests_pass(tmp_path):
    out = tmp_path / "automation-demo-thing"
    r = run(TOOLS / "new_automation.py", "automation-demo-thing", "--description", "Demo thing", "--owner", "team@example.com", "--out", out)
    assert r.returncode == 0, r.stdout + r.stderr
    for f in ("main.py", "automation.yaml", ".gitlab-ci.yml", "manifest-entry.yaml", "README.md", "tests/test_main.py"):
        assert (out / f).exists(), f
    assert "automation-demo-thing" in (out / "automation.yaml").read_text()


def test_scaffolder_rejects_bad_names_and_non_empty_dirs(tmp_path):
    assert run(TOOLS / "new_automation.py", "BadName", "--description", "x", "--owner", "a@b.c", "--out", tmp_path / "x").returncode != 0
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "f").write_text("x")
    assert run(TOOLS / "new_automation.py", "automation-ok", "--description", "x", "--owner", "a@b.c", "--out", tmp_path / "full").returncode != 0


def test_compat_smoke_passes_on_the_shipped_checkout():
    r = run(TOOLS / "compat_smoke.py", "--allow-placeholders")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "compatibility OK" in r.stdout
