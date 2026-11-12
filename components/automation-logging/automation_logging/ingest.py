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
from .validate import (MAX_BUNDLE_ITEMS, MAX_BYTES, check_name_matches_record, check_path_parts, normalize_timestamps, ref_digest,
                       validate_record)


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


MAX_REJECTED_FILES = 1000          # quarantine copies kept per archive
MAX_REJECT_BYTES = 20_000          # bytes of a rejected artefact kept for forensics
MAX_INGEST_FILES = 250_000         # files processed per ingest_dir run; newest first, so a cap can only ever cost OLD data (and is reported)
INGEST_WINDOW_DAYS = 60            # files dated older than this are not read at all (analytics only looks at 30 days)
MAX_SCAN_DEPTH = 8                 # directory levels below ingest/ that are walked (ring/kind/name/YYYY/MM/DD)


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
    stats["rejected"] += 1
    if not rej.exists() and sum(1 for _ in rej.parent.glob("*.txt")) >= MAX_REJECTED_FILES:
        stats["rejected_unstored"] = stats.get("rejected_unstored", 0) + 1   # a flood of poison must not fill the disk: count it, keep no copy
        return
    rej.write_bytes(raw[:MAX_REJECT_BYTES])
    (Path(archive) / "_rejected" / f"{stem}.reason").write_text(
        "".join(c if c.isprintable() else "?" for c in f"{ref[:200]}: {reason}") + chr(10), encoding="utf-8")


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
    if rec.get("ring") is not None and rec["ring"] != ring:
        _reject(archive, source_label if source_label else ref, raw, "ring in the content does not match the path", stats)
        return
    normalize_timestamps(rec)
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
    return {"archived": 0, "duplicates": 0, "rejected": 0, "by_kind": {}, "deleted_refs": 0, "errors": [],
            "deferred_files": 0, "out_of_window": 0, "symlinks": 0, "bundles": 0}


