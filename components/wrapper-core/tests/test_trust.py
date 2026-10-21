"""Ed25519 (RFC 8032) and signed registry manifests (trust.py + selfheal.fetch_manifest)."""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ed25519_vectors import VECTORS  # noqa: E402
from wrapper_core import ed25519, selfheal, trust, runctx  # noqa: E402
from wrapper_core import runner as runner_mod  # noqa: E402
from wrapper_core.paths import Paths  # noqa: E402
from wrapper_core.runner import run_automation  # noqa: E402

from conftest import HELLO  # noqa: E402

SEED_A = bytes(range(32))
SEED_B = bytes(range(100, 132))
PUB_A = ed25519.keypair_from_seed(SEED_A)[1].hex()
PUB_B = ed25519.keypair_from_seed(SEED_B)[1].hex()
T1, T2, T3 = "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"


# --- ed25519 --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,seed,pub,msg,sig", VECTORS, ids=[v[0] for v in VECTORS])
def test_rfc8032_vectors(name, seed, pub, msg, sig):
    s, p = ed25519.keypair_from_seed(bytes.fromhex(seed))
    assert p.hex() == pub
    assert ed25519.sign(s, bytes.fromhex(msg)).hex() == sig
    assert ed25519.verify(p, bytes.fromhex(msg), bytes.fromhex(sig))


def test_verify_is_fast_and_rejects_tampering():
    _, _, _, msg, sig = next(v for v in VECTORS if v[0] == "TEST 1024")
    pub = bytes.fromhex(next(v for v in VECTORS if v[0] == "TEST 1024")[2])
    t0 = time.perf_counter()
    assert ed25519.verify(pub, bytes.fromhex(msg), bytes.fromhex(sig))
    assert time.perf_counter() - t0 < 1.0
    bad_msg = bytearray(bytes.fromhex(msg))
    bad_msg[0] ^= 1
    assert not ed25519.verify(pub, bytes(bad_msg), bytes.fromhex(sig))
    bad_sig = bytearray(bytes.fromhex(sig))
    bad_sig[10] ^= 1
    assert not ed25519.verify(pub, bytes.fromhex(msg), bytes(bad_sig))


def test_verify_never_raises_on_garbage():
    assert not ed25519.verify(b"short", b"m", b"x" * 64)
    assert not ed25519.verify(bytes(32), b"m", b"x" * 63)
    assert not ed25519.verify(b"\xff" * 32, b"m", b"\xff" * 64)
    # S >= group order (malleable signature) is refused
    _, pub = ed25519.keypair_from_seed(SEED_A)
    sig = ed25519.sign(SEED_A, b"m")
    s = int.from_bytes(sig[32:], "little") + ed25519._L
    assert not ed25519.verify(pub, b"m", sig[:32] + (s % (1 << 256)).to_bytes(32, "little"))


def test_keygen_cli(capsys):
    assert ed25519.main(["keygen"]) == 0
    out = capsys.readouterr().out
    seed_hex = out.split("SECRET")[1].split(":")[1].split()[0]
    pub_hex = out.split("pin this in the fleet):")[1].split()[0]
    assert ed25519.keypair_from_seed(bytes.fromhex(seed_hex))[1].hex() == pub_hex


# --- trust.verify_manifest --------------------------------------------------------------------------------------
def _signed(manifest: bytes, seed=SEED_A, at=T1) -> dict:
    return trust.make_sig_doc(manifest, seed, at)


def test_verify_manifest_ok_and_returns_signed_at():
    m = b"schema_version: 1\n"
    assert trust.verify_manifest(m, _signed(m), [PUB_A]) == T1
    assert trust.verify_manifest(m, json.dumps(_signed(m)), [PUB_A.upper(), PUB_B]) == T1  # JSON text, any key order/case


@pytest.mark.parametrize("mutate,reason", [
    (lambda d: d.update(manifest_sha256="0" * 64), "does not match"),
    (lambda d: d.update(signed_at=T2), "not made by a trusted"),            # replayed signature with a new timestamp
    (lambda d: d.update(sig=base64.b64encode(b"x" * 64).decode()), "not made by a trusted"),
    (lambda d: d.update(sig="!!!"), "base64"),
    (lambda d: d.update(v=2), "unsupported"),
    (lambda d: d.pop("sig"), "missing fields"),
    (lambda d: d.update(signed_at="yesterday"), "ISO-8601"),
])
def test_verify_manifest_rejections(mutate, reason):
    m = b"schema_version: 1\n"
    doc = _signed(m)
    mutate(doc)
    with pytest.raises(trust.TrustError, match=reason):
        trust.verify_manifest(m, doc, [PUB_A])


