#!/usr/bin/env python3
"""Ed25519-sign the published registry manifest (endpoints verify it against pinned/configured public keys).

    python tools/sign_registry.py --gen-key key.txt          # writes the private seed (hex) to key.txt, prints the PUBLIC key
    REGISTRY_SIGNING_SEED=<hex> python tools/sign_registry.py --registry-dir registry-out

The private seed lives only in the GitHub secret REGISTRY_SIGNING_SEED (and your password manager). The public key goes into
`registry_pubkeys` in platform.json or PINNED_REGISTRY_KEYS in wrapper_core/trust.py. Until a key is pinned, endpoints accept
unsigned registries with a warning, so signing can be rolled out safely: sign first, pin second.

The signature document also binds (a) `seq`, a monotonic number endpoints persist per key to refuse a replay of an older release, and
(b) the sha256 of triggers.json (queued runs). Both live in a second signature (`sig2`) so endpoints that predate them still verify.

`seq` is ALWAYS present when signing and is derived from a monotonic timestamp (UTC seconds), never left empty: an unsequenced document
would switch the replay protection off. A caller-supplied SIGN_SEQ / --seq is only honoured when it is on the same scale (>= 1e9, i.e. a
timestamp); a small number such as a CI run number is ignored with a warning, because mixing run numbers into a timestamp series would make
later documents look like a rollback and lock the fleet out. If a previous signature is available (--previous-sig, $SIGN_PREVIOUS_SIG, or an
existing manifest.yaml.sig in the registry dir) the new seq must be higher than the previous one (derived values are bumped to prev+1; an
explicit lower value is refused) unless a recovery epoch reset is requested.

Recovery / rotation (third signature `sig3`, ignored by older endpoints; see RUNBOOKS.md):
    SIGN_EPOCH=<int>             operator-controlled counter, raise it to reset the endpoints' seq floor
    SIGN_RESET_SEQ=1             with a HIGHER SIGN_EPOCH than the fleet has seen: endpoints accept this (lower) seq
    SIGN_TRUSTED_KEYS=<hex,...>  public keys to endorse (key rotation); endpoints learn a key only if it also co-signed
    REGISTRY_COSIGN_SEEDS=<hex,...>  seeds of the endorsed keys (proof of possession), kept offline and supplied only for the rotation run
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import secrets as _secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "components" / "wrapper-core"))

from wrapper_core import ed25519, trust  # noqa: E402

TIMESTAMP_SCALE = 10 ** 9      # a seq below this is a counter (CI run number), not a timestamp


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen-key", metavar="FILE")
    ap.add_argument("--registry-dir")
    ap.add_argument("--seq", type=int, help="explicit seq (must be timestamp-scale); default: derived from the clock")
    ap.add_argument("--previous-sig", help="the previously published manifest.yaml.sig (default: $SIGN_PREVIOUS_SIG or the one in the registry dir)")
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
    epoch, reset = parse_epoch(os.environ.get("SIGN_EPOCH", "")), os.environ.get("SIGN_RESET_SEQ", "").strip().lower() in ("1", "true", "yes")
    if reset and epoch is None:
        raise SystemExit("SIGN_RESET_SEQ needs SIGN_EPOCH (a higher epoch than the fleet has seen)")
    prev = previous_seq(Path(a.previous_sig) if a.previous_sig else (Path(os.environ["SIGN_PREVIOUS_SIG"]) if os.environ.get("SIGN_PREVIOUS_SIG") else reg / "manifest.yaml.sig"))
    seq = choose_seq(os.environ.get("SIGN_SEQ", "") if a.seq is None else str(a.seq), prev, reset)
    doc = trust.make_sig_doc(manifest.read_bytes(), bytes.fromhex(seed_hex), when, seq=seq, extras=collect_extras(reg),
                             epoch=epoch, reset_seq=reset, trusted_keys=parse_keys(os.environ.get("SIGN_TRUSTED_KEYS", "")),
                             cosign_seeds=[bytes.fromhex(x) for x in parse_keys(os.environ.get("REGISTRY_COSIGN_SEEDS", ""))] or None)
    (reg / "manifest.yaml.sig").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"signed {manifest} ({when}), key id {doc.get('key_id', '?')}, seq {doc.get('seq')}, epoch {doc.get('epoch', 'none')}, "
          f"signed extras: {', '.join(sorted(doc.get('extras', {}))) or 'none'}"
          + (f", endorsed keys: {len(doc.get('trusted_keys', []))}" if doc.get("trusted_keys") else ""))
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


def parse_keys(text: str) -> list:
    return [t for t in re.split(r"[,\s]+", (text or "").strip()) if t]


def parse_epoch(text: str):
    text = (text or "").strip()
    if not text:
        return None
    if not text.isdigit() or int(text) > trust.MAX_EPOCH:
        raise SystemExit(f"SIGN_EPOCH must be a non-negative integer, got {text[:20]!r}")
    return int(text)


def previous_seq(sig_file: Path):
    try:
        v = json.loads(sig_file.read_text(encoding="utf-8")).get("seq")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def parse_seq(text: str):
    """An explicit seq, validated: empty -> None (derive one); anything but a non-negative integer is refused."""
    text = (text or "").strip()
    if not text:
        return None
    if not text.isdigit() or int(text) > trust.MAX_SEQ:
        raise SystemExit(f"SIGN_SEQ must be a non-negative integer, got {text[:20]!r}")
    return int(text)


def choose_seq(text: str, prev, reset: bool = False, now=None) -> int:
    """The seq to sign with (see the module docstring): never empty, timestamp-scale, strictly above the previous one."""
    explicit = parse_seq(text)
    if explicit is not None and explicit < TIMESTAMP_SCALE:
        print(f"warning: SIGN_SEQ={explicit} is a counter, not a timestamp; ignored (seq is derived from the clock so the series stays monotonic). "
              "Remove SIGN_SEQ from the workflow.", file=sys.stderr)
        explicit = None
    if explicit is not None:
        if prev is not None and explicit <= prev and not reset:
            raise SystemExit(f"refusing seq {explicit}: not higher than the previous document's {prev} (endpoints would reject it as a rollback)")
        return explicit
    seq = int(time.time() if now is None else now)
    if prev is not None and seq <= prev and not reset:
        seq = prev + 1
    return seq


if __name__ == "__main__":
    sys.exit(main())
