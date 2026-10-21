from __future__ import annotations

import json
import zipfile

import pytest

import build_installer as bi
import validate_nsi as vn
from conftest import make_python_zip, make_wrapper_zip


@pytest.fixture(autouse=True)
def _fake_makensis(monkeypatch):
    monkeypatch.setattr(bi, "find_makensis", lambda explicit: "fake-makensis")


def payload_zip(path, with_descriptor=True):
    with zipfile.ZipFile(path, "w") as z:
        if with_descriptor:
            z.writestr("automation.yaml", "name: automation-x\nversion: 1.0.0\nruntime: python\nentrypoint: main.py\n")
        z.writestr("main.py", "print('hi')\n")
    return path


def config(tmp_path, **extra):
    make_wrapper_zip(tmp_path / "w.zip")
    make_python_zip(tmp_path / "p.zip")
    payload_zip(tmp_path / "a.zip")
    raw = {"automation": "automation-x", "version": "1.0.0", "ring": "broad", "registry_url": "https://example.github.io/AutoStation/registry/",
           "log_remote": "local", "wrapper_core_zip": "w.zip", "python_embed_zip": "p.zip", "automation_zip": "a.zip"}
    raw.update(extra)
    return bi.load_config(raw, base_dir=tmp_path)


def test_standalone_installer_bundles_payload_and_needs_no_logging_server(tmp_path):
    cfg = config(tmp_path)
    stage = tmp_path / "stage"
    flags, wv = bi.stage_build(cfg, stage, {}, allow_missing_secrets=True)
    assert flags["HAVE_PAYLOAD"] is True
    meta = json.loads((stage / "bundled" / "automation-x.json").read_text())
    assert meta["version"] == "1.0.0" and len(meta["sha256"]) == 64
    nsi = stage / "i.nsi"
    nsi.write_text(bi.render_nsi(cfg, wv, flags, str(tmp_path / "o.exe"), ".", icon=""), encoding="utf-8")
    assert vn.validate(nsi) == []
    text = nsi.read_text(encoding="utf-8")
    assert "!define HAVE_PAYLOAD" in text and 'bundled' in text and '!define LOG_REMOTE      "local"' in text


def test_log_remote_local_is_the_only_non_ssh_value_accepted_outside_dev(tmp_path):
    config(tmp_path)  # ok
    with pytest.raises(bi.BuildError):
        config(tmp_path, log_remote="https://example.com/x")


def test_payload_must_contain_descriptor(tmp_path):
    cfg = config(tmp_path)
    payload_zip(tmp_path / "a.zip", with_descriptor=False)
    with pytest.raises(bi.BuildError, match="automation.yaml"):
        bi.stage_build(cfg, tmp_path / "s2", {}, allow_missing_secrets=True)


def test_missing_payload_file_is_reported(tmp_path):
    cfg = config(tmp_path, automation_zip="nope.zip")
    with pytest.raises(bi.BuildError, match="not found"):
        bi.stage_build(cfg, tmp_path / "s3", {}, allow_missing_secrets=True)
