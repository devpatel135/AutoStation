from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import build_installer as bi
import validate_nsi as vn
from conftest import PRIVATE_KEY, TOKEN, make_wrapper_zip

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _fake_makensis(monkeypatch):
    monkeypatch.setattr(bi, "find_makensis", lambda explicit: "fake-makensis")


def render_sample(build_dir, tmp_path, **flags):
    d, cfg_dict, env = build_dir
    cfg = bi.load_config(cfg_dict, base_dir=d)
    stage = tmp_path / "stage"
    f, wv = bi.stage_build(cfg, stage, env, allow_missing_secrets=False)
    nsi = stage / "installer.nsi"
    nsi.write_text(bi.render_nsi(cfg, wv, f, str(tmp_path / "out.exe"), ".", icon="icon.ico" if f["HAVE_ICON"] else ""), encoding="utf-8")
    return cfg, stage, nsi, f


def test_rendered_installer_validates_and_has_expected_content(build_dir, tmp_path):
    cfg, stage, nsi, flags = render_sample(build_dir, tmp_path)
    assert vn.validate(nsi) == []
    text = nsi.read_text(encoding="utf-8")
    assert flags == {"HAVE_ICON": True, "HAVE_PYTHON_EMBED": True, "HAVE_SSH_KEY": True, "HAVE_TOKEN": True, "HAVE_KNOWN_HOSTS": True}
    for needle in ['RequestExecutionLevel user', '!define AUTOMATION      "automation-example-hello"', '!define VERSION         "1.2.3"',
                   'VIProductVersion "1.2.3.0"', '!define DEFAULT_RING    "pilot"', 'wrapper_core install ${AUTOMATION}',
                   'wrapper_core uninstall ${AUTOMATION}', 'AUTOMAGIX/registry_token', '/RING=', '/NORUN',
                   'HKCU "${UNINST_KEY}"', 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\AUTOMAGIX-${AUTOMATION}',
                   '-DeleteInput', 'logging_key.dpapi']:
        assert needle in text, needle
    assert not re.search(r"RequestExecutionLevel\s+(admin|highest)", text)
    assert "HKLM" not in text  # per-user only


def test_secrets_never_appear_in_rendered_script(build_dir, tmp_path):
    _, stage, nsi, _ = render_sample(build_dir, tmp_path)
    text = nsi.read_text(encoding="utf-8")
    assert TOKEN not in text and "SECRETKEYMATERIAL" not in text and "BEGIN OPENSSH" not in text
    # they exist only as staged files that makensis embeds
    assert (stage / "secrets" / "logging_key").read_text().startswith("-----BEGIN")
    assert (stage / "secrets" / "registry_token").read_text() == TOKEN
    forbid = tmp_path / "forbid.txt"
    forbid.write_text(TOKEN)
    assert vn.main([str(nsi), "--forbid-file", str(forbid)]) == 0
    nsi.write_text(text + f'\n; leaked {TOKEN}\n')
    assert vn.main([str(nsi), "--forbid-file", str(forbid)]) == 1


def test_optional_blocks_follow_flags(build_dir, tmp_path):
    d, cfg_dict, env = build_dir
    for k in ("python_embed_zip", "icon", "known_hosts_file"):
        cfg_dict.pop(k)
    cfg = bi.load_config(cfg_dict, base_dir=d)
    stage = tmp_path / "s"
    flags, wv = bi.stage_build(cfg, stage, {}, allow_missing_secrets=True)
    assert flags == {k: False for k in flags}
    nsi = stage / "i.nsi"
    nsi.write_text(bi.render_nsi(cfg, wv, flags, str(tmp_path / "o.exe")), encoding="utf-8")
    assert vn.validate(nsi) == []   # inactive !ifdef branches reference files that were never staged


def test_escaping_prevents_nsis_injection():
    assert bi.nsis_escape('a"b$c\nd') == 'a$\\"b$$c$\\nd'
    for bad in ['evil"; ExecWait "calc"', "x`y", "a\nb"]:
        with pytest.raises(bi.BuildError):
            bi.load_config({"automation": "a", "version": "1.0.0", "registry_url": "https://r/", "log_remote": "git@h:p.git", "publisher": bad})


@pytest.mark.parametrize("patch,msg", [
    ({"automation": "bad name!"}, "automation"),
    ({"version": "banana"}, "invalid version"),
    ({"ring": "prod"}, "ring"),
    ({"log_remote": "https://not-ssh/repo.git"}, "ssh"),
    ({"llm_endpoint": "ftp://x"}, "llm_endpoint"),
    ({"surprise": 1}, "unknown keys"),
    ({"registry_url": ""}, "registry_url"),
])
def test_config_validation(build_dir, patch, msg):
    d, cfg, _ = build_dir
    with pytest.raises(bi.BuildError, match=msg):
        bi.load_config({**cfg, **patch}, base_dir=d)


