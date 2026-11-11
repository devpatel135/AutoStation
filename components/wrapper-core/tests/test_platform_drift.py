"""Tests for selfheal.installed_platform_version and the platform version drift check in selfheal.run."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wrapper_core import selfheal
from wrapper_core.paths import Paths

# Minimal valid manifest (schema_version required by mf.parse)
_MANIFEST_BASE = {
    "schema_version": 1,
    "components": {},
    "automations": {},
}


def _manifest_with_platform(target_version: str, ring: str = "pilot") -> dict:
    return {
        **_MANIFEST_BASE,
        "components": {
            "platform": {
                "rings": {ring: target_version},
                "versions": {target_version: {"url": "platform.zip", "sha256": "a" * 64}},
            }
        },
    }


def make_paths(tmp_path) -> Paths:
    p = Paths(tmp_path / "home")
    p.ensure()
    return p


# --- installed_platform_version ------------------------------------------------------------------

def test_installed_platform_version_absent(tmp_path):
    paths = make_paths(tmp_path)
    assert selfheal.installed_platform_version(paths) is None


def test_installed_platform_version_present(tmp_path):
    paths = make_paths(tmp_path)
    (paths.root / "platform-version.txt").write_text("1.3.0", encoding="utf-8")
    assert selfheal.installed_platform_version(paths) == "1.3.0"


def test_installed_platform_version_strips_whitespace(tmp_path):
    paths = make_paths(tmp_path)
    (paths.root / "platform-version.txt").write_text("  2.0.0\n", encoding="utf-8")
    assert selfheal.installed_platform_version(paths) == "2.0.0"


def test_installed_platform_version_empty_file_returns_none(tmp_path):
    paths = make_paths(tmp_path)
    (paths.root / "platform-version.txt").write_text("", encoding="utf-8")
    assert selfheal.installed_platform_version(paths) is None


# --- drift check via selfheal.run ----------------------------------------------------------------

def _run_with_manifest(tmp_path, manifest: dict, platform_txt: str | None, ring: str = "pilot") -> selfheal.HealResult:
    """Run selfheal against a local manifest file (no network); return the HealResult."""
    paths = make_paths(tmp_path)

    # write platform-version.txt
    if platform_txt is not None:
        (paths.root / "platform-version.txt").write_text(platform_txt, encoding="utf-8")

    # write manifest to a local file, use directory path as registry (selfheal reads file directly)
    manifest_text = json.dumps(manifest)   # JSON is valid YAML; miniyaml parses it
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir()
    (reg_dir / "manifest.yaml").write_text(manifest_text, encoding="utf-8")

    cfg = {
        "ring": ring,
        "registry_url": str(reg_dir),    # local path: selfheal reads it as a file path
        "log_transport": "dir",
        "log_remote": str(tmp_path / "logs"),
    }
    return selfheal.run(paths, cfg, None)


def test_no_platform_entry_in_manifest_no_error(tmp_path):
    """When the manifest has no components.platform, the drift check is skipped."""
    result = _run_with_manifest(tmp_path, _MANIFEST_BASE, platform_txt=None)
    drift_errors = [e for e in result.errors if "platform" in e.lower() and "drift" in e.lower()]
    assert not drift_errors


def test_platform_current_no_error(tmp_path):
    """Installed == manifest target: no drift error."""
    result = _run_with_manifest(tmp_path, _manifest_with_platform("1.3.0"), platform_txt="1.3.0")
    drift_errors = [e for e in result.errors if "drift" in e]
    assert not drift_errors


def test_platform_missing_when_manifest_expects_it(tmp_path):
    """platform-version.txt absent while manifest has a platform entry: error reported."""
    result = _run_with_manifest(tmp_path, _manifest_with_platform("1.3.0"), platform_txt=None)
    assert any("platform not installed" in e for e in result.errors)
    assert any("1.3.0" in e for e in result.errors)


def test_platform_stale_reports_drift(tmp_path):
    """Installed platform version older than manifest target: drift error."""
    result = _run_with_manifest(tmp_path, _manifest_with_platform("2.0.0"), platform_txt="1.3.0")
    assert any("drift" in e for e in result.errors)
    assert any("1.3.0" in e for e in result.errors)
    assert any("2.0.0" in e for e in result.errors)


def test_platform_newer_than_target_no_drift(tmp_path):
    """Installed platform ahead of manifest (possible during staged rollouts): not flagged as drift."""
    result = _run_with_manifest(tmp_path, _manifest_with_platform("1.0.0"), platform_txt="2.0.0")
    drift_errors = [e for e in result.errors if "drift" in e]
    assert not drift_errors


def test_platform_drift_does_not_halt_run(tmp_path):
    """Drift is advisory: the run continues (halted stays False)."""
    result = _run_with_manifest(tmp_path, _manifest_with_platform("2.0.0"), platform_txt="1.3.0")
    assert not result.halted
