from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "ci"))
sys.path.insert(0, str(ROOT / "components" / "wrapper-core"))

import upgrade_check as uc  # noqa: E402

MANIFEST = {
    "components": {"wrapper-core": {"rings": {"dev": "0.8.0", "pilot": "0.7.0", "broad": "0.7.0"}},
                   "execution-framework": {"rings": {"dev": "0.4.1", "pilot": "0.4.1", "broad": "0.4.1"}}},
    "automations": {"auto-a": {"rings": {"dev": "1.2.0", "pilot": "1.1.0", "broad": "1.1.0"}}},
}


def make_home(tmp_path, wc="0.7.0", fw="0.4.0", auto="1.1.0"):
    home = tmp_path / "home"
    (home / "wrapper-core" / "wrapper_core").mkdir(parents=True)
    (home / "wrapper-core" / "wrapper_core" / "__init__.py").write_text("", encoding="utf-8")
    (home / "wrapper-core" / "VERSION").write_text(wc + "\n", encoding="utf-8")
    (home / "execution-framework").mkdir()
    (home / "execution-framework" / "VERSION").write_text(fw + "\n", encoding="utf-8")
    (home / "automations" / "auto-a").mkdir(parents=True)
    (home / "automations" / "auto-a" / "VERSION").write_text(auto + "\n", encoding="utf-8")
    (home / "state").mkdir()
    (home / "logs").mkdir()
    return home


def test_expected_reads_the_ring_targets():
    assert uc.expected(MANIFEST, "auto-a", "dev") == {"wrapper-core": "0.8.0", "execution-framework": "0.4.1", "auto-a": "1.2.0"}
    assert uc.expected(MANIFEST, "auto-a", "broad")["wrapper-core"] == "0.7.0"


def test_expected_refuses_an_incomplete_manifest():
    with pytest.raises(ValueError):
        uc.expected(MANIFEST, "nope", "dev")
    with pytest.raises(ValueError):
        uc.expected({"components": {}, "automations": {}}, "auto-a", "dev")
    with pytest.raises(ValueError):
        uc.expected(MANIFEST, "auto-a", "canary")


def test_verify_passes_only_when_every_package_matches(tmp_path):
    home = make_home(tmp_path, wc="0.8.0", fw="0.4.1", auto="1.2.0")
    want = uc.expected(MANIFEST, "auto-a", "dev")
    assert uc.verify(home, "auto-a", want) == []
    (home / "automations" / "auto-a" / "VERSION").write_text("1.1.0\n", encoding="utf-8")
    problems = uc.verify(home, "auto-a", want)
    assert len(problems) == 1 and "auto-a: installed 1.1.0" in problems[0]


def test_verify_flags_a_missing_install_a_leftover_swap_and_a_rollback(tmp_path):
    home = make_home(tmp_path, wc="0.8.0", fw="0.4.1", auto="1.2.0")
    want = uc.expected(MANIFEST, "auto-a", "dev")
    shutil.rmtree(home / "execution-framework")
    (home / "wrapper-core.pending").mkdir()
    (home / "state" / "rejected-wrapper-core.txt").write_text("0.8.0\n", encoding="utf-8")
    (home / "logs" / "wrapper.log").write_text("Thu 11/11 apply-pending: the staged wrapper-core failed its self-test and was rolled back\n", encoding="utf-8")
    text = "\n".join(uc.verify(home, "auto-a", want))
    assert "execution-framework: installed nothing" in text and "wrapper-core.pending is still present" in text
    assert "was rejected" in text and "rolled back" in text


def test_break_stages_a_pending_copy_that_fails_its_selftest(tmp_path):
    home = make_home(tmp_path)
    assert uc.make_broken_pending(home) == uc.BROKEN_VERSION
    assert (home / "wrapper-core.pending" / "VERSION").read_text(encoding="utf-8").strip() == uc.BROKEN_VERSION
    r = subprocess.run([sys.executable, "-m", "wrapper_core", "selftest"], cwd=home / "wrapper-core.pending", env={"PYTHONPATH": str(home / "wrapper-core.pending")},
                       capture_output=True)
    assert r.returncode == 1
    with pytest.raises(ValueError):
        uc.make_broken_pending(tmp_path / "empty")


