"""Failure evidence (UiPath's screenshot-on-error): small files a payload leaves in ``<run_dir>/evidence`` are shipped
with the run as artefact kind ``evidence`` (SPEC addendum), so an operator can see *what the screen/page looked like*.

Caps keep a stolen write-only key and a runaway payload harmless: <=8 files, <=1.5 MB total, allow-listed types only.
Stored evidence is data: dashboards must never render it as HTML (the site shows escaped text / downloads).
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from . import runctx
from .util import hostname, iso

MAX_FILES = 8
MAX_TOTAL = 1_500_000
TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".html": "text/html", ".htm": "text/html",
         ".txt": "text/plain", ".json": "application/json", ".zip": "application/zip"}
FAILURE_STATUSES = {"failed", "hung", "partial", "self_heal_failed"}
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_name(name: str) -> str:
    base = _SAFE.sub("_", Path(name).name)[:80].lstrip(".") or "evidence"
    return base


def add_evidence(name: str, data, content_type: str | None = None) -> Path | None:
    """Attach evidence to the current run. ``data`` is bytes or str. Returns the written path, or None if dropped."""
    ctx = runctx.current()
    folder = ctx.run_dir / "evidence"
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_name(name)
    ext = Path(name).suffix.lower()
    if ext not in TYPES:
        ctx.append("evidence_dropped", {"name": name, "reason": f"type {ext or '(none)'} not allowed"})
        return None
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    existing = [p for p in folder.iterdir() if p.is_file()]
    if len(existing) >= MAX_FILES or sum(p.stat().st_size for p in existing) + len(raw) > MAX_TOTAL:
        ctx.append("evidence_dropped", {"name": name, "reason": "evidence cap reached"})
        return None
    target = folder / name
    target.write_bytes(raw)
    return target


def collect(run_dir: Path, run_id: str, automation: str, extra_text: dict[str, str] | None = None) -> dict | None:
    """Build the ``evidence`` artefact payload from ``run_dir/evidence`` (+ optional generated text files). None if empty."""
    folder = Path(run_dir) / "evidence"
    files: list[tuple[str, bytes]] = []
    if folder.is_dir():
        for p in sorted(folder.iterdir()):
            if p.is_file() and p.suffix.lower() in TYPES:
                files.append((p.name, p.read_bytes()))
    for name, text in (extra_text or {}).items():
        files.append((safe_name(name), text.encode("utf-8")))
    out, total = [], 0
    for name, raw in files[:MAX_FILES]:
        if total + len(raw) > MAX_TOTAL:
            continue
        total += len(raw)
        out.append({"name": name, "content_type": TYPES[Path(name).suffix.lower()], "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(), "data_b64": base64.b64encode(raw).decode("ascii")})
    if not out:
        return None
    return {"schema_version": 1, "kind": "evidence", "run_id": run_id, "automation": automation, "host": hostname(), "ts": iso(),
            "files": out}
