"""Pure-Python Ed25519 (RFC 8032 §5.1), stdlib only.

Used to verify signed registry manifests on endpoints that ship no cryptography package. Not constant-time: signing
happens on the operator's workstation with a key that never leaves it, and verification handles only public data.

    keypair_from_seed(seed32) -> (secret_seed, public_key)     # both 32 bytes
    sign(seed32, message)     -> 64-byte signature
    verify(public_key, message, signature) -> bool             # never raises on malformed input

CLI (operator side):  python -m wrapper_core.ed25519 keygen   # prints a fresh seed and public key as hex
"""
from __future__ import annotations

import hashlib
import os
import sys

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)  # sqrt(-1)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _recover_x(y: int, sign: int):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)  # extended coordinates (X, Y, Z, T)
_ZERO = (0, 1, 1, 0)


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, p):
    q = _ZERO
    while s > 0:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _equal(p, q) -> bool:
    return (p[0] * q[2] - q[0] * p[2]) % _P == 0 and (p[1] * q[2] - q[1] * p[2]) % _P == 0


def _compress(p) -> bytes:
    zi = pow(p[2], _P - 2, _P)
    x, y = p[0] * zi % _P, p[1] * zi % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _expand(seed: bytes):
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    h = _sha512(bytes(seed))
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def keypair_from_seed(seed: bytes):
    """Return (seed, public_key) for a 32-byte secret seed."""
    a, _ = _expand(seed)
    return bytes(seed), _compress(_mul(a, _G))


def sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, _G))
    r = int.from_bytes(_sha512(prefix + message), "little") % _L
    big_r = _compress(_mul(r, _G))
    k = int.from_bytes(_sha512(big_r + pub + message), "little") % _L
    s = (r + k * a) % _L
    return big_r + int.to_bytes(s, 32, "little")


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True only for a valid signature; any malformed input returns False."""
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        a_pt = _decompress(bytes(public_key))
        r_pt = _decompress(bytes(signature[:32]))
        if a_pt is None or r_pt is None:
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= _L:
            return False
        k = int.from_bytes(_sha512(bytes(signature[:32]) + bytes(public_key) + message), "little") % _L
        return _equal(_mul(s, _G), _add(r_pt, _mul(k, a_pt)))
    except (TypeError, ValueError):
        return False


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] != ["keygen"]:
        print("usage: python -m wrapper_core.ed25519 keygen", file=sys.stderr)
        return 2
    seed, pub = keypair_from_seed(os.urandom(32))
    print(f"seed (SECRET - keep offline, never commit): {seed.hex()}")
    print(f"public key (pin this in the fleet):         {pub.hex()}")
    print(f"key_id:                                     {pub.hex()[:8]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
