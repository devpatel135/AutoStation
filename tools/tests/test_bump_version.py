from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import bump_version as bv  # noqa: E402


def make(tmp: Path, wc="0.7.0", wc_py="0.7.0", fw="0.4.1", fw_pkg="0.4.1", auto="1.1.2") -> Path:
    c = tmp / "components"
    (c / "wrapper-core" / "wrapper_core").mkdir(parents=True)
    (c / "wrapper-core" / "VERSION").write_text(wc + "\n")
    (c / "wrapper-core" / "wrapper_core" / "_version.py").write_text(f'__version__ = "{wc_py}"\n')
    (c / "execution-framework").mkdir()
    (c / "execution-framework" / "VERSION").write_bytes((fw + "\r\n").encode())
    (c / "execution-framework" / "package.json").write_text('{\n  "name": "execution-framework",\n  "version": "%s",\n  "dependencies": {"playwright-core": "1.48.2"}\n}\n' % fw_pkg)
    a = c / "automations" / "automation-x"
    a.mkdir(parents=True)
    (a / "automation.yaml").write_text(f"name: automation-x\nversion: {auto}\nruntime: node\n")
    return tmp


def test_bump_edits_both_files_and_keeps_line_endings(tmp_path):
    repo = make(tmp_path)
    old, new, files = bv.bump("wrapper-core", "minor", repo=repo)
    assert (old, new) == ("0.7.0", "0.8.0") and len(files) == 2
    assert bv.read_versions("wrapper-core", repo) == {"components/wrapper-core/VERSION": "0.8.0", "components/wrapper-core/wrapper_core/_version.py": "0.8.0"}
    bv.bump("execution-framework", repo=repo)
    assert (repo / "components" / "execution-framework" / "VERSION").read_bytes() == b"0.4.2\r\n"            # CRLF preserved
    pkg = json.loads((repo / "components" / "execution-framework" / "package.json").read_text())
    assert pkg["version"] == "0.4.2" and pkg["dependencies"] == {"playwright-core": "1.48.2"}                  # nothing else touched
    assert bv.bump("automation-x", "major", repo=repo)[:2] == ("1.1.2", "2.0.0")
    assert "version: 2.0.0\nruntime: node" in (repo / "components" / "automations" / "automation-x" / "automation.yaml").read_text()


def test_bump_refuses_disagreeing_files_non_increasing_and_bad_names(tmp_path):
    repo = make(tmp_path, wc="0.7.0", wc_py="0.6.9")
    with pytest.raises(bv.BumpError, match="disagree"):
        bv.bump("wrapper-core", repo=repo)
    repo2 = make(tmp_path / "b")
    with pytest.raises(bv.BumpError, match="not greater"):
        bv.bump("wrapper-core", set_to="0.7.0", repo=repo2)
    with pytest.raises(bv.BumpError, match="invalid package name"):
        bv.bump("../evil", repo=repo2)
    assert bv.read_versions("wrapper-core", repo2)["components/wrapper-core/VERSION"] == "0.7.0"              # nothing was written


def test_dry_run_writes_nothing_and_cli_exit_codes(tmp_path, capsys):
    repo = make(tmp_path)
    assert bv.main(["wrapper-core", "--repo", str(repo), "--dry-run"]) == 0
    assert "0.7.0 -> 0.7.1 (dry run)" in capsys.readouterr().out
    assert (repo / "components" / "wrapper-core" / "VERSION").read_text().strip() == "0.7.0"
    assert bv.main(["--check", "--repo", str(repo)]) == 0
    (repo / "components" / "execution-framework" / "VERSION").write_text("0.9.9\n")
    assert bv.main(["--check", "--repo", str(repo)]) == 1
    assert "execution-framework: version files disagree" in capsys.readouterr().err
    assert bv.main(["nope-not-there", "--repo", str(repo)]) == 1


def test_the_real_repo_version_files_agree():
    """VERSION == wrapper_core/_version.py == package.json for every package: a bump that edits only one file fails CI."""
    assert bv.check_all(ROOT) == []
    wc = bv.read_versions("wrapper-core", ROOT)
    assert len(set(wc.values())) == 1
    fw = bv.read_versions("execution-framework", ROOT)
    assert len(set(fw.values())) == 1
