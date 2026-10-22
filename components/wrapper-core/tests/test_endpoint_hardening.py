"""vpn_tool allow-list and the AMX_CRED_* development fallback (endpoint-side hardening)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from wrapper_core import runner as runner_mod
from wrapper_core.activities import credentials
from wrapper_core.retry import RetryPolicy, _run_vpn_tool, vpn_tool_command


def touch(p: Path, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --- vpn_tool ---------------------------------------------------------------------------------------------------------
def test_a_script_inside_the_automation_folder_is_allowed(tmp_path):
    s = touch(tmp_path / "vpn" / "connect.ps1")
    cmd = vpn_tool_command("vpn/connect.ps1", str(tmp_path))
    assert cmd is not None and cmd[0] == "powershell.exe" and cmd[-1] == str(s.resolve())
    touch(tmp_path / "up.cmd")
    assert vpn_tool_command("up.cmd", str(tmp_path)) == [str((tmp_path / "up.cmd").resolve())]


@pytest.mark.parametrize("value", [
    None, "", "   ", 5, "..\\..\\evil.exe", "../evil.exe", "sub/../../evil.exe", "\\\\evil\\share\\x.exe", "//evil/share/x.exe",
    "C:\\Windows\\System32\\cmd.exe", "C:\\evil.exe", "calc.exe & whoami", "a;b.ps1", "x`y.ps1", "%COMSPEC%", "$env:x.ps1",
    'quote".exe', "a|b.exe", "x" * 300, "notes.txt", "sub/notes.txt",
])
def test_everything_else_is_refused(tmp_path, value):
    touch(tmp_path / "notes.txt")
    touch(tmp_path / "sub" / "notes.txt")
    assert vpn_tool_command(value, str(tmp_path)) is None


def test_symlink_or_junction_escape_is_refused(tmp_path):
    outside = touch(tmp_path / "outside" / "evil.ps1")
    inner = tmp_path / "automation"
    inner.mkdir()
    try:
        os.symlink(outside.parent, inner / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    assert vpn_tool_command("link/evil.ps1", str(inner)) is None


def test_absolute_path_only_when_listed_verbatim(tmp_path):
    tool = touch(tmp_path / "tools" / "vpn.exe")
    assert vpn_tool_command(str(tool), str(tmp_path / "automation")) is None
    assert vpn_tool_command(str(tool), str(tmp_path / "automation"), allowed=[str(tool)]) == [str(tool)]
    assert vpn_tool_command(str(tool), str(tmp_path), allowed=[str(tmp_path / "tools" / "other.exe")]) is None
    assert vpn_tool_command(str(tool), str(tmp_path), allowed=[123, None]) is None


def test_bare_exe_resolves_only_in_system32(tmp_path):
    sysdir = tmp_path / "System32"
    touch(sysdir / "rasdial.exe")
    assert vpn_tool_command("rasdial.exe", str(tmp_path), system_dir=str(sysdir)) == [str(sysdir / "rasdial.exe")]
    assert vpn_tool_command("missing.exe", str(tmp_path), system_dir=str(sysdir)) is None
    touch(tmp_path / "planted.exe")                      # an exe in the working folder is NOT found by bare name
    assert vpn_tool_command("planted.exe", str(tmp_path), system_dir=str(sysdir)) is None


def test_run_vpn_tool_refuses_without_running_anything(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0))
    assert _run_vpn_tool(RetryPolicy(vpn_tool="C:\\evil\\x.exe", cwd=str(tmp_path))) is False
    assert calls == []
    touch(tmp_path / "ok.cmd")
    assert _run_vpn_tool(RetryPolicy(vpn_tool="ok.cmd", cwd=str(tmp_path))) is True
    assert len(calls) == 1 and calls[0][0] == [str((tmp_path / "ok.cmd").resolve())]


def test_platform_json_vpn_tools_extends_the_allow_list(tmp_path, monkeypatch):
    import json
    from wrapper_core.paths import Paths
    monkeypatch.setenv("AUTOMAGIX_HOME", str(tmp_path / "home"))
    p = Paths().ensure()
    tool = touch(tmp_path / "corp" / "vpn.exe")
    (p.config / "platform.json").write_text(json.dumps({"ring": "broad", "vpn_tools": [str(tool)]}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a[0]) or subprocess.CompletedProcess(a, 0))
    assert _run_vpn_tool(RetryPolicy(vpn_tool=str(tool), cwd=str(tmp_path))) is True
    assert calls == [[str(tool)]]


# --- AMX_CRED_* -------------------------------------------------------------------------------------------------------
def test_amx_cred_is_stripped_from_payload_environments():
    env = runner_mod.scrub_env({"AMX_CRED_SAP": "user:pw", "amx_cred_lower": "x", "PATH": "p", "AUTOMAGIX_RING": "pilot"})
    assert "AMX_CRED_SAP" not in env and "amx_cred_lower" not in env
    assert env["PATH"] == "p" and env["AUTOMAGIX_RING"] == "pilot"


def test_amx_cred_fallback_only_honoured_in_insecure_dev_mode(monkeypatch):
    monkeypatch.setenv("AMX_CRED_MY_APP", "alice:s3cret")
    monkeypatch.setenv("AUTOMAGIX_ALLOW_INSECURE_SECRETS", "1")
    assert credentials.get_credential("my-app").username == "alice"
    monkeypatch.delenv("AUTOMAGIX_ALLOW_INSECURE_SECRETS")       # production: the variable is ignored
    with pytest.raises(credentials.CredentialNotFound):
        credentials.get_credential("my-app")
    monkeypatch.setenv("AUTOMAGIX_ALLOW_INSECURE_SECRETS", "0")
    with pytest.raises(credentials.CredentialNotFound):
        credentials.get_raw("my-app")
