"""Ingest: move endpoint artefacts into the date-partitioned archive (SPEC §7).

Archive layout: ``<archive>/<kind>/<name>/YYYY/MM/DD/<id>.json`` (the ring is stored inside the JSON as ``ring``).

* ``ingest_dir``: reads what wrapper-core's ``DirTransport`` writes (``<dir>/ingest/<ring>/<kind>/<name>/<id>.json``).
* ``ingest_git``: fetches ``refs/heads/ingest/*`` from the logging repo (the write-only endpoint key can only create such
  refs), archives each artefact, and only then deletes the processed remote refs.

Both are idempotent: an artefact already archived is not rewritten, and its source is still consumed. Anything that
fails validation is quarantined under ``<archive>/_rejected/`` instead of being silently dropped.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .common import KINDS, iso, parse_iso, utcnow

_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class IngestError(RuntimeError):
    pass


def artefact_time(rec: dict):
    raw = rec.get("started_at") if rec.get("kind") == "run" else rec.get("ts")
    return parse_iso(raw) if raw else utcnow()


def archive_path(archive: Path, kind: str, name: str, art_id: str, rec: dict) -> Path:
    d = artefact_time(rec)
    return Path(archive) / kind / name / d.strftime("%Y") / d.strftime("%m") / d.strftime("%d") / f"{art_id}.json"


def _validate(rec, kind: str, name: str, art_id: str) -> str | None:
    """Return a rejection reason or None."""
    if not isinstance(rec, dict):
        return "not a JSON object"
    if rec.get("schema_version") != 1:
        return f"unsupported schema_version {rec.get('schema_version')!r}"
    if rec.get("kind") != kind:
        return f"kind mismatch: path says {kind!r}, content says {rec.get('kind')!r}"
    if kind not in KINDS:
        return f"unknown kind {kind!r}"
    for label, val in (("name", name), ("id", art_id)):
        if not _SAFE.match(val):
            return f"unsafe {label} {val!r}"
    if kind == "run" and (not rec.get("automation") or not rec.get("run_id") or not rec.get("started_at")):
        return "run artefact missing automation/run_id/started_at"
    if kind in ("heartbeat", "lifecycle") and not rec.get("ts"):
        return f"{kind} artefact missing ts"
    try:
        artefact_time(rec)
    except ValueError:
        return "unparseable timestamp"
    return None


def _reject(archive: Path, ring: str, kind: str, name: str, art_id: str, raw: bytes, reason: str, stats: dict) -> None:
    rej = Path(archive) / "_rejected" / f"{ring}-{kind}-{name}-{art_id}.json.txt"
    rej.parent.mkdir(parents=True, exist_ok=True)
    rej.write_bytes(raw[:200_000])
    rej.with_suffix(".reason").write_text(reason + chr(10), encoding="utf-8")
    stats["rejected"] += 1


def _store(archive: Path, ring: str, kind: str, name: str, art_id: str, raw: bytes, stats: dict, source_label: str) -> None:
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        rec = None
    reason = _validate(rec, kind, name, art_id) if rec is not None else "invalid JSON"
    if reason:
        rej = Path(archive) / "_rejected" / f"{ring}-{kind}-{name}-{art_id}.json.txt"
        rej.parent.mkdir(parents=True, exist_ok=True)
        rej.write_bytes(raw[:200_000])
        (rej.with_suffix(".reason")).write_text(f"{source_label}: {reason}\n", encoding="utf-8")
        stats["rejected"] += 1
        return
    rec.setdefault("ring", ring)
    target = archive_path(archive, kind, name, art_id, rec)
    if target.exists():
        stats["duplicates"] += 1
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
    tmp.replace(target)
    stats["archived"] += 1
    stats["by_kind"][kind] = stats["by_kind"].get(kind, 0) + 1


def _stats() -> dict:
    return {"archived": 0, "duplicates": 0, "rejected": 0, "by_kind": {}, "deleted_refs": 0, "errors": []}


def ingest_dir(src: str | Path, archive: str | Path, keep_source: bool = False) -> dict:
    src, archive = Path(src), Path(archive)
    stats = _stats()
    root = src / "ingest"
    if not root.exists():
        return stats
    for f in sorted(root.glob("*/*/*/*.json")):
        ring, kind, name = f.relative_to(root).parts[:3]
        art_id = f.stem
        _store(archive, ring, kind, name, art_id, f.read_bytes(), stats, str(f))
        if not keep_source:
            try:
                f.unlink()
            except OSError as exc:
                stats["errors"].append(f"cannot remove {f}: {exc}")
    return stats


# --------------------------------------------------------------------------------------------- git mode
def _git(args: list[str], cwd: Path, env: dict, timeout: int = 300) -> subprocess.CompletedProcess:
    git = shutil.which("git")
    if not git:
        raise IngestError("git not found on PATH")
    return subprocess.run([git] + args, cwd=str(cwd), env=env, capture_output=True, timeout=timeout)


def _git_env(remote: str, token: str | None) -> tuple[list[str], dict]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    prefix: list[str] = []
    if token and remote.startswith(("http://", "https://")):
        prefix = ["-c", f"http.extraHeader=PRIVATE-TOKEN: {token}"]
    return prefix, env


def ingest_git(remote: str, archive: str | Path, delete: bool = True, token: str | None = None) -> dict:
    """Fetch ingest/* refs from ``remote``, archive their artefacts, then delete the processed refs (batched)."""
    archive = Path(archive)
    token = token or os.environ.get("GITLAB_READ_TOKEN")
    stats = _stats()
    prefix, env = _git_env(remote, token)
    with tempfile.TemporaryDirectory(prefix="amx-ingest-") as tmp:
        work = Path(tmp)
        _git(["init", "-q"], work, env)
        r = _git(prefix + ["fetch", "-q", "--no-tags", remote, "+refs/heads/ingest/*:refs/ingest/*"], work, env)
        if r.returncode != 0:
            msg = r.stderr.decode("utf-8", "replace").strip()
            if "couldn't find remote ref" in msg:
                return stats  # nothing to ingest
            raise IngestError(f"git fetch failed: {msg[:300]}")
        refs = _git(["for-each-ref", "--format=%(refname)", "refs/ingest/"], work, env).stdout.decode().split()
        to_delete: list[str] = []
        for ref in refs:
            parts = ref[len("refs/ingest/"):].split("/")
            if len(parts) != 4:
                stats["errors"].append(f"unexpected ref shape {ref}")
                continue
            ring, kind, name, art_id = parts
            show = _git(["show", f"{ref}:{kind}/{name}/{art_id}.json"], work, env)
            if show.returncode != 0:
                # strict: the commit must hold exactly <kind>/<name>/<id>.json. Quarantine whatever it holds, then drop the ref.
                ls = _git(["ls-tree", "-r", "--name-only", ref], work, env).stdout.decode().split()
                raw = _git(["show", f"{ref}:{ls[0]}"], work, env).stdout if ls else b""
                _reject(archive, ring, kind, name, art_id, raw, f"{ref}: unexpected layout {ls[:3]}", stats)
            else:
                _store(archive, ring, kind, name, art_id, show.stdout, stats, ref)
            to_delete.append("refs/heads/" + ref[len("refs/"):])
        if delete and to_delete:
            # delete only after everything above is safely on disk; batches keep the command line short
            for i in range(0, len(to_delete), 50):
                batch = to_delete[i:i + 50]
                d = _git(prefix + ["push", "-q", remote] + [f":{b}" for b in batch], work, env)
                if d.returncode != 0:
                    stats["errors"].append("ref delete failed: " + d.stderr.decode("utf-8", "replace").strip()[:200])
                else:
                    stats["deleted_refs"] += len(batch)
    return stats


def ingest(source: str, archive: str | Path, remote: str | None = None, src_dir: str | Path | None = None, **kw) -> dict:
    if source == "git":
        if not remote:
            raise IngestError("--remote is required for --source git")
        return ingest_git(remote, archive, **kw)
    if source == "dir":
        if not src_dir:
            raise IngestError("--from is required for --source dir")
        return ingest_dir(src_dir, archive)
    raise IngestError(f"unknown source {source!r}")


def prune(archive: str | Path, days: int, now=None) -> dict:
    """Retention: remove ``<archive>/<kind>/<name>/YYYY/MM/DD`` folders older than ``days`` (and empty parents)."""
    from datetime import datetime, timedelta
    archive = Path(archive)
    cutoff = ((now or utcnow()) - timedelta(days=days)).date()
    removed = 0
    for kind in KINDS:
        for day_dir in list((archive / kind).glob("*/*/*/*")) if (archive / kind).exists() else []:
            if not day_dir.is_dir():
                continue
            y, m, d = day_dir.parent.parent.name, day_dir.parent.name, day_dir.name
            try:
                if datetime(int(y), int(m), int(d)).date() >= cutoff:
                    continue
            except ValueError:
                continue
            shutil.rmtree(day_dir, ignore_errors=True)
            removed += 1
            for parent in (day_dir.parent, day_dir.parent.parent, day_dir.parent.parent.parent):
                try:
                    parent.rmdir()  # only succeeds when empty
                except OSError:
                    break
    return {"removed_day_folders": removed, "cutoff": cutoff.isoformat()}
