"""Secret handling: Windows DPAPI (CurrentUser) and Credential Manager, via ctypes (no extra packages).

* ``dpapi_protect`` / ``dpapi_unprotect`` use CryptProtectData with fixed entropy ``AUTOMAGIX-v1`` and are
  byte-compatible with ``[Security.Cryptography.ProtectedData]::Protect(..., 'CurrentUser')`` used by the
  installer's ``protect-secret.ps1``.
* ``credman_read`` / ``credman_write`` wrap CredReadW / CredWriteW (generic credentials).
* Off Windows (CI, tests) the same API works only when ``AUTOMAGIX_ALLOW_INSECURE_SECRETS=1`` (obfuscation
  only, clearly prefixed ``DEVONLY:``) so that nothing silently pretends to be encrypted.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from .paths import Paths

ENTROPY = b"AUTOMAGIX-v1"
IS_WINDOWS = sys.platform == "win32"
_DEV_PREFIX = b"DEVONLY:"


class SecretError(RuntimeError):
    pass


def _allow_insecure() -> bool:
    return os.environ.get("AUTOMAGIX_ALLOW_INSECURE_SECRETS") == "1"


# --- DPAPI -------------------------------------------------------------------------------------------
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(data: bytes) -> tuple["_BLOB", object]:
        buf = ctypes.create_string_buffer(data, len(data))
        return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _from_blob(blob: "_BLOB") -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob.pbData)

    def dpapi_protect(data: bytes) -> bytes:
        inb, _k1 = _to_blob(data)
        ent, _k2 = _to_blob(ENTROPY)
        out = _BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(inb), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out)):
            raise SecretError(f"CryptProtectData failed ({ctypes.GetLastError()})")
        return _from_blob(out)

    def dpapi_unprotect(blob_bytes: bytes) -> bytes:
        inb, _k1 = _to_blob(blob_bytes)
        ent, _k2 = _to_blob(ENTROPY)
        out = _BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(inb), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out)):
            raise SecretError(f"CryptUnprotectData failed ({ctypes.GetLastError()}); blob is bound to the installing user/machine")
        return _from_blob(out)
else:
    def dpapi_protect(data: bytes) -> bytes:
        if not _allow_insecure():
            raise SecretError("DPAPI is only available on Windows (set AUTOMAGIX_ALLOW_INSECURE_SECRETS=1 for dev/CI)")
        return _DEV_PREFIX + base64.b64encode(data)

    def dpapi_unprotect(blob_bytes: bytes) -> bytes:
        if not _allow_insecure():
            raise SecretError("DPAPI is only available on Windows (set AUTOMAGIX_ALLOW_INSECURE_SECRETS=1 for dev/CI)")
        if not blob_bytes.startswith(_DEV_PREFIX):
            raise SecretError("blob was not produced by the dev protector")
        return base64.b64decode(blob_bytes[len(_DEV_PREFIX):])


def _dev_roundtrip_ok(blob: bytes) -> bool:
    return blob.startswith(_DEV_PREFIX)


def protect_file(src: Path, dst: Path, delete_input: bool = False) -> None:
    """Encrypt `src` to `dst` (DPAPI user scope) with an owner-only ACL; optionally wipe+delete the plaintext."""
    data = Path(src).read_bytes()
    blob = dpapi_protect(data)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(blob)
    restrict_to_owner(dst)
    if delete_input:
        wipe_file(Path(src))


def unprotect_file(path: Path) -> bytes:
    return dpapi_unprotect(Path(path).read_bytes())


def wipe_file(path: Path) -> None:
    """Best-effort overwrite then delete (SSDs make true wiping impossible; this limits casual recovery)."""
    try:
        size = path.stat().st_size
        with open(path, "r+b") as f:
            f.write(b"\0" * size)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass


def restrict_to_owner(path: Path) -> None:
    """Owner-only ACL (icacls on Windows, chmod 600 elsewhere). Failure is non-fatal but logged by callers."""
    try:
        if IS_WINDOWS:
            import subprocess
            user = os.environ.get("USERNAME", "")
            if user:
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                               capture_output=True, timeout=20, check=False)
                # hardened hosts add explicit SYSTEM / Administrators / OWNER RIGHTS / Users / Everyone entries: owner-only means owner-only
                subprocess.run(["icacls", str(path), "/remove:g", "*S-1-5-18", "*S-1-5-32-544", "*S-1-3-4", "*S-1-5-32-545",
                                "*S-1-1-0", "*S-1-5-11"], capture_output=True, timeout=20, check=False)
        else:
            os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        pass


# --- Credential Manager ------------------------------------------------------------------------------
if IS_WINDOWS:
    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
        ]

    def credman_read(target: str) -> str | None:
        pcred = ctypes.POINTER(_CREDENTIAL)()
        if not ctypes.windll.advapi32.CredReadW(target, 1, 0, ctypes.byref(pcred)):
            return None
        try:
            cred = pcred.contents
            raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return raw.decode("utf-16-le")
        finally:
            ctypes.windll.advapi32.CredFree(pcred)

    def credman_write(target: str, secret: str, username: str = "automagix") -> None:
        blob = secret.encode("utf-16-le")
        buf = ctypes.create_string_buffer(blob, len(blob))
        cred = _CREDENTIAL()
        cred.Type = 1  # CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = username
        if not ctypes.windll.advapi32.CredWriteW(ctypes.byref(cred), 0):
            raise SecretError(f"CredWriteW failed ({ctypes.GetLastError()})")

    def credman_delete(target: str) -> bool:
        return bool(ctypes.windll.advapi32.CredDeleteW(target, 1, 0))
else:
    def _env_key(target: str) -> str:
        return "AUTOMAGIX_CRED_" + "".join(c if c.isalnum() else "_" for c in target.upper())

    def credman_read(target: str) -> str | None:
        return os.environ.get(_env_key(target))

    def credman_write(target: str, secret: str, username: str = "automagix") -> None:
        raise SecretError("Credential Manager is only available on Windows")

    def credman_delete(target: str) -> bool:
        return False


# --- named secrets used by wrapper-core --------------------------------------------------------------
def get_registry_token(paths: Paths | None = None) -> str | None:
    """Self-heal read token: env (dev/CI) → Credential Manager → DPAPI file."""
    tok = os.environ.get("AUTOMAGIX_REGISTRY_TOKEN") or credman_read("AUTOMAGIX/registry_token")
    if tok:
        return tok.strip()
    paths = paths or Paths()
    f = paths.secrets / "registry_token.dpapi"
    if f.exists():
        try:
            return unprotect_file(f).decode("utf-8").strip()
        except SecretError:
            return None
    return None


def get_llm_key() -> str | None:
    key = os.environ.get("AUTOSTATION_LLM_KEY") or credman_read("AUTOMAGIX/llm_key")
    return key.strip() if key else None


def logging_key_blob(paths: Paths | None = None) -> Path | None:
    paths = paths or Paths()
    f = paths.secrets / "logging_key.dpapi"
    return f if f.exists() else None


class TempPrivateKey:
    """Context manager that materialises the logging SSH key for the few seconds `git push` needs it.

    OpenSSH cannot read a key from memory on Windows, so the decrypted key is written to a per-process temp file
    in the user's private ``secrets`` dir with an owner-only ACL and is wiped in ``finally``. (The assessment's
    "never on disk" goal is not achievable with stock OpenSSH; this bounds the exposure window to one push.)
    """

    def __init__(self, blob: Path, workdir: Path):
        self.blob, self.workdir, self.path = blob, workdir, None

    def __enter__(self) -> Path:
        key = unprotect_file(self.blob)
        if not key.endswith(b"\n"):
            key += b"\n"  # OpenSSH rejects keys without a trailing newline
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.path = self.workdir / f".k{os.getpid()}-{os.urandom(4).hex()}"
        self.path.write_bytes(b"")
        restrict_to_owner(self.path)  # ACL first, then write
        self.path.write_bytes(key)
        return self.path

    def __exit__(self, *exc) -> None:
        if self.path:
            wipe_file(self.path)


def main(argv: list[str]) -> int:
    import argparse
    import getpass

    p = argparse.ArgumentParser(prog="wrapper_core secrets")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("protect", help="DPAPI-encrypt a file")
    a.add_argument("--in", dest="src", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--delete-input", action="store_true")
    c = sub.add_parser("credman-set", help="store a secret in Windows Credential Manager (read from stdin/prompt)")
    c.add_argument("target")
    c.add_argument("--stdin", action="store_true")
    d = sub.add_parser("credman-delete")
    d.add_argument("target")
    args = p.parse_args(argv)
    if args.cmd == "protect":
        protect_file(Path(args.src), Path(args.out), args.delete_input)
        print(f"protected -> {args.out}")
    elif args.cmd == "credman-set":
        secret = sys.stdin.readline().strip() if args.stdin else getpass.getpass("secret: ")
        credman_write(args.target, secret)
        print(f"stored {args.target}")
    elif args.cmd == "credman-delete":
        print("deleted" if credman_delete(args.target) else "not found")
    return 0
