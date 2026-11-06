"""Tests for the platform installer build script and the dropper mode of build_installer."""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import build_installer as bi
import build_platform_installer as bpi
import validate_nsi as vn
from conftest import PRIVATE_KEY, TOKEN, make_wrapper_zip, make_python_zip

ROOT = Path(__file__).resolve().parent.parent


# ---- platform config fixtures ---------------------------------------------------------------

def make_framework_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("VERSION", "0.5.0\n")
        z.writestr("index.js", "// stub\n")
    return path


@pytest.fixture
def platform_dir(tmp_path):
    d = tmp_path / "platform_build"
    d.mkdir()
    make_wrapper_zip(d / "wrapper-core-0.1.0.zip")
    make_python_zip(d / "python-embed.zip")
    make_framework_zip(d / "framework-0.5.0.zip")
    (d / "known_hosts").write_text("gitlab.example.internal ssh-ed25519 AAAAC3Nza\n")
    (d / "app.ico").write_bytes(b"\0\0\1\0")
    key = d / "id_logging"
    key.write_text(PRIVATE_KEY)
    tok = d / "token.txt"
    tok.write_text(TOKEN)
    cfg = {
        "version": "1.3.0", "ring": "pilot",
        "registry_url": "https://registry.example.internal/",
        "log_remote": "git@gitlab.example.internal:platform/automation-logging.git",
        "llm_endpoint": "https://llm.example.internal/v1/chat/completions",
        "llm_model": "default",
        "publisher": "Automation Platform Team",
        "icon": "app.ico",
        "wrapper_core_zip": "wrapper-core-0.1.0.zip",
        "python_embed_zip": "python-embed.zip",
        "framework_zip": "framework-0.5.0.zip",
        "known_hosts_file": "known_hosts",
    }
    env = {"LOGGING_SSH_KEY_FILE": str(key), "REGISTRY_READ_TOKEN_FILE": str(tok)}
    return d, cfg, env


@pytest.fixture(autouse=True)
def _fake_makensis(monkeypatch):
    # bpi imports find_makensis from bi, so patching bi is sufficient for both
    monkeypatch.setattr(bi, "find_makensis", lambda explicit: "fake-makensis")


# ---- platform config validation -------------------------------------------------------------

def test_platform_config_loads(platform_dir):
    d, cfg, _ = platform_dir
    pc = bpi.load_config(cfg, base_dir=d)
    assert pc.version == "1.3.0"
    assert pc.ring == "pilot"
    assert pc.framework_zip == "framework-0.5.0.zip"


@pytest.mark.parametrize("patch,msg", [
    ({"version": "banana"}, "invalid version"),
    ({"ring": "prod"}, "ring"),
    ({"log_remote": "https://not-ssh/"}, "ssh"),
    ({"llm_endpoint": "ftp://x"}, "llm_endpoint"),
    ({"mystery_key": 1}, "unknown keys"),
    ({"registry_url": ""}, "registry_url"),
])
def test_platform_config_validation(platform_dir, patch, msg):
    d, cfg, _ = platform_dir
    with pytest.raises(bi.BuildError, match=msg):
        bpi.load_config({**cfg, **patch}, base_dir=d)


# ---- platform staging -----------------------------------------------------------------------

def test_platform_stage_has_expected_layout(platform_dir, tmp_path):
    d, cfg, env = platform_dir
    pc = bpi.load_config(cfg, base_dir=d)
    flags, wv = bpi.stage_platform_build(pc, tmp_path / "s", env, allow_missing_secrets=False)
    stage = tmp_path / "s"
    assert (stage / "wrapper-core" / "VERSION").exists()
    assert (stage / "python" / "python.exe").exists()
    assert (stage / "execution-framework" / "VERSION").exists()
    assert (stage / "secrets" / "logging_key").exists()
    assert (stage / "secrets" / "registry_token").exists()
    assert flags["HAVE_PYTHON_EMBED"] and flags["HAVE_FRAMEWORK"] and flags["HAVE_SSH_KEY"] and flags["HAVE_TOKEN"]
    assert flags["HAVE_KNOWN_HOSTS"]
    # all platform scripts staged
    for name in bpi.PLATFORM_SCRIPTS:
        assert (stage / "scripts" / name).exists(), name


