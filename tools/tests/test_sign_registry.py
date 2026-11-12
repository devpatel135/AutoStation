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
    monkeypatch.setenv("SIGN_SEQ", "1800000042")
    assert sr.main(["--registry-dir", str(tmp_path)]) == 0
    doc = json.loads((tmp_path / "manifest.yaml.sig").read_text(encoding="utf-8"))
    v = trust.verify_manifest_doc((tmp_path / "manifest.yaml").read_bytes(), doc, [PUB])
    assert v.seq == 1800000042
    assert v.extras == {"triggers.json": hashlib.sha256((tmp_path / "triggers.json").read_bytes()).hexdigest()}


def test_no_triggers_file_means_no_extras_and_an_empty_seq_is_derived_never_legacy(tmp_path, monkeypatch):
    reg(tmp_path, triggers=False)
    monkeypatch.setenv("REGISTRY_SIGNING_SEED", SEED.hex())
    monkeypatch.delenv("SIGN_SEQ", raising=False)
    sr.main(["--registry-dir", str(tmp_path)])
    doc = json.loads((tmp_path / "manifest.yaml.sig").read_text(encoding="utf-8"))
    v = trust.verify_manifest_doc((tmp_path / "manifest.yaml").read_bytes(), doc, [PUB])
    assert "sig2" in doc and v.seq is not None and v.seq >= sr.TIMESTAMP_SCALE and v.extras == {}


def test_a_counter_style_seq_such_as_a_run_number_is_ignored(tmp_path, monkeypatch, capsys):
    reg(tmp_path)
    monkeypatch.setenv("REGISTRY_SIGNING_SEED", SEED.hex())
    monkeypatch.setenv("SIGN_SEQ", "57")
    sr.main(["--registry-dir", str(tmp_path)])
    assert "ignored" in capsys.readouterr().err
    doc = json.loads((tmp_path / "manifest.yaml.sig").read_text(encoding="utf-8"))
    assert doc["seq"] >= sr.TIMESTAMP_SCALE


def test_seq_never_goes_backwards_against_the_previous_signature(tmp_path, monkeypatch):
    reg(tmp_path)
    monkeypatch.setenv("REGISTRY_SIGNING_SEED", SEED.hex())
    monkeypatch.delenv("SIGN_SEQ", raising=False)
    future = 4_000_000_000
    (tmp_path / "manifest.yaml.sig").write_text(json.dumps({"seq": future}), encoding="utf-8")
    sr.main(["--registry-dir", str(tmp_path)])                                   # derived value is bumped above the previous one
    assert json.loads((tmp_path / "manifest.yaml.sig").read_text(encoding="utf-8"))["seq"] == future + 1
    monkeypatch.setenv("SIGN_SEQ", str(future))                                  # explicit but not higher: refused
    with pytest.raises(SystemExit, match="not higher"):
        sr.main(["--registry-dir", str(tmp_path)])


def test_reset_needs_an_epoch_and_writes_a_verifiable_v3_document(tmp_path, monkeypatch):
    reg(tmp_path)
    monkeypatch.setenv("REGISTRY_SIGNING_SEED", SEED.hex())
    monkeypatch.setenv("SIGN_RESET_SEQ", "1")
    with pytest.raises(SystemExit, match="SIGN_EPOCH"):
        sr.main(["--registry-dir", str(tmp_path)])
    monkeypatch.setenv("SIGN_EPOCH", "3")
    sr.main(["--registry-dir", str(tmp_path)])
    doc = json.loads((tmp_path / "manifest.yaml.sig").read_text(encoding="utf-8"))
    v = trust.verify_manifest_doc((tmp_path / "manifest.yaml").read_bytes(), doc, [PUB])
    assert v.epoch == 3 and v.reset_seq


def test_rotation_run_endorses_and_cosigns_the_new_key(tmp_path, monkeypatch):
    reg(tmp_path)
    seed_b = bytes(range(7, 39))
    pub_b = ed25519.keypair_from_seed(seed_b)[1].hex()
    monkeypatch.setenv("REGISTRY_SIGNING_SEED", SEED.hex())
    monkeypatch.setenv("SIGN_EPOCH", "1")
    monkeypatch.setenv("SIGN_TRUSTED_KEYS", pub_b)
    monkeypatch.setenv("REGISTRY_COSIGN_SEEDS", seed_b.hex())
    sr.main(["--registry-dir", str(tmp_path)])
    doc = json.loads((tmp_path / "manifest.yaml.sig").read_text(encoding="utf-8"))
    assert trust.verify_manifest_doc((tmp_path / "manifest.yaml").read_bytes(), doc, [PUB]).trusted_keys == (pub_b,)


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