def _store_bundle(archive: Path, ring: str, name: str, art_id: str, raw: bytes, stats: dict, label: str) -> None:
    """A bundle is one commit that carries a run's artefacts: unpack it, validating every item exactly like a stand-alone artefact."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        _reject(archive, label, raw, "invalid JSON (bundle)", stats)
        return
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or doc.get("schema_version") != 1 or doc.get("kind") != "bundle" or not isinstance(items, list):
        _reject(archive, label, raw, "malformed bundle", stats)
        return
    if len(items) > MAX_BUNDLE_ITEMS:
        _reject(archive, label, raw, f"bundle has {len(items)} items (limit {MAX_BUNDLE_ITEMS})", stats)
        return
    stats["bundles"] += 1
    for i, it in enumerate(items):
        ilabel = f"{label}#{i}"
        if not isinstance(it, dict) or it.get("kind") not in KINDS or not isinstance(it.get("payload"), dict):
            _reject(archive, ilabel, b"", "malformed bundle item", stats)
            continue
        try:
            body = json.dumps(it["payload"], default=str).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            _reject(archive, ilabel, b"", "bundle item is not serialisable", stats)
            continue
        iname, iid = str(it.get("name", "")), str(it.get("id", ""))
        _store(archive, ring, it["kind"], iname, iid, body, stats, ilabel)


_ID_DATE = re.compile(r"^([0-9]{4})([0-9]{2})([0-9]{2})T")


def _path_date(rel: tuple, art_id: str):
    """The day an artefact belongs to: its YYYY/MM/DD folders, else the timestamp prefix of its id, else unknown (None)."""
    from datetime import date
    try:
        if len(rel) >= 6:
            return date(int(rel[3]), int(rel[4]), int(rel[5]))
        m = _ID_DATE.match(art_id)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        pass
    return None


def _scan(root: Path, stats: dict) -> list:
    """Collect (rel_dirs, art_id, path) for every .json below ``root`` WITHOUT following symlinks (a pushed symlink could point at any
    file or directory of the machine that runs the ingest); each symlink is counted as rejected and skipped."""
    out: list = []
    stack: list = [(str(root), (), 0)]
    while stack:
        path, rel, depth = stack.pop()
        try:
            entries = list(os.scandir(path))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink():
                    stats["rejected"] += 1
                    stats["symlinks"] += 1
                elif e.is_dir(follow_symlinks=False):
                    if depth < MAX_SCAN_DEPTH:
                        stack.append((e.path, rel + (e.name,), depth + 1))
                elif e.is_file(follow_symlinks=False) and e.name.endswith(".json"):
                    out.append((rel, e.name[:-5], e.path))
            except OSError:
                continue
    return out


def ingest_dir(src: str | Path, archive: str | Path, keep_source: bool = False, window_days: int = INGEST_WINDOW_DAYS, now=None) -> dict:
    """Archive what is under <src>/ingest (both layouts: flat <ring>/<kind>/<name>/<id>.json and date-partitioned
    <ring>/<kind>/<name>/YYYY/MM/DD/<id>.json; bundle files are unpacked).

    Files dated older than ``window_days`` are skipped unread (``out_of_window``). The rest are processed NEWEST FIRST and capped at
    ``MAX_INGEST_FILES``; anything beyond the cap is ``deferred_files`` - the caller must treat that as a data-quality failure.
    ``keep_source=False`` (the historical default of this function) deletes each processed file; ``ingest()`` (the CLI path) keeps them.
    """
    from datetime import date as _date
    from datetime import timedelta
    src, archive = Path(src), Path(archive)
    stats = _stats()
    root = src / "ingest"
    if not root.exists() or root.is_symlink():
        return stats
    cutoff = ((now or utcnow()) - timedelta(days=window_days)).date()
    todo = []
    for rel, art_id, path in _scan(root, stats):
        if len(rel) not in (3, 6):
            stats["rejected"] += 1                       # not a layout any transport writes
            continue
        d = _path_date(rel, art_id)
        if d is not None and d < cutoff:
            stats["out_of_window"] += 1
            continue
        todo.append((d, rel, art_id, path))
    todo.sort(key=lambda t: (t[0] or _date.max, t[3]), reverse=True)        # newest first
    for n_files, (_d, rel, art_id, path) in enumerate(todo):
        if n_files >= MAX_INGEST_FILES:
            stats["deferred_files"] += 1
            continue
        ring, kind, name = rel[0], rel[1], rel[2]
        try:
            limit = MAX_BYTES.get(kind, 1_000_000)
            with open(path, "rb") as fh:
                raw = fh.read(limit + 1)  # never read an unbounded file into memory
            label = f"{ring}/{kind}/{name}/{art_id}"
            reason = check_path_parts(ring, kind, name, art_id)
            if reason:
                _reject(archive, label, raw, reason, stats)
            elif kind == "bundle":
                if len(raw) > limit:
                    _reject(archive, label, raw, f"bundle is over {limit} bytes", stats)
                else:
                    _store_bundle(archive, ring, name, art_id, raw, stats, label)
            else:
                _store(archive, ring, kind, name, art_id, raw, stats, label)
        except OSError as exc:
            stats["errors"].append(f"cannot read {ref_digest(str(path))}: {type(exc).__name__}")
        if not keep_source:
            try:
                os.unlink(path)
            except OSError as exc:
                stats["errors"].append(f"cannot remove {ref_digest(str(path))}: {type(exc).__name__}")
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


def data_quality_problems(stats: dict, max_rejected: int = 50) -> list:
    """What makes an ingest run untrustworthy as the basis of dashboards and signals (the workflow fails on any of these)."""
    out = []
    if stats.get("deferred_files"):
        out.append(f"{stats['deferred_files']} file(s) deferred by the per-run cap (newest data was processed first; the oldest was not)")
    if stats.get("deferred_refs"):
        out.append(f"{stats['deferred_refs']} ref(s) deferred")
    if stats.get("rejected", 0) > max_rejected:
        out.append(f"{stats['rejected']} artefact(s) rejected (limit {max_rejected})")
    if stats.get("errors"):
        out.append(f"{len(stats['errors'])} read/removal error(s)")
    return out


def write_stats(archive: str | Path, stats: dict) -> None:
    """Leave the verdict next to the archive so the report (analytics data_quality) can show it - a silently incomplete archive
    must never look like a healthy one."""
    try:
        doc = {k: v for k, v in stats.items() if k not in ("errors", "by_kind")}
        doc["errors"] = len(stats.get("errors") or [])
        doc["at"] = iso(utcnow())
        p = Path(archive)
        p.mkdir(parents=True, exist_ok=True)
        (p / "_ingest_stats.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    except OSError:
        pass


def ingest(source: str, archive: str | Path, remote: str | None = None, src_dir: str | Path | None = None, **kw) -> dict:
    if source == "git":
        if not remote:
            raise IngestError("--remote is required for --source git")
        stats = ingest_git(remote, archive, **kw)
    elif source == "dir":
        if not src_dir:
            raise IngestError("--from is required for --source dir")
        # a directory given with --from is somebody else's checkout: never delete from it unless --consume says so
        stats = ingest_dir(src_dir, archive, keep_source=not kw.get("consume", False),
                           window_days=kw.get("window_days", INGEST_WINDOW_DAYS), now=kw.get("now"))
    else:
        raise IngestError(f"unknown source {source!r}")
    write_stats(archive, stats)
    return stats


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