def test_verify_manifest_wrong_key_tampered_and_garbage():
    m = b"schema_version: 1\n"
    with pytest.raises(trust.TrustError, match="not made by a trusted"):
        trust.verify_manifest(m, _signed(m, SEED_B), [PUB_A])
    with pytest.raises(trust.TrustError, match="does not match"):
        trust.verify_manifest(m + b"# evil\n", _signed(m), [PUB_A])
    with pytest.raises(trust.TrustError, match="not valid JSON"):
        trust.verify_manifest(m, "{not json", [PUB_A])
    with pytest.raises(trust.TrustError, match="no trusted"):
        trust.verify_manifest(m, _signed(m), [])


def test_configured_keys_merge_sources(monkeypatch):
    monkeypatch.setattr(trust, "PINNED_REGISTRY_KEYS", [PUB_A])
    monkeypatch.setenv("AUTOMAGIX_REGISTRY_PUBKEYS", f"{PUB_B}, not-a-key")
    assert trust.configured_keys({"registry_pubkeys": [PUB_A.upper(), "zz"]}) == [PUB_A, PUB_B]
    monkeypatch.setattr(trust, "PINNED_REGISTRY_KEYS", [])
    monkeypatch.delenv("AUTOMAGIX_REGISTRY_PUBKEYS")
    assert trust.configured_keys({}) == []


def test_trust_show_prints_key_ids_only(monkeypatch, capsys):
    monkeypatch.setenv("AUTOMAGIX_REGISTRY_PUBKEYS", PUB_A)
    from wrapper_core.cli import main
    assert main(["trust", "--show"]) == 0
    out = capsys.readouterr().out
    assert PUB_A[:8] in out and PUB_A not in out
    monkeypatch.delenv("AUTOMAGIX_REGISTRY_PUBKEYS")
    assert main(["trust", "--show"]) == 0
    assert "UNSIGNED" in capsys.readouterr().out


# --- selfheal.fetch_manifest ---------------------------------------------------------------------------------------
def _publish(registry, seed=SEED_A, at=T1, *, sign=True):
    registry.save()
    raw = (registry.root / "manifest.yaml").read_bytes()
    sig = registry.root / "manifest.yaml.sig"
    if sign:
        sig.write_text(json.dumps(trust.make_sig_doc(raw, seed, at)), encoding="utf-8")
    elif sig.exists():
        sig.unlink()
    return raw


def _fetch(registry, keys=(PUB_A,), notes=None):
    p = Paths().ensure()
    return selfheal.fetch_manifest(p, str(registry.root), None, notes=notes, cfg={"registry_pubkeys": list(keys)})


def test_signed_manifest_accepted_and_state_stored(registry):
    registry.add_component("wrapper-core", "0.1.0")
    _publish(registry)
    notes = []
    m, offline = _fetch(registry, notes=notes)
    assert not offline and notes == [] and "wrapper-core" in m["components"]
    p = Paths()
    assert p.manifest_signed_at.read_text().strip() == T1
    assert json.loads(p.manifest_sig_cache.read_text())["signed_at"] == T1


def test_missing_signature_rejected_and_cache_kept(registry):
    registry.add_component("wrapper-core", "0.1.0")
    _publish(registry)
    _fetch(registry)  # populates the signed cache
    registry.add_component("wrapper-core", "0.2.0")
    _publish(registry, sign=False)  # attacker serves a new manifest without a signature
    notes = []
    m, offline = _fetch(registry, notes=notes)
    assert offline and any(n.startswith("manifest signature invalid:") for n in notes)
    assert list(m["components"]["wrapper-core"]["versions"]) == ["0.1.0"]  # the old, verified manifest


def test_wrong_key_signature_rejected(registry):
    registry.add_component("wrapper-core", "0.1.0")
    _publish(registry, seed=SEED_B)
    notes = []
    with pytest.raises(selfheal.HealError):  # nothing cached, nothing trusted
        _fetch(registry, notes=notes)
    assert any("not made by a trusted registry key" in n for n in notes)
    assert not Paths().manifest_cache.exists()  # an unverified manifest is never cached


def test_tampered_manifest_rejected(registry):
    registry.add_component("wrapper-core", "0.1.0")
    _publish(registry)
    _fetch(registry)
    (registry.root / "manifest.yaml").write_text((registry.root / "manifest.yaml").read_text() + "\n# injected\n", encoding="utf-8")
    notes = []
    _m, offline = _fetch(registry, notes=notes)
    assert offline and any("does not match the signed sha256" in n for n in notes)


def test_rollback_to_older_signed_manifest_refused(registry):
    registry.add_component("wrapper-core", "0.1.0")
    old = _publish(registry, at=T1)
    old_sig = (registry.root / "manifest.yaml.sig").read_text()
    registry.add_component("wrapper-core", "0.2.0")
    _publish(registry, at=T2)
    _fetch(registry)
    assert Paths().manifest_signed_at.read_text().strip() == T2
    # an attacker replays the genuine, older (vulnerable) manifest + its genuine signature
    (registry.root / "manifest.yaml").write_bytes(old)
    (registry.root / "manifest.yaml.sig").write_text(old_sig, encoding="utf-8")
    notes = []
    m, offline = _fetch(registry, notes=notes)
    assert offline and any("rollback refused" in n for n in notes)
    assert "0.2.0" in m["components"]["wrapper-core"]["versions"]
    assert Paths().manifest_signed_at.read_text().strip() == T2


