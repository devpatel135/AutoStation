from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "components" / "wrapper-core"))

import sign_registry as sr  # noqa: E402
from wrapper_core import ed25519, trust  # noqa: E402

SEED = bytes(range(32))
PUB = ed25519.keypair_from_seed(SEED)[1].hex()


def reg(tmp_path, triggers=True):
    (tmp_path / "manifest.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    if triggers:
        (tmp_path / "triggers.json").write_text('{"triggers": []}', encoding="utf-8")
    return tmp_path


def test_signs_manifest_seq_and_triggers_digest(tmp_path, monkeypatch):
    reg(tmp_path)
    monkeypatch.setenv("REGISTRY_SIGNING_SEED", SEED.hex())
    monkeypatch.setenv("SIGN_SEQ", "42")
    assert sr.main(["--registry-dir", str(tmp_path)]) == 0
    doc = json.loads((tmp_path / "manifest.yaml.sig").read_text(encoding="utf-8"))
    v = trust.verify_manifest_doc((tmp_path / "manifest.yaml").read_bytes(), doc, [PUB])
    assert v.seq == 42
    assert v.extras == {"triggers.json": hashlib.sha256((tmp_path / "triggers.json").read_bytes()).hexdigest()}


def test_no_triggers_file_means_no_extras_and_no_seq_means_legacy(tmp_path, monkeypatch):
    reg(tmp_path, triggers=False)
    monkeypatch.setenv("REGISTRY_SIGNING_SEED", SEED.hex())
    monkeypatch.delenv("SIGN_SEQ", raising=False)
    sr.main(["--registry-dir", str(tmp_path)])
    doc = json.loads((tmp_path / "manifest.yaml.sig").read_text(encoding="utf-8"))
    assert "sig2" not in doc and trust.verify_manifest_doc((tmp_path / "manifest.yaml").read_bytes(), doc, [PUB]).seq is None


@pytest.mark.parametrize("bad", ["-1", "abc", "1.5", str(2 ** 60)])
def test_bad_seq_is_refused(tmp_path, monkeypatch, bad):
    reg(tmp_path)
    monkeypatch.setenv("REGISTRY_SIGNING_SEED", SEED.hex())
    monkeypatch.setenv("SIGN_SEQ", bad)
    with pytest.raises(SystemExit):
        sr.main(["--registry-dir", str(tmp_path)])


def test_without_a_seed_nothing_is_signed(tmp_path, monkeypatch, capsys):
    reg(tmp_path)
    monkeypatch.delenv("REGISTRY_SIGNING_SEED", raising=False)
    assert sr.main(["--registry-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "manifest.yaml.sig").exists()
