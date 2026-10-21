#!/usr/bin/env python3
"""Deterministic packaging of components and automation payloads into registry-ready zips (SPEC section 4/5).

    package.py component wrapper-core <srcdir> --version 0.1.0 --out dist/
    package.py component execution-framework <srcdir> --version 0.1.0 --out dist/
    package.py automation automation-example-hello <srcdir> --version 1.0.0 --out dist/

The zip is written to ``<out>/components/<name>-<V>.zip`` or ``<out>/automations/<name>-<V>.zip`` (same relative
``url`` the manifest uses; ``--flat`` writes straight into ``<out>``). stdout is one JSON line:
``{"name","version","url","sha256","size"}``.

Determinism: entries sorted by archive path, fixed timestamp, fixed permissions, text files LF-normalised, so the
same inputs always produce the identical sha256 (a reproducible release is an auditable release).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _wc import miniyaml, versions  # noqa: E402

FIXED_TIME = (2020, 1, 1, 0, 0, 0)
# directories / files silently left out (build junk)
SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode", ".venv"}
SKIP_FILES = ["*.pyc", "*.pyo", ".DS_Store", "Thumbs.db", "*.zip.sha256"]
# files that must never ship: packaging FAILS rather than silently dropping them, so a leaked key is noticed
SECRET_PATTERNS = ["*.key", "*.pem", "*.pfx", "*.p12", "id_rsa*", "id_ed25519*", "id_ecdsa*", ".env", ".env.*", "*.dpapi",
                   "*secret*.json", "credentials*.json", "*.kdbx"]
# vendored third-party trees: test fixtures there legitimately contain .pem/.key files, so the secret check skips them
VENDORED_DIRS = {"node_modules", "python-embed", "node", "site-packages"}
TEXT_EXT = {".py", ".js", ".mjs", ".cjs", ".json", ".yaml", ".yml", ".md", ".txt", ".j2", ".ini", ".toml", ".cfg",
            ".html", ".css", ".ts", ".xml"}
TEXT_NAMES = {"VERSION", "LICENSE"}
RUNTIMES = {"python", "node", "powershell", "exe"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PackageError(ValueError):
    pass


def _matches(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name.lower(), p.lower()) for p in patterns)


def iter_files(root: Path, only: list[str] | None = None):
    """Yield (archive_path, file_path) in sorted order; raises PackageError on secret-looking files."""
    root = root.resolve()
    bases = [root / o for o in only] if only else [root]
    found: list[tuple[str, Path]] = []
    for base in bases:
        if not base.exists():
            raise PackageError(f"expected path missing in source: {base.relative_to(root)}")
        walker = [(base.parent, [], [base.name])] if base.is_file() else os.walk(base)
        for dirpath, dirnames, filenames in walker:
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                if _matches(fn, SKIP_FILES):
                    continue
                rel_parts = set(p.relative_to(root).parts[:-1])
                if _matches(fn, SECRET_PATTERNS) and not (rel_parts & VENDORED_DIRS):
                    raise PackageError(f"refusing to package secret-looking file: {p.relative_to(root)}")
                if p.is_symlink():
                    raise PackageError(f"refusing to package symlink: {p.relative_to(root)}")
                found.append((p.relative_to(root).as_posix(), p))
    return sorted(found)


def _normalise(arcname: str, data: bytes) -> bytes:
    name = arcname.rsplit("/", 1)[-1]
    ext = os.path.splitext(name)[1].lower()
    if (ext in TEXT_EXT or name in TEXT_NAMES) and b"\0" not in data:
        return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data


def write_zip(out_zip: Path, entries: list[tuple[str, bytes]]) -> None:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_zip.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for arc, data in sorted(entries):
            info = zipfile.ZipInfo(arc, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # unix: keeps external_attr stable across build hosts
            info.external_attr = 0o644 << 16
            z.writestr(info, data, compresslevel=9)
    os.replace(tmp, out_zip)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_automation(src: Path, name: str, version: str) -> dict:
    """Check automation.yaml: name/version consistency, entrypoint exists, requires parse, runtime known."""
    f = src / "automation.yaml"
    if not f.exists():
        raise PackageError("automation.yaml not found at payload root")
    try:
        d = miniyaml.load(f)
    except miniyaml.YamlError as exc:
        raise PackageError(f"automation.yaml is not valid: {exc}") from exc
    if not isinstance(d, dict):
        raise PackageError("automation.yaml must be a mapping")
    if d.get("name") != name:
        raise PackageError(f"automation.yaml name {d.get('name')!r} != package name {name!r}")
    if str(d.get("version")) != version:
        raise PackageError(f"automation.yaml version {d.get('version')!r} != --version {version!r}")
    runtime = d.get("runtime", "python")
    if runtime not in RUNTIMES:
        raise PackageError(f"unknown runtime {runtime!r} (expected one of {sorted(RUNTIMES)})")
    entry = d.get("entrypoint")
    if not entry:
        raise PackageError("automation.yaml needs an entrypoint")
    ep = (src / str(entry)).resolve()
    if src.resolve() not in ep.parents or not ep.is_file():
        raise PackageError(f"entrypoint {entry!r} does not exist inside the payload")
    for key, spec in (d.get("requires") or {}).items():
        try:
            versions.validate_spec(str(spec))
        except ValueError as exc:
            raise PackageError(f"requires.{key} {spec!r} is not a valid constraint") from exc
    try:
        versions.parse(version)
    except ValueError as exc:
        raise PackageError(str(exc)) from exc
    return d


def package(kind: str, name: str, src: Path, version: str, out: Path, flat: bool = False) -> dict:
    if kind not in ("component", "automation"):
        raise PackageError("kind must be component|automation")
    if not NAME_RE.match(name):
        raise PackageError(f"invalid name {name!r}")
    try:
        versions.parse(version)
    except ValueError as exc:
        raise PackageError(str(exc)) from exc
    src = Path(src)
    if not src.is_dir():
        raise PackageError(f"source dir not found: {src}")

    extra: list[tuple[str, bytes]] = []
    if kind == "automation":
        validate_automation(src, name, version)
        files = iter_files(src)
    elif name == "wrapper-core":
        # SPEC: wrapper-core zip holds wrapper_core/ + VERSION only (tests, pyproject etc. stay in the repo)
        files = iter_files(src, only=["wrapper_core"])
        extra.append(("VERSION", (version + "\n").encode()))
    else:
        files = iter_files(src)
        extra.append(("VERSION", (version + "\n").encode()))

    entries: dict[str, bytes] = {}
    for arc, p in files:
        entries[arc] = _normalise(arc, p.read_bytes())
    for arc, data in extra:
        entries[arc] = data  # generated VERSION wins over any stale file
    if not entries:
        raise PackageError("nothing to package")

    sub = "" if flat else ("components" if kind == "component" else "automations")
    rel = f"{sub}/{name}-{version}.zip" if sub else f"{name}-{version}.zip"
    target = Path(out) / rel
    write_zip(target, list(entries.items()))
    return {"name": name, "version": version, "url": rel, "sha256": sha256_of(target), "size": target.stat().st_size}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="kind", required=True)
    for kind in ("component", "automation"):
        s = sub.add_parser(kind)
        s.add_argument("name")
        s.add_argument("srcdir")
        s.add_argument("--version", required=True)
        s.add_argument("--out", required=True)
        s.add_argument("--flat", action="store_true", help="write <out>/<name>-<V>.zip with no components/ automations/ subdir")
    args = p.parse_args(argv)
    try:
        res = package(args.kind, args.name, Path(args.srcdir), args.version, Path(args.out), args.flat)
    except PackageError as exc:
        print(f"package.py: error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
