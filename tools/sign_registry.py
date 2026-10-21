#!/usr/bin/env python3
"""Ed25519-sign the published registry manifest (endpoints verify it against pinned/configured public keys).

    python tools/sign_registry.py --gen-key key.txt          # writes the private seed (hex) to key.txt, prints the PUBLIC key
    REGISTRY_SIGNING_SEED=<hex> python tools/sign_registry.py --registry-dir registry-out

The private seed lives only in the GitHub secret REGISTRY_SIGNING_SEED (and your password manager). The public key goes into
`registry_pubkeys` in platform.json or PINNED_REGISTRY_KEYS in wrapper_core/trust.py. Until a key is pinned, endpoints accept
unsigned registries with a warning, so signing can be rolled out safely: sign first, pin second.
"""
from __future__ import annotations

import argparse
import datetime
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
    manifest = Path(a.registry_dir) / "manifest.yaml"
    when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = trust.make_sig_doc(manifest.read_bytes(), bytes.fromhex(seed_hex), when)
    (Path(a.registry_dir) / "manifest.yaml.sig").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"signed {manifest} ({when}), key id {doc.get('key_id', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
