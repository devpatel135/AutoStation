"""Signed registry manifests: the fleet trust root (SPEC §4).

The registry is static files on a web host. TLS only proves who served the bytes, not that the operator approved them,
so ``manifest.yaml`` ships with a detached Ed25519 signature (``manifest.yaml.sig``). Endpoints pin the operator's public
key(s) and refuse any manifest that is unsigned (once a key is configured), mis-signed, or older than the newest one
already accepted (anti-rollback via ``signed_at``).

Key sources, merged in this order (duplicates ignored):
  1. ``PINNED_REGISTRY_KEYS`` below (baked in at build time)
  2. ``registry_pubkeys`` in config/platform.json (list of 64-hex public keys)
  3. env ``AUTOMAGIX_REGISTRY_PUBKEYS`` (comma/space separated hex)

Signature document (JSON)::

    {"v": 1, "key_id": "<first 8 hex of the public key>", "signed_at": "<ISO-8601 UTC>",
     "manifest_sha256": "<hex sha256 of the exact manifest bytes>", "sig": "<base64 Ed25519 signature>"}

Signed message: UTF-8 of ``autostation-manifest-v1\\n<manifest_sha256>\\n<signed_at>``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Iterable, List, NamedTuple, Optional, Tuple

from . import ed25519

PINNED_REGISTRY_KEYS: List[str] = []  # operator public keys (hex); empty = unsigned registries are accepted with a warning

DOMAIN = "autostation-manifest-v1"
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class TrustError(ValueError):
    """The manifest/signature pair cannot be trusted. ``str(exc)`` is the human-readable reason."""


def key_id(pubkey_hex: str) -> str:
    return pubkey_hex.lower()[:8]


def _clean(keys: Iterable[object]) -> List[str]:
    out: List[str] = []
    for k in keys:
        if isinstance(k, str) and _HEX64.match(k.strip()) and k.strip().lower() not in out:
            out.append(k.strip().lower())
    return out


def configured_keys(cfg: Optional[dict] = None) -> List[str]:
    """All trusted public keys (hex) from the pinned list, platform.json ``registry_pubkeys`` and the env override."""
    keys: List[object] = list(PINNED_REGISTRY_KEYS)
    cfg_keys = (cfg or {}).get("registry_pubkeys")
    if isinstance(cfg_keys, str):
        cfg_keys = re.split(r"[,\s]+", cfg_keys)
    if isinstance(cfg_keys, (list, tuple)):
        keys.extend(cfg_keys)
    env = os.environ.get("AUTOMAGIX_REGISTRY_PUBKEYS", "")
    keys.extend(t for t in re.split(r"[,\s]+", env) if t)
    return _clean(keys)


def parse_iso(value: str) -> datetime:
    try:
        d = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise TrustError(f"signed_at is not an ISO-8601 timestamp: {value!r}") from exc
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def signing_message(manifest_sha256: str, signed_at: str) -> bytes:
    return f"{DOMAIN}\n{manifest_sha256.lower()}\n{signed_at}".encode("utf-8")


DOMAIN2 = "autostation-manifest-v2"
_NAME_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_SEQ = 2 ** 53


def signing_message_v2(manifest_sha256: str, signed_at: str, seq: int, extras: dict) -> bytes:
    """Message covered by ``sig2``: the manifest digest, time, monotonic sequence number and the digests of the extra
    published files (``triggers.json``). Extras are listed sorted by name, one ``name=sha256`` per line."""
    lines = "\n".join(f"{n}={extras[n].lower()}" for n in sorted(extras))
    return f"{DOMAIN2}\n{manifest_sha256.lower()}\n{signed_at}\n{int(seq)}\n{lines}".encode("utf-8")


def _check_extras(extras) -> dict:
    if not isinstance(extras, dict) or len(extras) > 16:
        raise TrustError("signature extras must be a small mapping of file name to sha256")
    out = {}
    for n, h in extras.items():
        if not (isinstance(n, str) and _NAME_OK.match(n) and isinstance(h, str) and _HEX64.match(h)):
            raise TrustError("signature extras contain an invalid entry")
        out[n] = h.lower()
    return out


def make_sig_doc(manifest_bytes: bytes, seed: bytes, signed_at: str, seq: Optional[int] = None,
                 extras: Optional[dict] = None) -> dict:
    """Operator side (``tools/sign_registry.py``): sign exact manifest bytes with a 32-byte Ed25519 seed.

    The v1 signature (``sig``) always covers manifest digest + time, so endpoints that predate ``seq``/``extras`` keep working.
    When ``seq`` and/or ``extras`` are given they are covered by a second signature ``sig2`` (same key)."""
    _, pub = ed25519.keypair_from_seed(seed)
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    sig = ed25519.sign(seed, signing_message(digest, signed_at))
    doc = {"v": 1, "key_id": pub.hex()[:8], "signed_at": signed_at, "manifest_sha256": digest,
           "sig": base64.b64encode(sig).decode("ascii")}
    if seq is not None or extras:
        seq = 0 if seq is None else int(seq)
        if not 0 <= seq <= MAX_SEQ:
            raise TrustError("seq out of range")
        ex = _check_extras(extras or {})
        doc["seq"], doc["extras"] = seq, ex
        doc["sig2"] = base64.b64encode(ed25519.sign(seed, signing_message_v2(digest, signed_at, seq, ex))).decode("ascii")
    return doc


class Verified(NamedTuple):
    """Result of a successful verification. ``seq``/``extras`` are None/{} unless the document carried a valid ``sig2``."""
    signed_at: str
    key_id: str
    seq: Optional[int]
    extras: dict


def verify_manifest(manifest_bytes: bytes, sig_doc, keys: Iterable[str]) -> str:
    """Verify ``manifest_bytes`` against ``sig_doc`` (dict, or JSON text/bytes) and the trusted ``keys`` (hex).

    Returns the ``signed_at`` string on success, raises :class:`TrustError` with a reason otherwise."""
    return verify_manifest_doc(manifest_bytes, sig_doc, keys).signed_at


def verify_manifest_doc(manifest_bytes: bytes, sig_doc, keys: Iterable[str]) -> Verified:
    """Like :func:`verify_manifest` but also returns the verified key id, ``seq`` and ``extras``.

    A document with ``seq``/``extras`` but no valid ``sig2`` is rejected (tampered); a document without them is a legacy
    v1 document (seq None, extras {}), which still verifies."""
    trusted = _clean(keys)
    if not trusted:
        raise TrustError("no trusted registry key configured")
    if isinstance(sig_doc, (bytes, bytearray)):
        sig_doc = bytes(sig_doc).decode("utf-8", "replace")
    if isinstance(sig_doc, str):
        try:
            sig_doc = json.loads(sig_doc)
        except ValueError as exc:
            raise TrustError("signature file is not valid JSON") from exc
    if not isinstance(sig_doc, dict) or sig_doc.get("v") != 1:
        raise TrustError("unsupported signature document")
    signed_at, claimed_sha, sig_b64 = sig_doc.get("signed_at"), sig_doc.get("manifest_sha256"), sig_doc.get("sig")
    if not all(isinstance(x, str) for x in (signed_at, claimed_sha, sig_b64)):
        raise TrustError("signature document is missing fields")
    parse_iso(signed_at)
    actual = hashlib.sha256(manifest_bytes).hexdigest()
    if claimed_sha.lower() != actual:
        raise TrustError("manifest does not match the signed sha256 (tampered or stale signature)")
    try:
        sig = base64.b64decode(sig_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TrustError("signature is not valid base64") from exc
    msg = signing_message(actual, signed_at)
    kid = sig_doc.get("key_id")
    candidates = [k for k in trusted if isinstance(kid, str) and key_id(k) == kid.lower()] or trusted
    for k in candidates:
        if ed25519.verify(bytes.fromhex(k), msg, sig):
            return _verify_v2(sig_doc, actual, signed_at, k)
    raise TrustError("signature was not made by a trusted registry key")


def _verify_v2(doc: dict, digest: str, signed_at: str, key_hex: str) -> Verified:
    has_extra = any(f in doc for f in ("seq", "extras", "sig2"))
    if not has_extra:
        return Verified(signed_at, key_id(key_hex), None, {})
    seq, extras, sig2 = doc.get("seq"), doc.get("extras"), doc.get("sig2")
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= MAX_SEQ or not isinstance(sig2, str):
        raise TrustError("signature document has an incomplete seq/extras section")
    ex = _check_extras(extras)
    try:
        raw2 = base64.b64decode(sig2, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TrustError("sig2 is not valid base64") from exc
    if not ed25519.verify(bytes.fromhex(key_hex), signing_message_v2(digest, signed_at, seq, ex), raw2):
        raise TrustError("seq/extras were not signed by the registry key (tampered)")
    return Verified(signed_at, key_id(key_hex), seq, ex)


def pin_keys(candidates: Iterable[str], paths=None, fetch=None) -> Tuple[bool, str]:
    """Pin registry public key(s) into platform.json on an EXISTING install, but only after the live registry's manifest verifies
    against them (a wrong or mistyped key can never lock the machine out). Returns (ok, message)."""
    from . import selfheal
    from .paths import Paths, platform_config
    from .util import read_json, write_json
    paths = paths or Paths()
    new = _clean(candidates)
    if not new:
        return False, "no valid 64-hex Ed25519 public key given"
    cfg = platform_config(paths)
    base = str(cfg.get("registry_url") or "")
    if not base:
        return False, "platform.json has no registry_url"
    fetch = fetch or (lambda loc: selfheal.fetch_bytes(loc))
    try:
        manifest = fetch(selfheal.resolve_location("manifest.yaml", base))
        sig = fetch(selfheal.resolve_location("manifest.yaml.sig", base))
    except Exception as exc:  # noqa: BLE001
        return False, f"could not fetch the signed manifest to verify the key: {type(exc).__name__}: {str(exc)[:120]}"
    try:
        verified = verify_manifest_doc(manifest, sig, new)
    except TrustError as exc:
        return False, f"the live registry manifest does NOT verify against that key ({exc}); nothing was changed"
    raw = read_json(paths.platform_json, {}) or {}
    have = _clean(raw.get("registry_pubkeys") or [])
    merged = have + [k for k in new if k not in have]
    raw["registry_pubkeys"] = merged
    write_json(paths.platform_json, raw)
    return True, f"pinned (verified against the live registry, key id {verified.key_id}); this machine now accepts only a registry manifest signed by {len(merged)} pinned key(s)"


def main(argv: List[str]) -> int:
    """``python -m wrapper_core trust [--show]`` lists trusted key ids; ``trust --pin HEX[,HEX]`` pins a key after verifying the live registry."""
    from .paths import Paths, platform_config
    if argv[:1] == ["--pin"]:
        ok, msg = pin_keys(re.split(r"[,\s]+", " ".join(argv[1:])))
        print(msg)
        return 0 if ok else 1
    if argv[:1] not in (["--show"], []):
        print("usage: python -m wrapper_core trust [--show | --pin HEX[,HEX]]", file=sys.stderr)
        return 2
    keys = configured_keys(platform_config(Paths()))
    if not keys:
        print("no registry signing key pinned: manifests are accepted UNSIGNED (set registry_pubkeys in platform.json, or run: python -m wrapper_core trust --pin <public key>)")
        return 0
    print(f"{len(keys)} trusted registry key(s):")
    for k in keys:
        print(f"  key_id {key_id(k)}")
    return 0
