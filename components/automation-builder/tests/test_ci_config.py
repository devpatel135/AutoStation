"""Tests for ci_installer_config.py — make_config registry_pubkeys wiring."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ci_installer_config as cic

# A minimal valid 64-hex Ed25519 public key (all-zeroes stands in for a real one in tests)
FAKE_KEY = "a" * 64
FAKE_KEY2 = "b" * 64

BASE_ENV = {
    "REGISTRY_URL": "https://registry.example.internal/",
    "REGISTRY_READ_TOKEN": "fake-token",
    "BUILD_RING": "dev",
    "AUTOMATION_NAME": "automation-test-widget",
    "AUTOMATION_VERSION": "0.1.0",
    "LOG_REMOTE": "local",
    "PUBLISHER": "Test Team",
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """stub fetch_wrapper_core so tests never touch the network."""
    def fake_fetch(registry_url, ring, token, out_dir):
        zip_path = Path(out_dir) / "wrapper-core-0.1.0.zip"
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        import zipfile
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr("VERSION", "0.1.0\n")
        return zip_path
    monkeypatch.setattr(cic, "fetch_wrapper_core", fake_fetch)


# --- no pubkeys: key absent from build.json -------------------------------------------------------

def test_make_config_no_pubkeys_omits_key(tmp_path):
    cfg = cic.make_config(BASE_ENV, tmp_path / "build.json")
    assert "registry_pubkeys" not in cfg


# --- REGISTRY_PUBKEYS plain variable --------------------------------------------------------------

def test_make_config_registry_pubkeys_env(tmp_path):
    env = {**BASE_ENV, "REGISTRY_PUBKEYS": FAKE_KEY}
    cfg = cic.make_config(env, tmp_path / "build.json")
    assert cfg["registry_pubkeys"] == FAKE_KEY


def test_make_config_registry_pubkeys_multiple_keys(tmp_path):
    env = {**BASE_ENV, "REGISTRY_PUBKEYS": f"{FAKE_KEY},{FAKE_KEY2}"}
    cfg = cic.make_config(env, tmp_path / "build.json")
    assert FAKE_KEY in cfg["registry_pubkeys"]
    assert FAKE_KEY2 in cfg["registry_pubkeys"]


def test_make_config_registry_pubkeys_whitespace_stripped(tmp_path):
    env = {**BASE_ENV, "REGISTRY_PUBKEYS": f"  {FAKE_KEY}  "}
    cfg = cic.make_config(env, tmp_path / "build.json")
    assert cfg["registry_pubkeys"] == FAKE_KEY


# --- REGISTRY_PUBKEYS_FILE variable ---------------------------------------------------------------

def test_make_config_registry_pubkeys_file(tmp_path):
    kf = tmp_path / "pubkeys.txt"
    kf.write_text(FAKE_KEY + "\n", encoding="utf-8")
    env = {**BASE_ENV, "REGISTRY_PUBKEYS_FILE": str(kf)}
    cfg = cic.make_config(env, tmp_path / "build.json")
    assert cfg["registry_pubkeys"] == FAKE_KEY


def test_make_config_pubkeys_file_preferred_over_env(tmp_path):
    """When both are set, the File variable wins."""
    kf = tmp_path / "pubkeys.txt"
    kf.write_text(FAKE_KEY + "\n", encoding="utf-8")
    env = {**BASE_ENV, "REGISTRY_PUBKEYS_FILE": str(kf), "REGISTRY_PUBKEYS": FAKE_KEY2}
    cfg = cic.make_config(env, tmp_path / "build.json")
    assert FAKE_KEY in cfg["registry_pubkeys"]
    assert FAKE_KEY2 not in cfg["registry_pubkeys"]


def test_make_config_pubkeys_file_missing_raises(tmp_path):
    env = {**BASE_ENV, "REGISTRY_PUBKEYS_FILE": str(tmp_path / "nonexistent.txt")}
    with pytest.raises(cic.ConfigError, match="REGISTRY_PUBKEYS_FILE"):
        cic.make_config(env, tmp_path / "build.json")


# --- build.json written correctly -----------------------------------------------------------------

def test_make_config_pubkeys_written_to_json_file(tmp_path):
    env = {**BASE_ENV, "REGISTRY_PUBKEYS": FAKE_KEY}
    out = tmp_path / "build.json"
    cic.make_config(env, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["registry_pubkeys"] == FAKE_KEY


def test_make_config_empty_pubkeys_env_omits_key(tmp_path):
    env = {**BASE_ENV, "REGISTRY_PUBKEYS": ""}
    cfg = cic.make_config(env, tmp_path / "build.json")
    assert "registry_pubkeys" not in cfg
