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
from typing import Iterable, List, Optional

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


def make_sig_doc(manifest_bytes: bytes, seed: bytes, signed_at: str) -> dict:
    """Operator side (``tools/sign_registry.py``): sign exact manifest bytes with a 32-byte Ed25519 seed."""
    _, pub = ed25519.keypair_from_seed(seed)
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    sig = ed25519.sign(seed, signing_message(digest, signed_at))
    return {"v": 1, "key_id": pub.hex()[:8], "signed_at": signed_at, "manifest_sha256": digest,
            "sig": base64.b64encode(sig).decode("ascii")}


def verify_manifest(manifest_bytes: bytes, sig_doc, keys: Iterable[str]) -> str:
    """Verify ``manifest_bytes`` against ``sig_doc`` (dict, or JSON text/bytes) and the trusted ``keys`` (hex).

    Returns the ``signed_at`` string on success, raises :class:`TrustError` with a reason otherwise."""
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
            return signed_at
    raise TrustError("signature was not made by a trusted registry key")


def main(argv: List[str]) -> int:
    """``python -m wrapper_core trust --show``: list trusted key ids (never key material beyond the id)."""
    from .paths import Paths, platform_config
    if argv[:1] not in (["--show"], []):
        print("usage: python -m wrapper_core trust [--show]", file=sys.stderr)
        return 2
    keys = configured_keys(platform_config(Paths()))
    if not keys:
        print("no registry signing key pinned: manifests are accepted UNSIGNED (set registry_pubkeys in platform.json)")
        return 0
    print(f"{len(keys)} trusted registry key(s):")
    for k in keys:
        print(f"  key_id {key_id(k)}")
    return 0
