"""Runs the installer PowerShell scripts for real (Windows only). Nothing here touches the real Credential Manager,
registry, certificate stores or Startup folder: all paths are temp dirs and set-credential runs in dry-run mode."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="PowerShell 5.1 / DPAPI tests need Windows")

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "installer" / "scripts"
WRAPPER = ROOT.parent / "wrapper-core"


def ps(script: str, *args: str, env: dict | None = None, stdin: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / script), *args]
    return subprocess.run(cmd, capture_output=True, text=True, input=stdin, env={**os.environ, **(env or {})}, timeout=120)


def py(code: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(WRAPPER)}, timeout=60)


SECRET = bytes(range(256)) + b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"


def test_dpapi_ps_protect_then_python_unprotect(tmp_path):
    src, blob = tmp_path / "key", tmp_path / "key.dpapi"
    src.write_bytes(SECRET)
    r = ps("protect-secret.ps1", "-InFile", str(src), "-OutFile", str(blob), "-DeleteInput")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not src.exists(), "plaintext must be wiped after protect"
    assert blob.read_bytes() != SECRET and SECRET[:32] not in blob.read_bytes()
    out = tmp_path / "plain.bin"
    r = py("import sys; from wrapper_core import secrets; from pathlib import Path; "
           "Path(sys.argv[2]).write_bytes(secrets.dpapi_unprotect(Path(sys.argv[1]).read_bytes()))", str(blob), str(out))
    assert r.returncode == 0, r.stderr
    assert out.read_bytes() == SECRET


def test_dpapi_python_protect_then_ps_unprotect(tmp_path):
    src, blob, out = tmp_path / "in.bin", tmp_path / "in.dpapi", tmp_path / "out.bin"
    src.write_bytes(SECRET)
    r = py("import sys; from wrapper_core import secrets; from pathlib import Path; "
           "Path(sys.argv[2]).write_bytes(secrets.dpapi_protect(Path(sys.argv[1]).read_bytes()))", str(src), str(blob))
    assert r.returncode == 0, r.stderr
    r = ps("protect-secret.ps1", "-InFile", str(blob), "-OutFile", str(out), "-Unprotect")
    assert r.returncode == 0, r.stderr + r.stdout
    assert out.read_bytes() == SECRET


def test_wrapper_core_cli_protect_matches_ps_unprotect(tmp_path):
    src, blob, out = tmp_path / "k", tmp_path / "k.dpapi", tmp_path / "k.out"
    src.write_bytes(b"cli-secret\n")
    r = subprocess.run([sys.executable, "-m", "wrapper_core", "secrets", "protect", "--in", str(src), "--out", str(blob), "--delete-input"],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(WRAPPER)})
    assert r.returncode == 0, r.stderr
    assert not src.exists()
    assert ps("protect-secret.ps1", "-InFile", str(blob), "-OutFile", str(out), "-Unprotect").returncode == 0
    assert out.read_bytes() == b"cli-secret\n"


def test_protect_rejects_missing_and_empty_input(tmp_path):
    assert ps("protect-secret.ps1", "-InFile", str(tmp_path / "nope"), "-OutFile", str(tmp_path / "o")).returncode != 0
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert ps("protect-secret.ps1", "-InFile", str(empty), "-OutFile", str(tmp_path / "o")).returncode != 0


def test_protected_blob_acl_is_owner_only(tmp_path):
    f = tmp_path / "blob"
    f.write_bytes(b"x")
    r = ps("restrict-acl.ps1", "-Path", str(f))
    assert r.returncode == 0, r.stderr + r.stdout
    out = subprocess.run(["icacls.exe", str(f)], capture_output=True, text=True).stdout
    me = os.environ["USERNAME"].lower()
    aces = [l for l in out.splitlines() if ":(" in l]
    assert aces and all(me in l.lower() for l in aces), out
    assert "(I)" not in out  # no inherited entries
    d = tmp_path / "dir"
    d.mkdir()
    assert ps("restrict-acl.ps1", "-Path", str(d)).returncode == 0


def test_set_credential_dry_run_never_echoes_secret(tmp_path):
    token = "glpat-NEVER-PRINT-ME-123456"
    f = tmp_path / "tok"
    f.write_text(token)
    r = ps("set-credential.ps1", "-Target", "AUTOMAGIX/registry_token", "-SecretFile", str(f), "-DryRun")
    assert r.returncode == 0, r.stderr
    assert token not in r.stdout + r.stderr and "dry-run" in r.stdout
    assert not f.exists(), "secret file must be wiped and removed"
    r = ps("set-credential.ps1", "-Target", "AUTOMAGIX/registry_token", "-FromStdin", "-DryRun", stdin=token)
    assert r.returncode == 0 and token not in r.stdout + r.stderr
    r = ps("set-credential.ps1", "-Target", "AUTOMAGIX/registry_token", env={"AMX_SECRET": token, "AUTOMAGIX_CRED_DRYRUN": "1"})
    assert r.returncode == 0 and token not in r.stdout + r.stderr


def test_set_credential_validates_inputs():
    assert ps("set-credential.ps1", "-Target", "bad target!", "-DryRun", env={"AMX_SECRET": "x"}).returncode != 0
    assert ps("set-credential.ps1", "-Target", "AUTOMAGIX/registry_token", "-DryRun").returncode != 0  # no secret supplied


def test_install_check_reports_json_and_enforces_disk_space(tmp_path):
    r = ps("install-check.ps1", "-InstallDir", str(tmp_path / "amx"), "-SkipGit", "-MinFreeMB", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(r.stdout.strip().splitlines()[-1])
    assert data["ok"] is True
    assert {c["name"] for c in data["checks"]} >= {"windows", "install_dir_writable", "disk_space"}
    r = ps("install-check.ps1", "-InstallDir", str(tmp_path / "amx"), "-SkipGit", "-MinFreeMB", "999999999")
    assert r.returncode == 1
    bad = json.loads(r.stdout.strip().splitlines()[-1])
    assert bad["ok"] is False and any(c["name"] == "disk_space" and not c["ok"] for c in bad["checks"])


def test_install_check_git_and_ssh_detection(tmp_path):
    r = ps("install-check.ps1", "-InstallDir", str(tmp_path / "amx"), "-MinFreeMB", "1")
    data = json.loads(r.stdout.strip().splitlines()[-1])
    names = {c["name"]: c["ok"] for c in data["checks"]}
    assert "git" in names and "ssh" in names
    assert (r.returncode == 0) == data["ok"]
    # with an empty PATH git cannot be found -> hard failure
    r2 = ps("install-check.ps1", "-InstallDir", str(tmp_path / "amx2"), "-MinFreeMB", "1", env={"PATH": str(Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0")})
    assert r2.returncode == 1 and json.loads(r2.stdout.strip().splitlines()[-1])["git"] is None


def test_configure_python_rewrites_pth(tmp_path):
    py_dir = tmp_path / "python"
    py_dir.mkdir()
    (py_dir / "python312._pth").write_text("python312.zip\n.\n#import site\n")
    r = ps("configure-python.ps1", "-PythonDir", str(py_dir))
    assert r.returncode == 0, r.stderr
    lines = (py_dir / "python312._pth").read_text().splitlines()
    assert lines[:3] == ["python312.zip", ".", "..\\wrapper-core"] and "import site" in lines
    plain = tmp_path / "plain"
    plain.mkdir()
    assert ps("configure-python.ps1", "-PythonDir", str(plain)).returncode == 0  # no ._pth: no-op
    assert ps("configure-python.ps1", "-PythonDir", str(tmp_path / "missing")).returncode != 0


def test_embedded_python_layout_can_import_wrapper_core(tmp_path):
    """Simulate the installed layout: a ._pth next to a real interpreter copy must resolve ..\\wrapper-core."""
    home = tmp_path / "AUTOMAGIX"
    shutil.copytree(WRAPPER / "wrapper_core", home / "wrapper-core" / "wrapper_core")
    # we cannot ship a real embeddable python here; verify the path arithmetic the ._pth relies on instead
    pth_dir = home / "python"
    pth_dir.mkdir()
    assert ps("configure-python.ps1", "-PythonDir", str(pth_dir)).returncode == 0  # no ._pth -> no-op
    (pth_dir / "python312._pth").write_text("x")
    assert ps("configure-python.ps1", "-PythonDir", str(pth_dir)).returncode == 0
    entry = [l for l in (pth_dir / "python312._pth").read_text().splitlines() if "wrapper-core" in l][0]
    assert (pth_dir / entry).resolve() == (home / "wrapper-core").resolve()


def test_write_platform_config_is_consumable_by_wrapper_core(tmp_path, monkeypatch):
    out = tmp_path / "config" / "platform.json"
    r = ps("write-platform-config.ps1", "-Out", str(out), "-Ring", "pilot", "-RegistryUrl", "https://reg.example.internal/",
           "-LogRemote", "git@gitlab.example.internal:platform/automation-logging.git",
           "-LlmEndpoint", "https://llm.example.internal/v1/chat/completions", "-LlmModel", "m1")
    assert r.returncode == 0, r.stderr + r.stdout
    cfg = json.loads(out.read_text(encoding="utf-8"))
    assert cfg["ring"] == "pilot" and cfg["log_transport"] == "git-ssh" and cfg["llm_model"] == "m1"
    # the real consumer
    sys.path.insert(0, str(WRAPPER))
    from wrapper_core.paths import Paths, platform_config
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    shutil.copyfile(out, home / "config" / "platform.json")
    for k in list(os.environ):
        if k.startswith("AUTOMAGIX_") or k.startswith("AUTOSTATION_"):
            monkeypatch.delenv(k)
    merged = platform_config(Paths(home))
    assert merged["registry_url"] == "https://reg.example.internal/" and merged["log_transport"] == "git-ssh"
    # re-install: keeps operator-added keys, updates managed ones, handles dir remotes and empty optionals
    cfg["operator_note"] = "keep me"
    out.write_text(json.dumps(cfg))
    assert ps("write-platform-config.ps1", "-Out", str(out), "-Ring", "dev", "-RegistryUrl", r"C:\registry", "-LogRemote", r"\\share\logs").returncode == 0
    cfg2 = json.loads(out.read_text(encoding="utf-8"))
    assert cfg2["operator_note"] == "keep me" and cfg2["ring"] == "dev" and cfg2["log_transport"] == "dir"
    assert ps("write-platform-config.ps1", "-Out", str(out), "-Ring", "prod", "-RegistryUrl", "x").returncode != 0


def test_first_run_rejects_bad_input(tmp_path):
    assert ps("first-run.ps1", "-Python", str(tmp_path / "nope.exe"), "-AutomagixHome", str(tmp_path), "-Automation", "a").returncode != 0
    assert ps("first-run.ps1", "-Python", sys.executable, "-AutomagixHome", str(tmp_path), "-Automation", "bad name; calc").returncode != 0


def _make_pfx(tmp_path: Path, password: str) -> Path:
    pfx = tmp_path / "test.pfx"
    script = f"""
