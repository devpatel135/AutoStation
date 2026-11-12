"""Phase 1 (1.8): key rotation by endorsement + co-signature, epoch/reset recovery, old documents, stale registry."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from wrapper_core import ed25519, selfheal, trust
from wrapper_core.paths import Paths

SEED_A, SEED_B = bytes(range(32)), bytes(range(1, 33))
PUB_A = ed25519.keypair_from_seed(SEED_A)[1].hex()
PUB_B = ed25519.keypair_from_seed(SEED_B)[1].hex()
MANIFEST = b"schema_version: 1\ncomponents: {}\nautomations: {}\n"
WHEN = "2026-05-01T00:00:00Z"


def test_old_documents_still_verify_and_carry_no_v3_fields():
    doc = trust.make_sig_doc(MANIFEST, SEED_A, WHEN, seq=5)
    v = trust.verify_manifest_doc(MANIFEST, doc, [PUB_A])
    assert v.seq == 5 and v.epoch is None and v.trusted_keys == () and "sig3" not in doc


def test_new_document_still_verifies_through_v1_v2_only():
    """An endpoint that predates sig3 only looks at sig/sig2: it must still accept a v3 document."""
    doc = trust.make_sig_doc(MANIFEST, SEED_A, WHEN, seq=5, epoch=2, trusted_keys=[PUB_B], cosign_seeds=[SEED_B])
    legacy = {k: v for k, v in doc.items() if k not in ("epoch", "reset_seq", "trusted_keys", "sig3", "cosigs")}
    assert trust.verify_manifest_doc(MANIFEST, legacy, [PUB_A]).seq == 5


def test_rotation_endorsed_key_is_learned_only_with_cosignature():
    both = trust.make_sig_doc(MANIFEST, SEED_A, WHEN, seq=1, epoch=1, trusted_keys=[PUB_B], cosign_seeds=[SEED_B])
    assert trust.verify_manifest_doc(MANIFEST, both, [PUB_A]).trusted_keys == (PUB_B,)
    only_endorsed = trust.make_sig_doc(MANIFEST, SEED_A, WHEN, seq=1, epoch=1, trusted_keys=[PUB_B])
    assert trust.verify_manifest_doc(MANIFEST, only_endorsed, [PUB_A]).trusted_keys == ()      # no proof of possession: not learned


def test_tampered_v3_fields_are_rejected():
    doc = trust.make_sig_doc(MANIFEST, SEED_A, WHEN, seq=1, epoch=1, trusted_keys=[PUB_B], cosign_seeds=[SEED_B])
    forged = dict(doc, trusted_keys=[PUB_B, "ab" * 32])
    with pytest.raises(trust.TrustError):
        trust.verify_manifest_doc(MANIFEST, forged, [PUB_A])
    forged2 = dict(doc, epoch=99)
    with pytest.raises(trust.TrustError):
        trust.verify_manifest_doc(MANIFEST, forged2, [PUB_A])
    bad_cos = json.loads(json.dumps(doc))
    bad_cos["cosigs"][0]["sig"] = doc["sig"]
    with pytest.raises(trust.TrustError):
        trust.verify_manifest_doc(MANIFEST, bad_cos, [PUB_A])


def test_rotation_end_to_end_new_key_trusted_after_learning(tmp_path):
    p = Paths(tmp_path).ensure()
    cfg = {"registry_pubkeys": [PUB_A]}
    assert trust.effective_keys(p, cfg) == [PUB_A]
    doc = trust.make_sig_doc(MANIFEST, SEED_A, WHEN, seq=1, epoch=1, trusted_keys=[PUB_B], cosign_seeds=[SEED_B])
    v = trust.verify_manifest_doc(MANIFEST, doc, trust.effective_keys(p, cfg))
    assert trust.learn_keys(p, v.trusted_keys) == [PUB_B]
    assert set(trust.effective_keys(p, cfg)) == {PUB_A, PUB_B}
    later = trust.make_sig_doc(MANIFEST, SEED_B, "2026-06-01T00:00:00Z", seq=2)             # signed by the NEW key only
    assert trust.verify_manifest_doc(MANIFEST, later, trust.effective_keys(p, cfg)).key_id == trust.key_id(PUB_B)
    with pytest.raises(trust.TrustError):                                                    # ... but not before it was learned
        trust.verify_manifest_doc(MANIFEST, later, [PUB_A])


def test_learned_keys_never_switch_enforcement_on(tmp_path):
    p = Paths(tmp_path).ensure()
    trust.learn_keys(p, [PUB_B])
    assert trust.effective_keys(p, {}) == []


def _marks(p):
    return json.loads(p.manifest_seq.read_text(encoding="utf-8"))


def test_epoch_reset_lets_a_lower_seq_through_only_with_a_higher_epoch(tmp_path):
    p = Paths(tmp_path).ensure()

    def accept(doc):
        v = trust.verify_manifest_doc(MANIFEST, doc, [PUB_A])
        selfheal._check_seq(p, v)
        selfheal._remember_seq(p, v)
        return v

    accept(trust.make_sig_doc(MANIFEST, SEED_A, WHEN, seq=1000, epoch=1))
    low = trust.make_sig_doc(MANIFEST, SEED_A, WHEN, seq=5)
    with pytest.raises(selfheal._Untrusted):
        accept(low)                                                                  # plain replay of a lower seq: refused
    same_epoch_reset = trust.make_sig_doc(MANIFEST, SEED_A, WHEN, seq=5, epoch=1, reset_seq=True)
    with pytest.raises(selfheal._Untrusted):
        accept(same_epoch_reset)                                                     # reset without a higher epoch: refused
    accept(trust.make_sig_doc(MANIFEST, SEED_A, WHEN, seq=5, epoch=2, reset_seq=True))
    assert _marks(p)[trust.key_id(PUB_A)] == 5 and _marks(p)["_epoch"] == 2
    with pytest.raises(selfheal._Untrusted):
        accept(trust.make_sig_doc(MANIFEST, SEED_A, WHEN, seq=9, epoch=1))           # an older epoch is refused outright
    accept(trust.make_sig_doc(MANIFEST, SEED_A, WHEN, seq=6, epoch=2))               # normal progress continues


def test_stale_registry_age_from_signature_and_mtime(tmp_path):
    p = Paths(tmp_path).ensure()
    assert selfheal.cached_manifest_age_days(p) is None
    p.manifest_cache.write_text("x", encoding="utf-8")
    old = time.time() - 20 * 86400
    os.utime(p.manifest_cache, (old, old))
    assert 19 < selfheal.cached_manifest_age_days(p) < 21
    signed = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p.manifest_sig_cache.write_text(json.dumps({"signed_at": signed}), encoding="utf-8")
    assert 2.9 < selfheal.cached_manifest_age_days(p) < 3.1


def test_offline_with_an_old_cache_is_reported_loudly(tmp_path, monkeypatch):
    p = Paths(tmp_path).ensure()
    p.manifest_cache.write_text("schema_version: 1\ncomponents: {}\nautomations: {}\n", encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(p.manifest_cache, (old, old))
    monkeypatch.setattr(selfheal, "fetch_bytes", lambda *a, **k: (_ for _ in ()).throw(selfheal.HealError("offline")))
    res = selfheal.run(p, {"ring": "dev", "registry_url": "https://registry.invalid/"}, None)
    assert res.offline and res.stale_registry
    assert any(e.startswith("stale registry") for e in res.errors)


def test_signature_is_fetched_before_the_manifest_and_skew_is_retried(tmp_path, monkeypatch):
    p = Paths(tmp_path).ensure()
    old_m, new_m = MANIFEST, MANIFEST + b"# newer\n"
    sig_new = json.dumps(trust.make_sig_doc(new_m, SEED_A, WHEN, seq=2)).encode()
    calls = []

    def fake_fetch(loc, *a, **k):
        calls.append(loc.rsplit("/", 1)[-1])
        if loc.endswith(".sig"):
            return sig_new
        return old_m if calls.count("manifest.yaml") == 1 else new_m      # first manifest fetch is the stale CDN copy
    monkeypatch.setattr(selfheal, "fetch_bytes", fake_fetch)
    monkeypatch.setattr(selfheal.time, "sleep", lambda s: None)
    m, offline = selfheal.fetch_manifest(p, "https://registry.invalid/", None, cfg={"registry_pubkeys": [PUB_A]})
    assert not offline and calls[0] == "manifest.yaml.sig"
    assert calls.count("manifest.yaml") == 2 and calls.count("manifest.yaml.sig") == 2
