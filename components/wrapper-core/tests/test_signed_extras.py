"""Signed extras (triggers.json digest) and the monotonic seq anti-rollback (trust.py + selfheal.py)."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wrapper_core import ed25519, selfheal, trust  # noqa: E402
from wrapper_core.paths import Paths  # noqa: E402

SEED_A = bytes(range(32))
SEED_B = bytes(range(100, 132))
PUB_A = ed25519.keypair_from_seed(SEED_A)[1].hex()
PUB_B = ed25519.keypair_from_seed(SEED_B)[1].hex()
T1, T2, T3 = "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"
TRIG = json.dumps({"triggers": [{"id": "t1", "automation": "auto-a", "slot": "poll", "params": {}}]}).encode()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def publish(registry, seed=SEED_A, at=T1, seq=None, triggers=TRIG, sign_extras=True):
    registry.save()
    raw = (registry.root / "manifest.yaml").read_bytes()
    if triggers is not None:
        (registry.root / "triggers.json").write_bytes(triggers)
    extras = {"triggers.json": sha(triggers)} if (triggers is not None and sign_extras) else None
    doc = trust.make_sig_doc(raw, seed, at, seq=seq, extras=extras)
    (registry.root / "manifest.yaml.sig").write_text(json.dumps(doc), encoding="utf-8")
    return raw, doc


def fetch(registry, keys=(PUB_A,), notes=None):
    p = Paths().ensure()
    return selfheal.fetch_manifest(p, str(registry.root), None, notes=notes, cfg={"registry_pubkeys": list(keys)})


# --- the signature document ------------------------------------------------------------------------------------------
def test_v1_document_has_no_seq_or_extras_and_still_verifies():
    m = b"schema_version: 1\n"
    doc = trust.make_sig_doc(m, SEED_A, T1)
    assert "sig2" not in doc and "seq" not in doc and "extras" not in doc
    v = trust.verify_manifest_doc(m, doc, [PUB_A])
    assert (v.signed_at, v.seq, v.extras) == (T1, None, {})


def test_seq_and_extras_are_signed_and_returned():
    m = b"schema_version: 1\n"
    doc = trust.make_sig_doc(m, SEED_A, T1, seq=7, extras={"triggers.json": sha(TRIG)})
    v = trust.verify_manifest_doc(m, doc, [PUB_A])
    assert v.seq == 7 and v.extras == {"triggers.json": sha(TRIG)} and v.key_id == PUB_A[:8]
    assert trust.verify_manifest(m, doc, [PUB_A]) == T1          # the old API still returns signed_at


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(seq=8),                                   # bump the sequence number
    lambda d: d.update(seq=6),                                   # or lower it
    lambda d: d["extras"].update({"triggers.json": "0" * 64}),  # swap the trigger digest
    lambda d: d["extras"].update({"evil.json": "1" * 64}),       # add an extra
    lambda d: d.pop("sig2"),                                     # strip the second signature but keep the fields
    lambda d: d.update(sig2="!!!"),
    lambda d: d.update(seq=True),
    lambda d: d.update(extras=[]),
])
def test_tampering_with_seq_or_extras_is_rejected(mutate):
    m = b"schema_version: 1\n"
    doc = trust.make_sig_doc(m, SEED_A, T1, seq=7, extras={"triggers.json": sha(TRIG)})
    mutate(doc)
    with pytest.raises(trust.TrustError):
        trust.verify_manifest_doc(m, doc, [PUB_A])


def test_stripping_seq_extras_and_sig2_downgrades_to_a_valid_legacy_document():
    # still a genuine v1 signature: the endpoint then has no seq and no signed extras (triggers fail closed, see below)
    m = b"schema_version: 1\n"
    doc = trust.make_sig_doc(m, SEED_A, T1, seq=7, extras={"triggers.json": sha(TRIG)})
    for f in ("seq", "extras", "sig2"):
        doc.pop(f)
    v = trust.verify_manifest_doc(m, doc, [PUB_A])
    assert v.seq is None and v.extras == {}


def test_sig2_made_by_another_key_is_rejected():
    m = b"schema_version: 1\n"
    doc = trust.make_sig_doc(m, SEED_A, T1, seq=7, extras={})
    other = trust.make_sig_doc(m, SEED_B, T1, seq=7, extras={})
    doc["sig2"] = other["sig2"]
    with pytest.raises(trust.TrustError, match="tampered"):
        trust.verify_manifest_doc(m, doc, [PUB_A])


def test_extras_validation():
    with pytest.raises(trust.TrustError):
        trust.make_sig_doc(b"m", SEED_A, T1, seq=1, extras={"../x": "a" * 64})
    with pytest.raises(trust.TrustError):
        trust.make_sig_doc(b"m", SEED_A, T1, seq=1, extras={"a": "short"})
    with pytest.raises(trust.TrustError):
        trust.make_sig_doc(b"m", SEED_A, T1, seq=-1)


# --- seq anti-rollback in fetch_manifest -----------------------------------------------------------------------------
def test_higher_and_equal_seq_accepted_lower_refused(registry):
    registry.add_component("wrapper-core", "0.1.0")
    publish(registry, at=T1, seq=10)
    _m, offline = fetch(registry)
    assert not offline and json.loads(Paths().manifest_seq.read_text())[PUB_A[:8]] == 10
    _m, offline = fetch(registry)                                  # same manifest again: equal seq is fine
    assert not offline
    registry.add_component("wrapper-core", "0.2.0")
    publish(registry, at=T2, seq=11)
    _m, offline = fetch(registry)
    assert not offline and json.loads(Paths().manifest_seq.read_text())[PUB_A[:8]] == 11
    # a validly signed manifest with a NEWER signed_at but a LOWER seq (e.g. a stale CI run) is refused
    registry.add_component("wrapper-core", "0.3.0")
    publish(registry, at=T3, seq=5)
    notes = []
    m, offline = fetch(registry, notes=notes)
    assert offline and any("seq 5 is lower" in n for n in notes)
    assert "0.3.0" not in m["components"]["wrapper-core"]["versions"]
    assert json.loads(Paths().manifest_seq.read_text())[PUB_A[:8]] == 11


def test_first_sight_without_a_stored_seq_is_accepted(registry):
    registry.add_component("wrapper-core", "0.1.0")
    publish(registry, at=T1, seq=3)
    assert not Paths().manifest_seq.exists()
    _m, offline = fetch(registry)
    assert not offline


def test_corrupt_seq_state_does_not_lock_the_fleet_out(registry):
    registry.add_component("wrapper-core", "0.1.0")
    publish(registry, at=T1, seq=3)
    p = Paths().ensure()
    p.manifest_seq.write_text("{not json", encoding="utf-8")
    _m, offline = fetch(registry)
    assert not offline
    assert json.loads(p.manifest_seq.read_text())[PUB_A[:8]] == 3  # repaired with the accepted value
    p.manifest_seq.write_text(json.dumps({PUB_A[:8]: "banana"}), encoding="utf-8")
    _m, offline = fetch(registry)
    assert not offline


def test_seq_is_tracked_per_key_so_a_rotated_key_starts_fresh(registry):
    registry.add_component("wrapper-core", "0.1.0")
    publish(registry, seed=SEED_A, at=T1, seq=500)
    fetch(registry, keys=(PUB_A, PUB_B))
    registry.add_component("wrapper-core", "0.2.0")
    publish(registry, seed=SEED_B, at=T2, seq=1)                   # new key, its own counter
    _m, offline = fetch(registry, keys=(PUB_A, PUB_B))
    assert not offline
    marks = json.loads(Paths().manifest_seq.read_text())
    assert marks == {PUB_A[:8]: 500, PUB_B[:8]: 1}


def test_legacy_unsequenced_document_still_accepted_and_leaves_seq_untouched(registry):
    registry.add_component("wrapper-core", "0.1.0")
    publish(registry, at=T1, seq=9)
    fetch(registry)
    registry.add_component("wrapper-core", "0.2.0")
    publish(registry, at=T2, seq=None, triggers=None)             # a v1-only document
    _m, offline = fetch(registry)
    assert not offline and json.loads(Paths().manifest_seq.read_text())[PUB_A[:8]] == 9


# --- triggers.json integrity ---------------------------------------------------------------------------------------------
def _enable_key(monkeypatch, key=PUB_A):
    monkeypatch.setenv("AUTOMAGIX_REGISTRY_PUBKEYS", key)


def test_signed_triggers_are_accepted(registry, monkeypatch):
    _enable_key(monkeypatch)
    registry.add_component("wrapper-core", "0.1.0")
    publish(registry, at=T1, seq=1)
    fetch(registry)
    got = selfheal.pending_triggers(Paths(), str(registry.root), None, "auto-a")
    assert [t["id"] for t in got] == ["t1"]


def test_tampered_triggers_are_ignored(registry, monkeypatch):
    _enable_key(monkeypatch)
    registry.add_component("wrapper-core", "0.1.0")
    publish(registry, at=T1, seq=1)
    fetch(registry)
    evil = json.dumps({"triggers": [{"id": "evil", "automation": "auto-a", "slot": "poll", "params": {"x": 1}}]}).encode()
    (registry.root / "triggers.json").write_bytes(evil)           # the Pages artifact / triggers branch was altered
    assert selfheal.pending_triggers(Paths(), str(registry.root), None, "auto-a") == []


def test_old_signed_triggers_cannot_be_replayed_against_a_newer_manifest(registry, monkeypatch):
    _enable_key(monkeypatch)
    registry.add_component("wrapper-core", "0.1.0")
    publish(registry, at=T1, seq=1)
    old = (registry.root / "triggers.json").read_bytes()
    new = json.dumps({"triggers": [{"id": "t2", "automation": "auto-a", "slot": "poll", "params": {}}]}).encode()
    publish(registry, at=T2, seq=2, triggers=new)
    fetch(registry)
    (registry.root / "triggers.json").write_bytes(old)            # replay the earlier genuine file
    assert selfheal.pending_triggers(Paths(), str(registry.root), None, "auto-a") == []
    (registry.root / "triggers.json").write_bytes(new)
    assert [t["id"] for t in selfheal.pending_triggers(Paths(), str(registry.root), None, "auto-a")] == ["t2"]


def test_triggers_ignored_when_the_signature_has_no_extras(registry, monkeypatch):
    _enable_key(monkeypatch)
    registry.add_component("wrapper-core", "0.1.0")
    publish(registry, at=T1, seq=1, sign_extras=False)             # triggers.json published but not vouched for
    fetch(registry)
    assert selfheal.pending_triggers(Paths(), str(registry.root), None, "auto-a") == []


def test_triggers_ignored_for_a_legacy_v1_signature(registry, monkeypatch):
    _enable_key(monkeypatch)
    registry.add_component("wrapper-core", "0.1.0")
    publish(registry, at=T1, seq=None, sign_extras=False)
    fetch(registry)
    assert selfheal.pending_triggers(Paths(), str(registry.root), None, "auto-a") == []


def test_without_a_pinned_key_triggers_keep_working_with_a_warning(registry, monkeypatch, caplog):
    import logging
    monkeypatch.delenv("AUTOMAGIX_REGISTRY_PUBKEYS", raising=False)
    registry.add_component("wrapper-core", "0.1.0")
    publish(registry, at=T1, seq=1)
    wlog = logging.getLogger("wrapper_core")
    wlog.addHandler(caplog.handler)
    try:
        got = selfheal.pending_triggers(Paths().ensure(), str(registry.root), None, "auto-a")
    finally:
        wlog.removeHandler(caplog.handler)
    assert [t["id"] for t in got] == ["t1"] and "unsigned" in caplog.text


def test_triggers_ignored_when_the_cached_signature_is_gone(registry, monkeypatch):
    _enable_key(monkeypatch)
    registry.add_component("wrapper-core", "0.1.0")
    publish(registry, at=T1, seq=1)
    fetch(registry)
    Paths().manifest_sig_cache.unlink()
    assert selfheal.pending_triggers(Paths(), str(registry.root), None, "auto-a") == []


# ---- trust --pin on an existing install --------------------------------------------------------------------------------------
def test_pin_keys_pins_only_after_the_live_manifest_verifies(tmp_path, monkeypatch):
    import json as _json
    from wrapper_core import ed25519, trust
    from wrapper_core.paths import Paths
    monkeypatch.setenv("AUTOMAGIX_HOME", str(tmp_path))
    monkeypatch.delenv("AUTOMAGIX_REGISTRY_PUBKEYS", raising=False)
    p = Paths().ensure()
    p.platform_json.write_text(_json.dumps({"ring": "broad", "registry_url": "https://reg.example.test/registry/"}), encoding="utf-8")
    seed, pub = ed25519.keypair_from_seed(bytes(range(32)))
    manifest = b"components: {}\nautomations: {}\n"
    doc = trust.make_sig_doc(manifest, seed, "2026-01-01T00:00:00Z", seq=5)
    files = {"https://reg.example.test/registry/manifest.yaml": manifest,
             "https://reg.example.test/registry/manifest.yaml.sig": _json.dumps(doc).encode()}
    fetch = lambda loc: files[loc]
    # wrong key: refused, nothing written
    _, other_pub = ed25519.keypair_from_seed(bytes(range(1, 33)))
    ok, msg = trust.pin_keys([other_pub.hex()], p, fetch)
    assert not ok and "does NOT verify" in msg and "registry_pubkeys" not in _json.loads(p.platform_json.read_text(encoding="utf-8"))
    # right key: pinned, merged, idempotent
    ok, msg = trust.pin_keys([pub.hex()], p, fetch)
    assert ok and _json.loads(p.platform_json.read_text(encoding="utf-8"))["registry_pubkeys"] == [pub.hex()]
    ok, _ = trust.pin_keys([pub.hex()], p, fetch)
    assert ok and _json.loads(p.platform_json.read_text(encoding="utf-8"))["registry_pubkeys"] == [pub.hex()]
    # garbage input and an unreachable registry fail closed
    assert trust.pin_keys(["nothex"], p, fetch)[0] is False
    def boom(loc):
        raise OSError("offline")
    assert trust.pin_keys([pub.hex()], p, boom)[0] is False