def test_same_signed_at_different_manifest_refused(registry):
    registry.add_component("wrapper-core", "0.1.0")
    _publish(registry, at=T2)
    _fetch(registry)
    registry.add_component("wrapper-core", "0.3.0")
    _publish(registry, at=T2)  # same timestamp, different content
    notes = []
    _m, offline = _fetch(registry, notes=notes)
    assert offline and any("already-accepted signed_at" in n for n in notes)


def test_newer_signed_manifest_advances_state(registry):
    registry.add_component("wrapper-core", "0.1.0")
    _publish(registry, at=T1)
    _fetch(registry)
    registry.add_component("wrapper-core", "0.2.0")
    _publish(registry, at=T3)
    m, offline = _fetch(registry)
    assert not offline and "0.2.0" in m["components"]["wrapper-core"]["versions"]
    assert Paths().manifest_signed_at.read_text().strip() == T3


def test_tampered_cache_is_ignored(registry):
    registry.add_component("wrapper-core", "0.1.0")
    _publish(registry)
    _fetch(registry)
    p = Paths()
    p.manifest_cache.write_text(p.manifest_cache.read_text() + "\n# local tamper\n", encoding="utf-8")
    (registry.root / "manifest.yaml.sig").unlink()  # registry also unreachable/unsigned -> must use the cache... which is now bad
    notes = []
    with pytest.raises(selfheal.HealError):
        _fetch(registry, notes=notes)
    assert any("cached manifest signature invalid" in n for n in notes)


def test_cache_saved_before_a_key_was_pinned_is_not_trusted(registry):
    registry.add_component("wrapper-core", "0.1.0")
    _publish(registry, sign=False)
    p = Paths().ensure()
    selfheal.fetch_manifest(p, str(registry.root), None, cfg={})  # unsigned registry, no key: accepted (with a warning)
    assert p.manifest_cache.exists()
    (registry.root / "manifest.yaml").unlink()  # outage
    notes = []
    with pytest.raises(selfheal.HealError):
        _fetch(registry, notes=notes)  # keys now configured: the unsigned cache is refused
    assert any("no signature" in n for n in notes)


def test_unsigned_registry_accepted_when_no_key_configured(registry, caplog):
    registry.add_component("wrapper-core", "0.1.0")
    _publish(registry, sign=False)
    p = Paths().ensure()
    m, offline = selfheal.fetch_manifest(p, str(registry.root), None, cfg={})
    assert not offline and "wrapper-core" in m["components"]
    assert "unsigned" in caplog.text


# --- end to end ----------------------------------------------------------------------------------------------------
def test_run_automation_with_bad_signature_applies_no_update(registry, monkeypatch):
    from test_runner import setup_reg
    monkeypatch.setattr(runner_mod, "_sleep", lambda s: None)
    monkeypatch.setenv("AUTOMAGIX_REGISTRY_PUBKEYS", PUB_A)
    setup_reg(registry)
    _publish(registry, at=T1)
    runctx.set_current(None)
    rec = run_automation("auto-a", "daily").record
    assert rec["status"] == "success" and rec["automation_version"] == "1.0.0"
    # attacker publishes a "2.0.0" with a signature from the wrong key
    registry.add_automation("auto-a", "2.0.0", "raise SystemExit('pwned')\n")
    _publish(registry, seed=SEED_B, at=T2)
    runctx.set_current(None)
    rec = run_automation("auto-a", "daily").record
    assert rec["status"] == "success" and rec["automation_version"] == "1.0.0"  # installed payload still runs, not updated
    assert any("manifest signature invalid" in e for e in rec["self_heal"]["errors"])
    assert rec["self_heal"]["installed"] == [] and rec["self_heal"]["offline"] is True
    assert Paths().automation_dir("auto-a").joinpath("VERSION").read_text().strip() == "1.0.0"


def test_run_automation_applies_a_correctly_signed_update(registry, monkeypatch):
    from test_runner import setup_reg
    monkeypatch.setattr(runner_mod, "_sleep", lambda s: None)
    monkeypatch.setenv("AUTOMAGIX_REGISTRY_PUBKEYS", PUB_A)
    setup_reg(registry)
    _publish(registry, at=T1)
    runctx.set_current(None)
    assert run_automation("auto-a", "daily").record["automation_version"] == "1.0.0"
    registry.add_automation("auto-a", "1.1.0", HELLO)
    _publish(registry, at=T2)
    runctx.set_current(None)
    rec = run_automation("auto-a", "daily").record
    assert rec["automation_version"] == "1.1.0" and rec["self_heal"]["errors"] == []
