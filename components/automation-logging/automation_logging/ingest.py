"""Ingest: move endpoint artefacts into the date-partitioned archive (SPEC §7).

Archive layout: ``<archive>/<kind>/<name>/YYYY/MM/DD/<id>.json`` (the ring is stored inside the JSON as ``ring``).

* ``ingest_dir``: reads what wrapper-core's ``DirTransport`` writes (``<dir>/ingest/<ring>/<kind>/<name>/<id>.json``).
* ``ingest_git``: fetches ``refs/heads/ingest/*`` from the logging repo (the write-only endpoint key can only create such
  refs), archives each artefact, and only then deletes the processed remote refs.

Both are idempotent: an artefact already archived is not rewritten, and its source is still consumed. Anything that
fails validation is quarantined under ``<archive>/_rejected/`` instead of being silently dropped.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .common import KINDS, iso, parse_iso, utcnow
from .validate import MAX_BYTES, check_name_matches_record, check_path_parts, ref_digest, validate_record


class IngestError(RuntimeError):
    pass


# ---- evidence artefacts (screenshots / DOM snapshots / traces captured on failure) -------------------------------------
EVIDENCE_MAX_BYTES = 2 * 1024 * 1024
EVIDENCE_MAX_FILES = 12
EVIDENCE_TYPES = {"image/png", "image/jpeg", "text/html", "text/plain", "application/zip", "application/json"}
_EVIDENCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_MAGIC = {"image/png": (bytes([0x89]) + b"PNG" + bytes([0x0d, 0x0a, 0x1a, 0x0a]),), "image/jpeg": (bytes([0xff, 0xd8, 0xff]),), "application/zip": (b"PK" + bytes([3, 4]), b"PK" + bytes([5, 6]))}


def validate_evidence(rec: dict) -> str | None:
    """Strict checks on an evidence artefact. Payloads are data: never executed, never rendered as HTML."""
    if not rec.get("run_id") or not rec.get("automation"):
        return "evidence artefact missing run_id/automation"
    files = rec.get("files")
    if not isinstance(files, list) or not files:
        return "evidence artefact has no files"
    if len(files) > EVIDENCE_MAX_FILES:
        return f"too many evidence files ({len(files)} > {EVIDENCE_MAX_FILES})"
    total = 0
    for f in files:
        if not isinstance(f, dict):
            return "evidence file entry is not an object"
        name, ctype = f.get("name"), f.get("content_type")
        if not isinstance(name, str) or not _EVIDENCE_NAME.match(name) or ".." in name:
            return f"unsafe evidence file name {name!r}"
        if ctype not in EVIDENCE_TYPES:
            return f"content type {ctype!r} is not allowed (allowed: {sorted(EVIDENCE_TYPES)})"
        try:
            blob = base64.b64decode(f.get("data_b64") or "", validate=True)
        except (binascii.Error, ValueError):
            return f"{name}: data_b64 is not valid base64"
        if len(blob) != f.get("size"):
            return f"{name}: size does not match the payload"
        if hashlib.sha256(blob).hexdigest() != f.get("sha256"):
            return f"{name}: sha256 does not match the payload"
        magic = _MAGIC.get(ctype)
        if magic and not blob.startswith(magic):
            return f"{name}: content does not look like {ctype}"
        if ctype in ("text/html", "text/plain", "application/json"):
            try:
                blob.decode("utf-8")
            except UnicodeDecodeError:
                return f"{name}: text evidence is not valid UTF-8"
        total += len(blob)
        if total > EVIDENCE_MAX_BYTES:
            return f"evidence exceeds {EVIDENCE_MAX_BYTES} bytes"
    return None


def artefact_time(rec: dict):
    raw = rec.get("started_at") if rec.get("kind") == "run" else rec.get("ts")
    return parse_iso(raw) if raw else utcnow()


def archive_path(archive: Path, kind: str, name: str, art_id: str, rec: dict) -> Path:
    d = artefact_time(rec)
    return Path(archive) / kind / name / d.strftime("%Y") / d.strftime("%m") / d.strftime("%d") / f"{art_id}.json"


def _validate(rec, kind: str, name: str, art_id: str) -> str | None:
    """Return a rejection reason or None. Everything in ``rec`` is attacker-authored (see validate.py)."""
    bad = validate_record(rec, kind)
    if bad:
        return bad
    bad = check_name_matches_record(kind, name, rec)
    if bad:
        return bad
    if kind == "evidence":
        bad = validate_evidence(rec)
        if bad:
            return bad
    return None


def _reject(archive: Path, ref: str, raw: bytes, reason: str, stats: dict) -> None:
    """Quarantine under a name derived only from a hash of the (untrusted) ref - never from the ref text itself."""
    stem = ref_digest(ref)
    rej = Path(archive) / "_rejected" / f"{stem}.txt"
    rej.parent.mkdir(parents=True, exist_ok=True)
    rej.write_bytes(raw[:50_000])
    (Path(archive) / "_rejected" / f"{stem}.reason").write_text(
        "".join(c if c.isprintable() else "?" for c in f"{ref[:200]}: {reason}") + chr(10), encoding="utf-8")
    stats["rejected"] += 1


def _store(archive: Path, ring: str, kind: str, name: str, art_id: str, raw: bytes, stats: dict, source_label: str) -> None:
    ref = f"{ring}/{kind}/{name}/{art_id}"
    reason = check_path_parts(ring, kind, name, art_id)
    rec = None
    if not reason and len(raw) > MAX_BYTES[kind]:
        reason = f"artefact is {len(raw)} bytes (limit {MAX_BYTES[kind]})"
    if not reason:
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            reason = "invalid JSON"
    if not reason:
        try:
            reason = _validate(rec, kind, name, art_id)
        except Exception as exc:  # a forged record must never abort the whole ingest run
            reason = f"validator error: {type(exc).__name__}"
    if reason:
        _reject(archive, source_label if source_label else ref, raw, reason, stats)
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
        try:
            limit = MAX_BYTES.get(kind, 1_000_000)
            with open(f, "rb") as fh:
                raw = fh.read(limit + 1)  # never read an unbounded file into memory
            _store(archive, ring, kind, name, art_id, raw, stats, f"{ring}/{kind}/{name}/{art_id}")
        except OSError as exc:
            stats["errors"].append(f"cannot read {ref_digest(str(f))}: {type(exc).__name__}")
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
    """The token travels in the environment (GIT_CONFIG_*), never on the command line where ``ps`` would show it."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token and remote.startswith(("http://", "https://")):
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader", GIT_CONFIG_VALUE_0=f"PRIVATE-TOKEN: {token}")
    return [], env