$ErrorActionPreference='Stop'
$rsa=[System.Security.Cryptography.RSA]::Create(2048)
$req=New-Object System.Security.Cryptography.X509Certificates.CertificateRequest('CN=amx-test-signer',$rsa,[System.Security.Cryptography.HashAlgorithmName]::SHA256,[System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
$oids=New-Object System.Security.Cryptography.OidCollection
[void]$oids.Add((New-Object System.Security.Cryptography.Oid('1.3.6.1.5.5.7.3.3')))
$req.CertificateExtensions.Add((New-Object System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension($oids,$false)))
$cert=$req.CreateSelfSigned([DateTimeOffset]::UtcNow.AddMinutes(-5),[DateTimeOffset]::UtcNow.AddDays(1))
[System.IO.File]::WriteAllBytes('{pfx}',$cert.Export('Pfx','{password}'))
"""
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not pfx.exists():
        pytest.skip(f"cannot create a test certificate in-memory on this host: {r.stderr[:200]}")
    return pfx


def test_sign_ps1_validates_inputs(tmp_path):
    exe = tmp_path / "a.exe"
    exe.write_bytes(b"MZ")
    assert ps("sign.ps1", "-File", str(exe)).returncode != 0                                    # no cert source
    assert ps("sign.ps1", "-File", str(exe), "-Thumbprint", "AA", "-PfxFile", "x").returncode != 0  # both sources
    assert ps("sign.ps1", "-File", str(exe), "-Thumbprint", "0" * 40, "-WhatIf").returncode != 0     # unknown thumbprint
    assert ps("sign.ps1", "-File", str(tmp_path / "missing.exe"), "-PfxFile", "x").returncode != 0


def test_sign_ps1_signs_a_binary_with_a_pfx(tmp_path):
    password = "t3st-Pfx-pass"
    pfx = _make_pfx(tmp_path, password)
    exe = tmp_path / "copy.exe"
    comp = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                           f"Add-Type -TypeDefinition 'public class P {{ public static void Main() {{}} }}' -OutputAssembly '{exe}' -OutputType ConsoleApplication"],
                          capture_output=True, text=True, timeout=120)
    if comp.returncode != 0 or not exe.exists():
        pytest.skip(f"cannot compile an unsigned test exe: {comp.stderr[:200]}")
    wi = ps("sign.ps1", "-File", str(exe), "-PfxFile", str(pfx), "-PfxPasswordEnv", "AMX_T", "-WhatIf", env={"AMX_T": password})
    assert wi.returncode == 0, wi.stderr + wi.stdout
    assert ps("sign.ps1", "-File", str(exe), "-PfxFile", str(pfx), "-PfxPasswordEnv", "AMX_T", env={"AMX_T": "wrong"}).returncode != 0
    r = ps("sign.ps1", "-File", str(exe), "-PfxFile", str(pfx), "-PfxPasswordEnv", "AMX_T", env={"AMX_T": password})
    assert r.returncode == 0, r.stderr + r.stdout
    assert password not in r.stdout + r.stderr
    sig = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                          f"(Get-AuthenticodeSignature -LiteralPath '{exe}').SignerCertificate.Subject"], capture_output=True, text=True)
    assert "amx-test-signer" in sig.stdout


def test_nsis_style_command_line_with_empty_arguments(tmp_path):
    """nsExec hands powershell one command-line string with quoted (possibly empty) values - reproduce exactly that."""
    out = tmp_path / "config" / "platform.json"
    cmd = (f'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{SCRIPTS / "write-platform-config.ps1"}" '
           f'-Out "{out}" -Ring "pilot" -RegistryUrl "https://registry.example.internal/" -LogRemote "git@h:p.git" -LlmEndpoint "" -LlmModel ""')
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    cfg = json.loads(out.read_text(encoding="utf-8"))
    assert cfg["ring"] == "pilot" and "llm_endpoint" not in cfg and cfg["log_transport"] == "git-ssh"