def test_platform_stage_no_automation_payload(platform_dir, tmp_path):
    """Platform staging must not produce a 'bundled' automation directory."""
    d, cfg, env = platform_dir
    pc = bpi.load_config(cfg, base_dir=d)
    bpi.stage_platform_build(pc, tmp_path / "s", env, allow_missing_secrets=False)
    assert not (tmp_path / "s" / "bundled").exists()


# ---- platform NSI rendering -----------------------------------------------------------------

def test_platform_nsi_renders_and_validates(platform_dir, tmp_path):
    d, cfg, env = platform_dir
    pc = bpi.load_config(cfg, base_dir=d)
    flags, wv = bpi.stage_platform_build(pc, tmp_path / "s", env, allow_missing_secrets=False)
    nsi_text = bpi.render_platform_nsi(pc, wv, flags, str(tmp_path / "out.exe"), ".", icon="icon.ico")
    nsi = tmp_path / "s" / "installer.nsi"
    nsi.write_text(nsi_text, encoding="utf-8")
    assert vn.validate(nsi) == [], vn.validate(nsi)
    assert 'RequestExecutionLevel user' in nsi_text
    assert '!define PLATFORM_VERSION' in nsi_text
    assert '!define AUTOMATION' not in nsi_text          # platform installer has no automation
    assert 'wrapper_core install' not in nsi_text        # no per-automation install call
    assert 'first-run-platform.ps1' in nsi_text
    assert 'platform-version.txt' in nsi_text
    assert "AUTOMAGIX-Platform" in nsi_text              # A/R Programs key
    assert "HKLM" not in nsi_text


def test_platform_nsi_secrets_not_in_rendered_text(platform_dir, tmp_path):
    d, cfg, env = platform_dir
    pc = bpi.load_config(cfg, base_dir=d)
    flags, wv = bpi.stage_platform_build(pc, tmp_path / "s", env, allow_missing_secrets=False)
    nsi_text = bpi.render_platform_nsi(pc, wv, flags, str(tmp_path / "out.exe"), ".", icon="")
    assert TOKEN not in nsi_text
    assert "SECRETKEYMATERIAL" not in nsi_text
    assert "BEGIN OPENSSH" not in nsi_text


# ---- dropper config validation --------------------------------------------------------------

def test_dropper_config_requires_requires_platform(build_dir):
    d, cfg, _ = build_dir
    with pytest.raises(bi.BuildError, match="requires_platform"):
        bi.load_config({**cfg, "dropper": True}, base_dir=d)


def test_dropper_config_loads_when_complete(build_dir):
    d, cfg, _ = build_dir
    dropper_cfg = bi.load_config({**cfg, "dropper": True, "requires_platform": "1.3.0"}, base_dir=d)
    assert dropper_cfg.dropper is True
    assert dropper_cfg.requires_platform == "1.3.0"


def test_dropper_config_rejects_bad_platform_version(build_dir):
    d, cfg, _ = build_dir
    with pytest.raises(bi.BuildError, match="requires_platform"):
        bi.load_config({**cfg, "dropper": True, "requires_platform": "not-a-version"}, base_dir=d)


# ---- dropper staging ------------------------------------------------------------------------

def test_dropper_stage_skips_platform_components(build_dir, tmp_path):
    d, cfg, env = build_dir
    dropper_cfg = bi.load_config({**cfg, "dropper": True, "requires_platform": "1.3.0"}, base_dir=d)
    flags, wv = bi.stage_build(dropper_cfg, tmp_path / "s", env, allow_missing_secrets=True)
    stage = tmp_path / "s"
    # platform components must NOT be staged by a dropper
    assert not (stage / "python").exists()
    assert not (stage / "wrapper-core").exists()
    assert not (stage / "execution-framework").exists()
    # wrapper_version in dropper is the platform compat string
    assert wv == "1.3.0"
    # only the dropper scripts are staged
    for name in bi.DROPPER_SCRIPTS:
        assert (stage / "scripts" / name).exists(), name
    for name in set(bi.STAGE_SCRIPTS) - set(bi.DROPPER_SCRIPTS):
        assert not (stage / "scripts" / name).exists(), f"{name} should not be in dropper"