def ingest_git(remote: str, archive: str | Path, delete: bool = True, token: str | None = None, max_refs: int = 2000) -> dict:
    """Fetch ingest/* refs from ``remote``, archive their artefacts, then delete the processed refs (batched).

    At most ``max_refs`` refs are processed per run (the rest stay for the next run); malformed or hostile refs are
    quarantined and *deleted* like any other, so poison is never re-fetched forever.
    """
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
        if len(refs) > max_refs:
            stats["deferred_refs"] = len(refs) - max_refs  # left for the next run; not an error
            refs = refs[:max_refs]
        for ref in refs:
            try:
                parts = ref[len("refs/ingest/"):].split("/")
                if len(parts) != 4:
                    # wrong shape: quarantine the ref name only and drop it, otherwise it is fetched again on every run
                    _reject(archive, ref, b"", "unexpected ref shape", stats)
                else:
                    ring, kind, name, art_id = parts
                    problem = check_path_parts(ring, kind, name, art_id)
                    if problem:
                        _reject(archive, ref, b"", problem, stats)
                    else:
                        # never `git show` an unbounded blob: ask for its size first
                        size = _git(["cat-file", "-s", f"{ref}:{kind}/{name}/{art_id}.json"], work, env)
                        if size.returncode != 0:
                            ls = _git(["ls-tree", "-r", "--name-only", ref], work, env).stdout.decode("utf-8", "replace").split()
                            _reject(archive, ref, b"", f"unexpected layout ({len(ls)} entries)", stats)
                        elif int(size.stdout.decode().strip() or 0) > MAX_BYTES[kind]:
                            _reject(archive, ref, b"", f"artefact is {size.stdout.decode().strip()} bytes (limit {MAX_BYTES[kind]})", stats)
                        else:
                            show = _git(["show", f"{ref}:{kind}/{name}/{art_id}.json"], work, env)
                            _store(archive, ring, kind, name, art_id, show.stdout, stats, ref)
            except Exception as exc:  # one poisoned ref must not stop the run or leave it undeleted
                stats["errors"].append(f"{ref_digest(ref)}: {type(exc).__name__}")
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
