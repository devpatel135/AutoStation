"""Hardened XML / archive reading for files we do not control (UiPath projects, nupkg/zip bundles, xlsx workbooks).

* ``contains_dtd`` looks at the WHOLE document, decoded every way an XML parser could decode it (UTF-8, UTF-16/32 with or without
  a BOM), so padding the file with comments / whitespace, or switching encoding, cannot hide a ``<!DOCTYPE`` / ``<!ENTITY``.
* ``read_member`` / ``extract_zip`` enforce member-count and total-size caps with a *streaming* byte count (the sizes a zip
  header declares are attacker-controlled and are not trusted).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

_MARKERS = (b"<!doctype", b"<!entity", b"<!element", b"<!attlist", b"<!notation")

MAX_MEMBERS = 10_000
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_MEMBER_BYTES = 100 * 1024 * 1024
MAX_XML_MEMBER_BYTES = 50 * 1024 * 1024
_CHUNK = 1 << 16


class DtdNotAllowed(ValueError):
    pass


class ArchiveLimitError(ValueError):
    pass


def contains_dtd(raw: bytes) -> bool:
    """True if any DTD marker appears anywhere in ``raw``. The NUL-stripped view catches UTF-16 / UTF-32 (either endianness,
    BOM or not): every marker character is ASCII, so it is the same byte sequence once the zero bytes are gone."""
    raw = bytes(raw)
    low = raw.lower()
    if any(m in low for m in _MARKERS):
        return True
    if b"\x00" in raw:
        stripped = raw.replace(b"\x00", b"").lower()
        return any(m in stripped for m in _MARKERS)
    return False


def safe_xml_root(raw: bytes):
    if contains_dtd(raw):
        raise DtdNotAllowed("DTD/entity declarations are not allowed")
    return ET.fromstring(raw)


def read_member(z: zipfile.ZipFile, name: str, cap: int = MAX_XML_MEMBER_BYTES) -> bytes:
    """Read one zip member, never holding more than ``cap`` bytes (declared size is checked, then the stream is counted)."""
    info = z.getinfo(name)
    if info.file_size > cap:
        raise ArchiveLimitError(f"{name}: {info.file_size} bytes exceeds the {cap} byte limit")
    out, total = bytearray(), 0
    with z.open(info) as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise ArchiveLimitError(f"{name}: expands beyond the {cap} byte limit")
            out += chunk
    return bytes(out)


def extract_zip(archive: Path, dest: Path, max_members: int | None = None, max_total: int | None = None,
                max_member: int | None = None) -> int:
    """Extract ``archive`` into ``dest`` (path-traversal checked, member count and total/uncompressed size capped while streaming).

    Returns the number of bytes written. Raises ``ValueError`` (``ArchiveLimitError``) on any violation. Limits default to the
    module constants (read at call time)."""
    max_members = MAX_MEMBERS if max_members is None else max_members
    max_total = MAX_TOTAL_BYTES if max_total is None else max_total
    max_member = MAX_MEMBER_BYTES if max_member is None else max_member
    dest = Path(dest)
    root = dest.resolve()
    written = 0
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        if len(infos) > max_members:
            raise ArchiveLimitError(f"{Path(archive).name}: {len(infos)} members exceeds the limit of {max_members}")
        if sum(i.file_size for i in infos) > max_total:
            raise ArchiveLimitError(f"{Path(archive).name}: declared uncompressed size exceeds {max_total} bytes")
        for info in infos:
            target = (dest / info.filename).resolve()
            if root != target and root not in target.parents:
                raise ValueError(f"unsafe path in archive {Path(archive).name}: {info.filename}")
        for info in infos:
            if info.is_dir():
                (dest / info.filename).mkdir(parents=True, exist_ok=True)
                continue
            target = dest / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            member = 0
            with z.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(_CHUNK)
                    if not chunk:
                        break
                    member += len(chunk)
                    written += len(chunk)
                    if member > max_member or written > max_total:
                        out.close()
                        raise ArchiveLimitError(f"{Path(archive).name}: {info.filename} expands beyond the size limit")
                    out.write(chunk)
    return written