def test_dropper_stage_can_include_automation_payload(build_dir, tmp_path):
    d, cfg, env = build_dir
    # create a minimal automation zip
    az = d / "hello.zip"
    with zipfile.ZipFile(az, "w") as z:
        z.writestr("automation.yaml", "name: hello\n")
        z.writestr("main.py", "# stub\n")
    dropper_cfg = bi.load_config(
        {**cfg, "dropper": True, "requires_platform": "1.3.0", "automation_zip": "hello.zip"}, base_dir=d)
    flags, _ = bi.stage_build(dropper_cfg, tmp_path / "s", env, allow_missing_secrets=True)
    assert flags["HAVE_PAYLOAD"]
    assert (tmp_path / "s" / "bundled" / f"{dropper_cfg.automation}.zip").exists()


# ---- dropper NSI rendering ------------------------------------------------------------------

def test_dropper_nsi_renders_and_validates(build_dir, tmp_path):
    d, cfg, env = build_dir
    dropper_cfg = bi.load_config({**cfg, "dropper": True, "requires_platform": "1.3.0"}, base_dir=d)
    flags, wv = bi.stage_build(dropper_cfg, tmp_path / "s", env, allow_missing_secrets=True)
    nsi_text = bi.render_nsi(dropper_cfg, wv, flags, str(tmp_path / "out.exe"), ".")
    nsi = tmp_path / "s" / "installer.nsi"
    nsi.write_text(nsi_text, encoding="utf-8")
    assert vn.validate(nsi) == [], vn.validate(nsi)
    assert 'RequestExecutionLevel user' in nsi_text
    assert '!define AUTOMATION' in nsi_text
    assert '!define REQUIRES_PLATFORM' in nsi_text
    assert '"1.3.0"' in nsi_text                         # requires-platform value embedded
    assert 'platform-version.txt' in nsi_text            # dropper reads this
    assert 'AUTOMAGIX/registry_token' not in nsi_text    # dropper doesn't install secrets
    assert "HKLM" not in nsi_text
    # platform check must precede the install section
    platform_check_pos = nsi_text.find("platform-version.txt")
    install_pos = nsi_text.find("Section")
    assert platform_check_pos < install_pos, "platform check must be in .onInit, before Section"


def test_dropper_nsi_uses_dropper_template(build_dir, tmp_path):
    """Dropper uses autostation-dropper.nsi.in, not autostation.nsi.in."""
    d, cfg, env = build_dir
    dropper_cfg = bi.load_config({**cfg, "dropper": True, "requires_platform": "1.3.0"}, base_dir=d)
    flags, wv = bi.stage_build(dropper_cfg, tmp_path / "s", env, allow_missing_secrets=True)
    nsi_text = bi.render_nsi(dropper_cfg, wv, flags, str(tmp_path / "out.exe"), ".")
    # dropper template comment is present; bundled-mode comment is not
    assert "autostation-dropper" in nsi_text.splitlines()[1].lower() or "dropper" in nsi_text[:400].lower()


def test_bundled_mode_nsi_unchanged(build_dir, tmp_path):
    """Standard (non-dropper) build still uses the original template."""
    d, cfg, env = build_dir
    std_cfg = bi.load_config(cfg, base_dir=d)
    flags, wv = bi.stage_build(std_cfg, tmp_path / "s", env, allow_missing_secrets=False)
    nsi_text = bi.render_nsi(std_cfg, wv, flags, str(tmp_path / "out.exe"), ".")
    assert "per-automation installer" in nsi_text or "AUTOSTATION per-automation" in nsi_text
    assert "REQUIRES_PLATFORM" not in nsi_text
