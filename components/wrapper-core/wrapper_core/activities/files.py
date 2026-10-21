"""File-system activities (UiPath: Copy/Move/Delete File, Path Exists, Wait for file, file triggers, Compress/Extract)."""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Iterator, List, Optional

from ..retry import DataError, SystemUnavailable
from ..util import atomic_write_text, safe_name
from ._common import data_dir, emit


class FileWaitTimeout(SystemUnavailable, TimeoutError):
    """The expected file did not appear (or never stopped growing) in time."""


def sha256_of(path: "str | Path") -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- file-watcher semantics -----------------------------------------------------------------------------
def _state_path(state_name: str) -> Path:
    return data_dir("filestate") / f"{safe_name(state_name)}.json"


def _load_state(state_name: str) -> dict:
    try:
        return json.loads(_state_path(state_name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _sig(p: Path) -> str:
    st = p.stat()
    return f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}"


def new_files(folder: "str | Path", pattern: str = "*", state_name: str = "default", recursive: bool = False,
              min_age_s: float = 2.0, mark: bool = False) -> List[Path]:
    """Files in `folder` matching `pattern` that were not processed before (name + size + mtime), oldest first.

    Files modified less than `min_age_s` seconds ago are skipped: they may still be written by another process.
    Call :func:`mark_processed` after handling each file (or pass ``mark=True`` to mark them immediately).
    Designed for ``poll`` schedule slots: a quiet run returns ``[]``.
    """
    base = Path(folder)
    if not base.is_dir():
        return []
    it = base.rglob("*") if recursive else base.iterdir()
    seen = _load_state(state_name)
    now = time.time()
    out = []
    for p in it:
        try:
            if not p.is_file() or not fnmatch.fnmatch(p.name, pattern):
                continue
            st = p.stat()
            if now - st.st_mtime < min_age_s:
                continue
            if _sig(p) in seen:
                continue
            out.append((st.st_mtime, p))
        except OSError:
            continue  # vanished between listing and stat
    out.sort(key=lambda t: (t[0], str(t[1])))
    result = [p for _, p in out]
    if mark:
        for p in result:
            mark_processed(state_name, p)
    emit("files.new_files", count=len(result), state=state_name)
    return result


def mark_processed(state_name: str, path: "str | Path") -> None:
    p = Path(path)
    state = _load_state(state_name)
    state[_sig(p)] = time.time()
    if len(state) > 5000:  # keep the state file bounded: drop the oldest entries
        for k, _ in sorted(state.items(), key=lambda kv: kv[1])[: len(state) - 5000]:
            del state[k]
    atomic_write_text(_state_path(state_name), json.dumps(state))


def forget_processed(state_name: str) -> None:
    with contextlib.suppress(OSError):
        _state_path(state_name).unlink()


def wait_for_file(path: "str | Path", timeout: float = 60, stable_for_s: float = 1.0, poll: float = 0.25) -> Path:
    """Block until `path` exists and its size has been stable for `stable_for_s` seconds. Raises FileWaitTimeout."""
    p = Path(path)
    deadline = time.monotonic() + timeout
    last_size, stable_since = -1, None
    while time.monotonic() < deadline:
        try:
            size = p.stat().st_size
        except OSError:
            last_size, stable_since = -1, None
        else:
            if size == last_size:
                stable_since = stable_since if stable_since is not None else time.monotonic()
                if time.monotonic() - stable_since >= stable_for_s:
                    return p
            else:
                last_size, stable_since = size, None
        time.sleep(poll)
    raise FileWaitTimeout(f"file {p} did not appear/stabilise within {timeout}s")


# --- copy / move ----------------------------------------------------------------------------------------
def _unique(dst: Path) -> Path:
    if not dst.exists():
        return dst
    n = 1
    while True:
        cand = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
        if not cand.exists():
            return cand
        n += 1


def safe_copy(src: "str | Path", dst: "str | Path", overwrite: bool = False, verify: bool = True) -> Path:
    """Copy atomically (temp file in the destination folder, then rename). Never clobbers unless ``overwrite=True``.

    `dst` may be a directory (the file keeps its name). Returns the final path; with `verify` the sha256 of source and
    copy are compared and a mismatch raises DataError (the temp file is removed).
    """
    s, d = Path(src), Path(dst)
    if not s.is_file():
        raise DataError(f"source file not found: {s}")
    if d.is_dir():
        d = d / s.name
    d.parent.mkdir(parents=True, exist_ok=True)
    final = d if overwrite else _unique(d)
    fd, tmp = tempfile.mkstemp(dir=str(d.parent), prefix=".amx-", suffix=".part")
    os.close(fd)
    try:
        shutil.copyfile(s, tmp)
        if verify and sha256_of(s) != sha256_of(tmp):
            raise DataError(f"copy verification failed for {s}")
        os.replace(tmp, final)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    emit("files.safe_copy", bytes=final.stat().st_size)
    return final


def safe_move(src: "str | Path", dst: "str | Path", overwrite: bool = False, verify: bool = True) -> Path:
    """Move with the same guarantees as safe_copy: the source is deleted only after the copy is verified."""
    s = Path(src)
    final = safe_copy(s, dst, overwrite=overwrite, verify=verify)
    s.unlink()
    return final


# --- zip ------------------------------------------------------------------------------------------------
def zip_dir(src_dir: "str | Path", zip_path: "str | Path", include_root: bool = False) -> Path:
    src, zp = Path(src_dir), Path(zip_path)
    if not src.is_dir():
        raise DataError(f"not a directory: {src}")
    zp.parent.mkdir(parents=True, exist_ok=True)
    tmp = zp.with_suffix(zp.suffix + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                rel = p.relative_to(src.parent if include_root else src)
                z.write(p, rel.as_posix())
    os.replace(tmp, zp)
    return zp


def unzip_safe(zip_path: "str | Path", dest: "str | Path", max_files: int = 10000, max_total_bytes: int = 1 << 30) -> List[Path]:
    """Extract refusing zip-slip (``..``, absolute paths, drive letters), too many entries, or a zip bomb."""
    dest_p = Path(dest)
    dest_p.mkdir(parents=True, exist_ok=True)
    root = dest_p.resolve()
    out: List[Path] = []
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise DataError(f"not a valid zip: {zip_path}") from exc
    with z:
        infos = z.infolist()
        if len(infos) > max_files:
            raise DataError(f"zip has {len(infos)} entries (limit {max_files})")
        if sum(i.file_size for i in infos) > max_total_bytes:
            raise DataError("zip expands beyond the size limit")
        for info in infos:
            name = info.filename
            if name.startswith(("/", "\\")) or ":" in name.split("/")[0] or ".." in Path(name.replace("\\", "/")).parts:
                raise DataError(f"unsafe path in zip: {name}")
            target = (dest_p / name).resolve()
            if root != target and root not in target.parents:
                raise DataError(f"unsafe path in zip: {name}")
        for info in infos:
            z.extract(info, dest_p)
            if not info.is_dir():
                out.append((dest_p / info.filename))
    emit("files.unzip_safe", files=len(out))
    return out


# --- housekeeping ---------------------------------------------------------------------------------------
def cleanup_older_than(folder: "str | Path", days: Optional[float] = None, seconds: Optional[float] = None, pattern: str = "*",
                       dry_run: bool = False, recursive: bool = False) -> List[Path]:
    """Delete files older than `days`/`seconds` matching `pattern`. Returns what was (or would be, if dry_run) removed."""
    age = (days or 0) * 86400 + (seconds or 0)
    if age <= 0:
        raise ValueError("give days and/or seconds > 0")
    base = Path(folder)
    cutoff = time.time() - age
    removed = []
    if not base.is_dir():
        return removed
    for p in (base.rglob("*") if recursive else base.iterdir()):
        try:
            if p.is_file() and fnmatch.fnmatch(p.name, pattern) and p.stat().st_mtime < cutoff:
                if not dry_run:
                    p.unlink()
                removed.append(p)
        except OSError:
            continue
    emit("files.cleanup_older_than", removed=len(removed), dry_run=dry_run)
    return removed


@contextlib.contextmanager
def workdir(prefix: str = "amx") -> Iterator[Path]:
    """A per-run scratch directory (inside the run dir when under the runner) that is removed on exit."""
    parent = os.environ.get("AUTOMAGIX_RUN_DIR")
    if parent:
        Path(parent).mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix=prefix + "-", dir=parent or None))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
