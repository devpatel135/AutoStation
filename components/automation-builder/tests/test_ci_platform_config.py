"""Tests for ci_platform_config.py — make_config field wiring."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ci_platform_config as cpc

FAKE_KEY = "a" * 64
FAKE_KEY2 = "b" * 64

BASE_ENV = {
    "PLATFORM_VERSION": "1.3.0",
    "REGISTRY_URL": "https://registry.example.internal/",
    "LOG_REMOTE": "git@gitlab.example.internal:platform/automation-logging.git",
}


# --- required fields -------------------------------------------------------------------------------

def test_make_config_required_fields(tmp_path):
    cfg = cpc.make_config(BASE_ENV, tmp_path / "platform-build.json")
    assert cfg["version"] == "1.3.0"
    assert cfg["registry_url"] == BASE_ENV["REGISTRY_URL"]
    assert cfg["log_remote"] == BASE_ENV["LOG_REMOTE"]


def test_make_config_missing_version_raises(tmp_path):
    env = {k: v for k, v in BASE_ENV.items() if k != "PLATFORM_VERSION"}
    with pytest.raises(cpc.ConfigError, match="PLATFORM_VERSION"):
        cpc.make_config(env, tmp_path / "platform-build.json")


def test_make_config_missing_registry_url_raises(tmp_path):
    env = {k: v for k, v in BASE_ENV.items() if k != "REGISTRY_URL"}
    with pytest.raises(cpc.ConfigError, match="REGISTRY_URL"):
        cpc.make_config(env, tmp_path / "platform-build.json")


def test_make_config_missing_log_remote_raises(tmp_path):
    env = {k: v for k, v in BASE_ENV.items() if k != "LOG_REMOTE"}
    with pytest.raises(cpc.ConfigError, match="LOG_REMOTE"):
        cpc.make_config(env, tmp_path / "platform-build.json")


# --- defaults ---------------------------------------------------------------------------------------

def test_make_config_default_ring_is_broad(tmp_path):
    cfg = cpc.make_config(BASE_ENV, tmp_path / "platform-build.json")
    assert cfg["ring"] == "broad"


def test_make_config_build_ring_override(tmp_path):
    env = {**BASE_ENV, "BUILD_RING": "pilot"}
    cfg = cpc.make_config(env, tmp_path / "platform-build.json")
    assert cfg["ring"] == "pilot"


def test_make_config_default_publisher(tmp_path):
    cfg = cpc.make_config(BASE_ENV, tmp_path / "platform-build.json")
    assert cfg["publisher"] == "AUTOMAGIX"


# --- optional file/path variables -------------------------------------------------------------------

def test_make_config_optional_file_vars(tmp_path):
    env = {**BASE_ENV, "PYTHON_EMBED_ZIP": "/path/to/python.zip",
           "FRAMEWORK_ZIP": "/path/to/framework.zip", "GIT_ZIP": "/path/to/git.zip"}
    cfg = cpc.make_config(env, tmp_path / "platform-build.json")
    assert cfg["python_embed_zip"] == "/path/to/python.zip"
    assert cfg["framework_zip"] == "/path/to/framework.zip"
    assert cfg["git_zip"] == "/path/to/git.zip"


def test_make_config_optional_vars_absent_omitted(tmp_path):
    cfg = cpc.make_config(BASE_ENV, tmp_path / "platform-build.json")
    for key in ("python_embed_zip", "framework_zip", "git_zip", "icon", "known_hosts_file"):
        assert key not in cfg


# --- registry_pubkeys (same file-preferred pattern as ci_installer_config) --------------------------

def test_make_config_no_pubkeys_omits_key(tmp_path):
    cfg = cpc.make_config(BASE_ENV, tmp_path / "platform-build.json")
    assert "registry_pubkeys" not in cfg


def test_make_config_pubkeys_env_var(tmp_path):
    env = {**BASE_ENV, "REGISTRY_PUBKEYS": FAKE_KEY}
    cfg = cpc.make_config(env, tmp_path / "platform-build.json")
    assert cfg["registry_pubkeys"] == FAKE_KEY


def test_make_config_pubkeys_file_preferred_over_env(tmp_path):
    kf = tmp_path / "pubkeys.txt"
    kf.write_text(FAKE_KEY + "\n", encoding="utf-8")
    env = {**BASE_ENV, "REGISTRY_PUBKEYS_FILE": str(kf), "REGISTRY_PUBKEYS": FAKE_KEY2}
    cfg = cpc.make_config(env, tmp_path / "platform-build.json")
    assert FAKE_KEY in cfg["registry_pubkeys"]
    assert FAKE_KEY2 not in cfg["registry_pubkeys"]


def test_make_config_pubkeys_file_strips_whitespace(tmp_path):
    kf = tmp_path / "pubkeys.txt"
    kf.write_text(f"  {FAKE_KEY}  \n", encoding="utf-8")
    env = {**BASE_ENV, "REGISTRY_PUBKEYS_FILE": str(kf)}
    cfg = cpc.make_config(env, tmp_path / "platform-build.json")
    assert cfg["registry_pubkeys"] == FAKE_KEY


def test_make_config_pubkeys_file_missing_raises(tmp_path):
    env = {**BASE_ENV, "REGISTRY_PUBKEYS_FILE": str(tmp_path / "nonexistent.txt")}
    with pytest.raises(cpc.ConfigError, match="REGISTRY_PUBKEYS_FILE"):
        cpc.make_config(env, tmp_path / "platform-build.json")


# --- output file ------------------------------------------------------------------------------------

def test_make_config_written_to_json(tmp_path):
    env = {**BASE_ENV, "REGISTRY_PUBKEYS": FAKE_KEY}
    out = tmp_path / "platform-build.json"
    cpc.make_config(env, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["version"] == "1.3.0"
    assert data["registry_pubkeys"] == FAKE_KEY


def test_make_config_creates_parent_dirs(tmp_path):
    out = tmp_path / "sub" / "dir" / "platform-build.json"
    cpc.make_config(BASE_ENV, out)
    assert out.exists()
