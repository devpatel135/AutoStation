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


def fw_zip(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("VERSION", "0.3.0\n")
        z.writestr("index.js", "module.exports = {};\n")
        z.writestr("node/node.exe", "MZ-fake-node")
        z.writestr("node_modules/playwright-core/package.json", "{}")
    return path


def test_framework_bundle_and_logging_token_are_staged_and_rendered(tmp_path):
    fw_zip(tmp_path / "fw.zip")
    tok = tmp_path / "tok.txt"
    tok.write_text("github_pat_SECRETTOKENVALUE0123456789\n")
    cfg = config(tmp_path, framework_zip="fw.zip", log_remote="devpatel135/AutoStation-logs")
    stage = tmp_path / "stage"
    flags, wv = bi.stage_build(cfg, stage, {"LOGGING_TOKEN_FILE": str(tok)}, allow_missing_secrets=True)
    assert flags["HAVE_FRAMEWORK"] and flags["HAVE_LOG_TOKEN"]
    assert (stage / "execution-framework" / "node" / "node.exe").exists()
    assert (stage / "secrets" / "logging_token").read_text() == "github_pat_SECRETTOKENVALUE0123456789"
    nsi = stage / "i.nsi"
    nsi.write_text(bi.render_nsi(cfg, wv, flags, str(tmp_path / "o.exe"), ".", icon=""), encoding="utf-8")
    assert vn.validate(nsi) == []
    text = nsi.read_text(encoding="utf-8")
    assert "SECRETTOKENVALUE" not in text                      # secrets never appear in the script
    assert '!define GIT_ARG "-SkipGit"' in text                # github-api transport needs no git
    assert "logging_token.dpapi" in text and "execution-framework" in text


def test_ssh_remote_still_requires_git(tmp_path):
    cfg = config(tmp_path, log_remote="git@github.com:o/r.git")
    stage = tmp_path / "s"
    flags, wv = bi.stage_build(cfg, stage, {}, allow_missing_secrets=True)
    text = bi.render_nsi(cfg, wv, flags, "o.exe", ".", icon="")
    assert '!define GIT_ARG ""' in text


def test_framework_zip_must_be_a_real_framework(tmp_path):
    with zipfile.ZipFile(tmp_path / "bad.zip", "w") as z:
        z.writestr("readme.txt", "x")
    cfg = config(tmp_path, framework_zip="bad.zip")
    with pytest.raises(bi.BuildError, match="framework_zip"):
        bi.stage_build(cfg, tmp_path / "s4", {}, allow_missing_secrets=True)
