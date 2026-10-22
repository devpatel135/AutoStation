#!/usr/bin/env python3
"""Ed25519-sign the published registry manifest (endpoints verify it against pinned/configured public keys).

    python tools/sign_registry.py --gen-key key.txt          # writes the private seed (hex) to key.txt, prints the PUBLIC key
    REGISTRY_SIGNING_SEED=<hex> SIGN_SEQ=<run number> python tools/sign_registry.py --registry-dir registry-out

The private seed lives only in the GitHub secret REGISTRY_SIGNING_SEED (and your password manager). The public key goes into
`registry_pubkeys` in platform.json or PINNED_REGISTRY_KEYS in wrapper_core/trust.py. Until a key is pinned, endpoints accept
unsigned registries with a warning, so signing can be rolled out safely: sign first, pin second.

The signature document also binds (a) `seq`, a monotonic number endpoints persist per key to refuse a replay of an older release, and
(b) the sha256 of triggers.json (queued runs). Both live in a second signature (`sig2`) so endpoints that predate them still verify.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import secrets as _secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "components" / "wrapper-core"))

from wrapper_core import ed25519, trust  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen-key", metavar="FILE")
    ap.add_argument("--registry-dir")
    ap.add_argument("--seq", type=int, help="monotonic sequence number (default: $SIGN_SEQ; CI passes github.run_number)")
    a = ap.parse_args(argv)
    if a.gen_key:
        seed, pub = ed25519.keypair_from_seed(_secrets.token_bytes(32))
        Path(a.gen_key).write_text(seed.hex() + "\n", encoding="ascii")
        print(f"private seed written to {a.gen_key} (keep it secret)\npublic key: {pub.hex()}\nkey id: {trust.key_id(pub.hex())}")
        return 0
    if not a.registry_dir:
        ap.error("--registry-dir or --gen-key required")
    seed_hex = os.environ.get("REGISTRY_SIGNING_SEED", "").strip()
    if not seed_hex:
        print("REGISTRY_SIGNING_SEED not set: leaving the registry unsigned")
        return 0
    reg = Path(a.registry_dir)
    manifest = reg / "manifest.yaml"
    when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    seq = parse_seq(os.environ.get("SIGN_SEQ", "") if a.seq is None else str(a.seq))
    doc = trust.make_sig_doc(manifest.read_bytes(), bytes.fromhex(seed_hex), when, seq=seq, extras=collect_extras(reg))
    (reg / "manifest.yaml.sig").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"signed {manifest} ({when}), key id {doc.get('key_id', '?')}, seq {doc.get('seq', 'none')}, "
          f"signed extras: {', '.join(sorted(doc.get('extras', {}))) or 'none'}")
    return 0


# extra published files whose sha256 is bound into the signature (endpoints refuse them if the bytes differ)
SIGNED_EXTRAS = ("triggers.json",)


def collect_extras(reg: Path) -> dict:
    out = {}
    for name in SIGNED_EXTRAS:
        f = reg / name
        if f.is_file():
            out[name] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def parse_seq(text: str):
    """The monotonic sequence number (CI passes github.run_number as SIGN_SEQ). Empty -> unsequenced (legacy) document."""
    text = (text or "").strip()
    if not text:
        return None
    if not text.isdigit() or int(text) > trust.MAX_SEQ:
        raise SystemExit(f"SIGN_SEQ must be a non-negative integer, got {text[:20]!r}")
    return int(text)


if __name__ == "__main__":
    sys.exit(main())