def test_dev_ring_may_log_to_a_share(build_dir):
    d, cfg, _ = build_dir
    assert bi.load_config({**cfg, "ring": "dev", "log_remote": r"\\fileserver\amx-logs"}, base_dir=d).ring == "dev"
    with pytest.raises(bi.BuildError):
        bi.load_config({**cfg, "ring": "broad", "log_remote": r"\\fileserver\amx-logs"}, base_dir=d)


def test_trailing_backslash_stripped_from_registry_path(build_dir):
    d, cfg, _ = build_dir
    assert bi.load_config({**cfg, "registry_url": "\\\\share\\registry\\"}, base_dir=d).registry_url == "\\\\share\\registry"


def test_missing_secrets_fail_closed(build_dir, tmp_path):
    d, cfg_dict, env = build_dir
    cfg = bi.load_config(cfg_dict, base_dir=d)
    with pytest.raises(bi.BuildError, match="required secrets"):
        bi.stage_build(cfg, tmp_path / "s", {}, allow_missing_secrets=False)
    bad_key = d / "notakey"
    bad_key.write_text("hello")
    with pytest.raises(bi.BuildError, match="private key"):
        bi.stage_build(cfg, tmp_path / "s2", {**env, "LOGGING_SSH_KEY_FILE": str(bad_key)}, allow_missing_secrets=False)