def test_rollback_problems_describe_what_is_wrong(tmp_path):
    home = make_home(tmp_path)
    before = {"wrapper-core": "0.7.0"}
    text = "\n".join(uc.rollback_problems(home, before))
    assert "wrapper-core.bad" in text and "rejected-wrapper-core.txt" in text       # nothing was rejected yet
    (home / "wrapper-core.bad").mkdir()
    (home / "state" / "rejected-wrapper-core.txt").write_text(uc.BROKEN_VERSION + "\n", encoding="utf-8")
    assert uc.rollback_problems(home, before) == []
    (home / "wrapper-core" / "VERSION").write_text(uc.BROKEN_VERSION + "\n", encoding="utf-8")
    assert "NOT rolled back" in "\n".join(uc.rollback_problems(home, before))


def test_cli_roundtrip(tmp_path, capsys):
    home = make_home(tmp_path, wc="0.8.0", fw="0.4.1", auto="1.2.0")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("""components:
  wrapper-core:
    rings: {dev: "0.8.0", pilot: "0.7.0", broad: "0.7.0"}
  execution-framework:
    rings: {dev: "0.4.1", pilot: "0.4.1", broad: "0.4.1"}
automations:
  auto-a:
    rings: {dev: "1.2.0", pilot: "1.1.0", broad: "1.1.0"}
""", encoding="utf-8")
    assert uc.main(["expected", "--manifest", str(manifest), "--automation", "auto-a", "--ring", "dev"]) == 0
    want = json.loads(capsys.readouterr().out)
    exp = tmp_path / "expected.json"
    exp.write_text(json.dumps(want), encoding="utf-8")
    assert uc.main(["verify", "--home", str(home), "--automation", "auto-a", "--expected", str(exp)]) == 0
    assert uc.main(["expected", "--manifest", str(manifest), "--automation", "missing"]) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="bin\\apply-pending.cmd is a Windows batch file")
def test_apply_pending_really_rejects_the_broken_copy_and_keeps_the_old_one(tmp_path):
    """The rollback assertion of the Windows job, run here for real: the launcher script written by wrapper_core's scheduler, a real Python
    interpreter, a genuinely broken wrapper-core.pending."""
    from wrapper_core.paths import Paths
    from wrapper_core.scheduler import Scheduler
    home = tmp_path / "AUTOMAGIX"
    src = ROOT / "components" / "wrapper-core"
    (home / "wrapper-core").parent.mkdir(parents=True)
    shutil.copytree(src / "wrapper_core", home / "wrapper-core" / "wrapper_core", ignore=shutil.ignore_patterns("__pycache__"))
    (home / "wrapper-core" / "VERSION").write_text("0.7.0\n", encoding="utf-8")
    for d in ("state", "logs", "bin"):
        (home / d).mkdir()
    paths = Paths(home)
    Scheduler(paths, python_exe=sys.executable).write_apply_pending()
    before = {"wrapper-core": "0.7.0"}
    assert uc.make_broken_pending(home) == uc.BROKEN_VERSION
    r = subprocess.run(["cmd.exe", "/c", str(home / "bin" / "apply-pending.cmd")], capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert uc.rollback_problems(home, before) == []
    # and the restored copy still passes its own selftest
    ok = subprocess.run([sys.executable, "-m", "wrapper_core", "selftest"], env={"PYTHONPATH": str(home / "wrapper-core"), "AUTOMAGIX_HOME": str(home),
                                                                                  "SYSTEMROOT": "C:\\Windows"}, capture_output=True, timeout=120)
    assert ok.returncode == 0, ok.stdout + ok.stderr


# ---- the PowerShell driver: static checks (it only really runs on a Windows runner) -------------------------------------------------
PS = ROOT / "tools" / "ci" / "upgrade-smoke.ps1"


def test_upgrade_smoke_script_parses_when_powershell_is_available():
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        pytest.skip("no PowerShell on this machine")
    cmd = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$null,[ref]$e); "
           "if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }" % str(PS).replace("'", "''"))
    r = subprocess.run([exe, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_upgrade_smoke_script_covers_the_required_checks():
    t = PS.read_text(encoding="utf-8")
    assert "installers-latest" in t and "sha256" in t.lower()                        # the PREVIOUS released installer, verified
    assert "AUTOMAGIX_REGISTRY_URL" in t and "AUTOMAGIX_RING" in t and "AUTOMAGIX_REGISTRY_PUBKEYS" in t
    assert "sign_registry.py" in t and "127.0.0.1" in t and "REGISTRY_SIGNING_SEED" in t   # throwaway key, local server
    assert "run-$Automation.cmd" in t and "apply-pending.cmd" in t                   # the real scheduled path
    assert "upgrade_check.py" in t and "'verify'" in t and "'rollback'" in t and "'break'" in t
    assert "selftest" in t
    assert re.search(r"Cleanup", t) and "uninstall" in t                              # always cleans up