def test_bad_bootstrap_inputs(build_dir, tmp_path):
    d, cfg_dict, env = build_dir
    import zipfile
    with zipfile.ZipFile(d / "evil.zip", "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(bi.BuildError, match="unsafe path"):
        bi.stage_build(bi.load_config({**cfg_dict, "wrapper_core_zip": "evil.zip"}, base_dir=d), tmp_path / "a", env, False)
    with zipfile.ZipFile(d / "nover.zip", "w") as z:
        z.writestr("wrapper_core/__init__.py", "")
    with pytest.raises(bi.BuildError, match="VERSION"):
        bi.stage_build(bi.load_config({**cfg_dict, "wrapper_core_zip": "nover.zip"}, base_dir=d), tmp_path / "b", env, False)


# ---- the validator catches real defects ----------------------------------------------------------
def _good(tmp_path) -> str:
    return (ROOT / "installer" / "autostation.nsi.in").read_text(encoding="utf-8")


def mutated(build_dir, tmp_path, fn) -> list[str]:
    cfg, stage, nsi, _ = render_sample(build_dir, tmp_path)
    nsi.write_text(fn(nsi.read_text(encoding="utf-8")), encoding="utf-8")
    return vn.validate(nsi)


@pytest.mark.parametrize("fn,expect", [
    (lambda t: t.replace("SectionEnd\n", "", 1), "unclosed section|Section opened inside"),
    (lambda t: t + "\nFunctionEnd\n", "FunctionEnd without Function"),
    (lambda t: t.replace("!endif", "", 1), "unterminated|unclosed|without"),
    (lambda t: t.replace("${EndIf}", "", 1), "unclosed LogicLib"),
    (lambda t: t.replace("RequestExecutionLevel user", "RequestExecutionLevel admin"), "per-user|missing 'RequestExecutionLevel user'"),
    (lambda t: t.replace('!define AUTOMATION      "automation-example-hello"', '!define AUTOMATION "@@OOPS@@"'), "unresolved token"),
    (lambda t: t.replace('File "./scripts/protect-secret.ps1"', 'File "./scripts/nope.ps1"').replace('File ".\\scripts\\protect-secret.ps1"', 'File ".\\scripts\\nope.ps1"'), "does not exist"),
    (lambda t: t.replace("wrapper_core uninstall", "something else"), "uninstall"),
    (lambda t: t + "\n; -----BEGIN OPENSSH PRIVATE KEY-----\n", "secret-looking"),
])
def test_validator_detects_defects(build_dir, tmp_path, fn, expect):
    errs = mutated(build_dir, tmp_path, fn)
    assert errs and any(re.search(expect, e, re.I) for e in errs), errs


def test_every_referenced_installer_script_exists_and_is_staged(build_dir, tmp_path):
    _, stage, nsi, _ = render_sample(build_dir, tmp_path)
    for name in re.findall(r'([\w-]+\.ps1)', nsi.read_text(encoding="utf-8")):
        assert (ROOT / "installer" / "scripts" / name).exists(), name
    for name in bi.STAGE_SCRIPTS:
        assert (stage / "scripts" / name).exists()


# ---- full pipeline with a fake makensis / signer -------------------------------------------------------
class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        exe = Path(argv[0]).name.lower()
        if exe.startswith("makensis") or argv[0] == "fake-makensis":
            nsi = Path(argv[-1]).read_text(encoding="utf-8")
            out = re.search(r'OutFile "([^"]+)"', nsi).group(1)
            Path(out).write_bytes(b"MZ-fake-installer")
        elif argv[0] == "osslsigncode":
            Path(argv[argv.index("-out") + 1]).write_bytes(b"MZ-fake-installer-SIGNED")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def full_build(build_dir, tmp_path, env_extra=None, **kw):
    d, cfg_dict, env = build_dir
    cfg = bi.load_config(cfg_dict, base_dir=d)
    runner = FakeRunner()
    res = bi.run_build(cfg, tmp_path / "dist", {**env, **(env_extra or {})}, makensis="fake-makensis", runner=runner, **kw)
    return res, runner


def test_full_build_outputs_exe_and_sha256_and_wipes_stage(build_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(bi, "find_makensis", lambda explicit: "fake-makensis")
    stages_before = set(Path(__import__("tempfile").gettempdir()).glob("amx-stage-*"))
    res, runner = full_build(build_dir, tmp_path)
    exe = tmp_path / "dist" / "installers" / "automation-example-hello-1.2.3-setup.exe"
    assert res["built"] and exe.exists() and res["url"] == "installers/automation-example-hello-1.2.3-setup.exe"
    assert (exe.parent / (exe.name + ".sha256")).read_text().split()[0] == res["sha256"]
    assert not res["signed"]
    assert set(Path(__import__("tempfile").gettempdir()).glob("amx-stage-*")) == stages_before  # secrets staging removed
    assert TOKEN not in json.dumps(res)


def test_render_only_keeps_validated_nsi_without_secrets(build_dir, tmp_path):
    res, _ = full_build(build_dir, tmp_path, render_only=True)
    nsi = Path(res["nsi"]).read_text(encoding="utf-8")
    assert not res["built"] and "SECRETKEY" not in nsi and TOKEN not in nsi


def test_require_signing_fails_closed(build_dir, tmp_path):
    with pytest.raises(bi.BuildError, match="signing is required"):
        full_build(build_dir, tmp_path, require_signing=True)
    assert not (tmp_path / "dist" / "installers").exists() or not list((tmp_path / "dist").rglob("*.exe"))


def test_sign_without_certificate_warns_and_builds_unsigned(build_dir, tmp_path, capsys):
    res, _ = full_build(build_dir, tmp_path, sign=True)
    assert res["built"] and not res["signed"]
    assert "UNSIGNED" in capsys.readouterr().err


def test_osslsigncode_command_has_no_secret_in_argv(tmp_path):
    cfg = bi.SignConfig(pfx_file="/ci/cert.pfx", pfx_password="hunter2-PFX-PASSWORD", timestamp_url="http://ts.example/rfc3161")
    exe = tmp_path / "x.exe"
    exe.write_bytes(b"MZ")
    plan = bi.build_sign_command(exe, cfg, "osslsigncode", tmp_path)
    joined = " ".join(plan.argv)
    assert "hunter2" not in joined and "hunter2" not in json.dumps(plan.env)
    assert plan.argv[0] == "osslsigncode" and "-readpass" in plan.argv and "-ts" in plan.argv
    pw_file = Path(plan.argv[plan.argv.index("-readpass") + 1])
    assert pw_file.read_text() == "hunter2-PFX-PASSWORD" and plan.cleanup == [pw_file]
    # a password FILE (CI file variable) is used as-is, nothing copied
    plan2 = bi.build_sign_command(exe, bi.SignConfig(pfx_file="/ci/c.pfx", pfx_password_file="/ci/pw.txt"), "osslsigncode", tmp_path)
    assert plan2.argv[plan2.argv.index("-readpass") + 1] == "/ci/pw.txt" and plan2.cleanup == []
    with pytest.raises(bi.BuildError, match="Windows-only"):
        bi.build_sign_command(exe, bi.SignConfig(thumbprint="AB12"), "osslsigncode", tmp_path)


def test_powershell_signer_passes_password_via_environment(tmp_path):
    cfg = bi.SignConfig(pfx_file="C:/ci/cert.pfx", pfx_password="hunter2-PFX-PASSWORD", timestamp_url="http://ts.example")
    plan = bi.build_sign_command(tmp_path / "x.exe", cfg, "powershell", tmp_path)
    assert "hunter2" not in " ".join(plan.argv)
    assert plan.env == {"AMX_SIGN_PFX_PASSWORD": "hunter2-PFX-PASSWORD"}
    assert "-PfxPasswordEnv" in plan.argv and "AMX_SIGN_PFX_PASSWORD" in plan.argv and plan.argv[plan.argv.index("-TimestampUrl") + 1] == "http://ts.example"
    thumb = bi.build_sign_command(tmp_path / "x.exe", bi.SignConfig(thumbprint="AB 12"), "powershell", tmp_path)
    assert "-Thumbprint" in thumb.argv and thumb.env == {}


def test_signing_replaces_installer_and_logs_no_password(build_dir, tmp_path, capsys, monkeypatch):
    pw = tmp_path / "pw.txt"
    pw.write_text("topsecret-pfx-pass")
    extra = {"SIGNING_PFX_FILE": str(tmp_path / "c.pfx"), "SIGNING_PFX_PASSWORD_FILE": str(pw), "SIGNING_TIMESTAMP_URL": "http://ts.example"}
    res, runner = full_build(build_dir, tmp_path, extra, require_signing=True, signer="osslsigncode")
    exe = Path(res["path"])
    assert res["signed"] and exe.read_bytes() == b"MZ-fake-installer-SIGNED"
    out = capsys.readouterr()
    assert "topsecret" not in out.out + out.err
    # the sha256 describes the SIGNED file
    assert res["sha256"] == __import__("hashlib").sha256(exe.read_bytes()).hexdigest()


def test_signing_failure_is_an_error(build_dir, tmp_path):
    class Failing(FakeRunner):
        def __call__(self, argv, **kw):
            if argv[0] == "osslsigncode":
                return SimpleNamespace(returncode=1, stdout="", stderr="bad password")
            return super().__call__(argv, **kw)
    d, cfg_dict, env = build_dir
    cfg = bi.load_config(cfg_dict, base_dir=d)
    pwf = tmp_path / "p"
    pwf.write_text("x")
    with pytest.raises(bi.BuildError, match="signing failed"):
        bi.run_build(cfg, tmp_path / "d", {**env, "SIGNING_PFX_FILE": "c.pfx", "SIGNING_PFX_PASSWORD_FILE": str(pwf)},
                     makensis="fake-makensis", runner=Failing(), require_signing=True, signer="osslsigncode")


def test_missing_makensis_is_clear(build_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(bi, "find_makensis", lambda explicit: None)
    d, cfg_dict, env = build_dir
    with pytest.raises(bi.BuildError, match="makensis not found"):
        bi.run_build(bi.load_config(cfg_dict, base_dir=d), tmp_path / "dist", env)


def test_cli_render_only(build_dir, tmp_path):
    d, cfg, env = build_dir
    (d / "build.json").write_text(json.dumps(cfg))
    import os
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_installer.py"), "--config", str(d / "build.json"), "--out", str(tmp_path / "o"),
                        "--render-only", "--ring", "dev"], capture_output=True, text=True, env={**os.environ, **env})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert 'DEFAULT_RING    "dev"' in Path(out["nsi"]).read_text(encoding="utf-8")
    r2 = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_installer.py"), "--config", str(d / "build.json"), "--out", str(tmp_path / "o2"),
                         "--require-signing", "--render-only"], capture_output=True, text=True, env={**os.environ, **env})
    assert r2.returncode == 1 and "signing is required" in r2.stderr


def test_nsis_script_invocations_match_powershell_parameters():
    """Every `RunPS "x.ps1" '-Param ...'` in the template must use parameters the script really declares."""
    tpl = (ROOT / "installer" / "autostation.nsi.in").read_text(encoding="utf-8")
    calls = re.findall(r'!insertmacro RunPS "([\w-]+\.ps1)" \'([^\']*)\'', tpl)
    assert len(calls) >= 6
    for script, argstr in calls:
        body = (ROOT / "installer" / "scripts" / script).read_text(encoding="utf-8")
        param_block = body[body.index("param("):]
        for name in re.findall(r"(?:^|\s)-([A-Za-z]+)\b", argstr):
            assert re.search(rf"\${name}\b", param_block, re.I), f"{script} has no parameter -{name}"
    # install-check is invoked outside the macro (needs stdout)
    assert re.search(r"install-check\.ps1\" -InstallDir", tpl)
