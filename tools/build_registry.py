#!/usr/bin/env python3
"""Build the publishable registry from the working tree (the GitHub-hosted replacement for a package feed).

    python tools/build_registry.py --out registry-out [--artifacts-dir <checkout of branch registry-artifacts>]
                                   [--write-manifest components/automation-registry/manifest.yaml] [--bootstrap]

What it does (see docs/PIPELINES.md):

1. Packages every component (wrapper-core, execution-framework) and every automation under ``components/automations``
   into deterministic zips using ``automation-builder/scripts/package.py``.
2. Compares them with the zips already published on the append-only ``registry-artifacts`` branch (``--artifacts-dir``).
   Released versions are IMMUTABLE: same version + different content => FAIL ("bump the version"). New version => added.
3. Regenerates the manifest for NEW versions through the registry ops: a new version enters ring ``dev`` only. Promotion to
   pilot/broad is a human decision made in a pull request (``registry promote`` / the admin panel) - never done here.
4. Validates (``registry validate --artifacts``, placeholders NOT allowed) and writes a publishable registry directory:
   ``manifest.yaml``, ``triggers.json``, ``index.json``, ``artifacts/<components|automations>/<name>-<version>.zip``.

``--bootstrap`` is the first-run mode: it seeds a complete, valid manifest from the repository (keeping the catalog metadata
of the starter manifest, replacing placeholder hashes with real ones, and putting each automation's current version in every
ring so the install pages work immediately). After that, only dev-ring additions happen automatically.

Zips are compared by *content* (names + decompressed bytes), not by zip bytes, so a different zlib on a developer machine can
never be mistaken for a changed release.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENTS = ROOT / "components"
sys.path.insert(0, str(COMPONENTS / "wrapper-core"))
sys.path.insert(0, str(COMPONENTS / "automation-registry"))

from wrapper_core import miniyaml  # noqa: E402
from registry import checks, ops, store  # noqa: E402
from registry.ops import OpError  # noqa: E402

PLACEHOLDER = "0" * 64
FRAMEWORK_EXCLUDE_DIRS = {"test", "tools", ".git", "__pycache__"}
FRAMEWORK_EXCLUDE_FILES = {".gitlab-ci.yml", ".npmrc", ".gitignore"}
RINGS = ("dev", "pilot", "broad")


def _package_module():
    spec = importlib.util.spec_from_file_location("amx_package", COMPONENTS / "automation-builder" / "scripts" / "package.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["amx_package"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class BuildError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass
class Source:
    kind: str          # component | automation
    name: str
    version: str
    src: Path
    descriptor: dict | None = None

    @property
    def folder(self) -> str:
        return "components" if self.kind == "component" else "automations"

    @property
    def rel(self) -> str:
        return f"artifacts/{self.folder}/{self.name}-{self.version}.zip"


def discover(repo: Path) -> list[Source]:
    comps = Path(repo) / "components"
    out: list[Source] = []
    for name in ("wrapper-core", "execution-framework"):
        d = comps / name
        vf = d / "VERSION"
        if d.is_dir() and vf.is_file():
            out.append(Source("component", name, vf.read_text(encoding="utf-8").strip(), d))
    adir = comps / "automations"
    if adir.is_dir():
        for d in sorted(p for p in adir.iterdir() if p.is_dir()):
            f = d / "automation.yaml"
            if f.is_file():
                desc = miniyaml.loads(f.read_text(encoding="utf-8")) or {}
                out.append(Source("automation", str(desc.get("name") or d.name), str(desc.get("version")), d, desc))
    return out


def content_digest(zip_path: Path) -> str:
    """sha256 over sorted (entry name, sha256 of decompressed bytes): independent of zlib/zip metadata."""
    h = hashlib.sha256()
    with zipfile.ZipFile(zip_path) as z:
        for info in sorted(z.infolist(), key=lambda i: i.filename):
            if info.is_dir():
                continue
            h.update(info.filename.encode("utf-8") + b"\0" + hashlib.sha256(z.read(info)).digest())
    return h.hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _stage_framework(src: Path, tmp: Path) -> Path:
    """Ship the runtime, not the repo: no tests/tools/CI files. node_modules only when a lockfile pins it (determinism)."""
    dest = tmp / "execution-framework"
    has_lock = (src / "package-lock.json").is_file()

    def ignore(d, names):
        skip = {n for n in names if n in FRAMEWORK_EXCLUDE_FILES}
        if Path(d).resolve() == src.resolve():
            skip |= {n for n in names if n in FRAMEWORK_EXCLUDE_DIRS}
            if not has_lock:
                skip.add("node_modules")
        else:
            skip |= {n for n in names if n in ("__pycache__",)}
        return skip

    shutil.copytree(src, dest, ignore=ignore)
    return dest


def _requires(desc: dict | None) -> dict:
    req = (desc or {}).get("requires") or {}
    out = {}
    if req.get("wrapper_core"):
        out["wrapper_core"] = str(req["wrapper_core"])
    if req.get("execution_framework"):
        out["execution_framework"] = str(req["execution_framework"])
    return out


def build(repo: Path, manifest_path: Path, out: Path, artifacts_dir: Path | None = None, bootstrap: bool = False,
          bootstrap_rings: str = "all", default_owner: str = "automation-platform@example.com",
          write_manifest: Path | None = None, new_dir: Path | None = None) -> dict:
    repo, manifest_path, out = Path(repo), Path(manifest_path), Path(out)
    pkg = _package_module()
    m = copy.deepcopy(store.load_manifest(manifest_path))
    errors: list[str] = []
    added: list[str] = []
    reused: list[str] = []
    new_files: list[str] = []
    if out.exists():
        shutil.rmtree(out)
    (out / "artifacts" / "components").mkdir(parents=True)
    (out / "artifacts" / "automations").mkdir(parents=True)
    new_dir = Path(new_dir) if new_dir else None
    if new_dir:
        shutil.rmtree(new_dir, ignore_errors=True)

    # published history first: every zip already on the artifacts branch is part of the registry
    published: dict[str, Path] = {}
    if artifacts_dir and (Path(artifacts_dir) / "artifacts").is_dir():
        for z in sorted((Path(artifacts_dir) / "artifacts").rglob("*.zip")):
            rel = z.relative_to(artifacts_dir).as_posix()
            published[rel] = z
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(z, dest)

    with tempfile.TemporaryDirectory(prefix="amx-reg-") as tmpname:
        tmp = Path(tmpname)
        for s in discover(repo):
            src = _stage_framework(s.src, tmp) if s.name == "execution-framework" else s.src
            try:
                info = pkg.package(s.kind, s.name, src, s.version, tmp / "zips")
            except pkg.PackageError as exc:
                errors.append(f"{s.name} {s.version}: cannot package: {exc}")
                continue
            built = tmp / "zips" / info["url"]
            final = out / s.rel
            if s.rel in published:
                if content_digest(published[s.rel]) != content_digest(built):
                    errors.append(f"{s.name} {s.version} is already released with different content. Released versions are immutable: "
                                  "bump the version (VERSION file / automation.yaml version) instead of editing it.")
                    continue
                reused.append(f"{s.name} {s.version}")
            else:
                final.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(built, final)
                if new_dir:
                    nd = new_dir / s.rel
                    nd.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(built, nd)
                    new_files.append(s.rel)
            sha = sha256_file(final)
            rec: dict = {"url": s.rel, "sha256": sha}
            if s.kind == "automation" and _requires(s.descriptor):
                rec["requires"] = _requires(s.descriptor)

            coll = m.setdefault("components" if s.kind == "component" else "automations", {})
            entry = coll.get(s.name)
            try:
                if entry is None:
                    if s.kind == "component":
                        coll[s.name] = entry = {"rings": {}, "versions": {}}
                    else:
                        ops.new_automation(m, s.name, owner=default_owner,
                                           description=str((s.descriptor or {}).get("description") or f"{s.name} (auto-registered: complete the catalog entry)"))
                        entry = coll[s.name]
                vs = entry.setdefault("versions", {})
                if bootstrap:
                    # seed: keep only versions we can actually serve; point every ring (or just dev) at the current version
                    vs.clear()
                    vs[s.version] = rec
                    rings = RINGS if bootstrap_rings == "all" else ("dev",)
                    entry["rings"] = {r: s.version for r in rings}
                    added.append(f"{s.name} {s.version} (bootstrap: rings {', '.join(rings)})")
                elif s.version in vs:
                    cur = (vs[s.version] or {}).get("sha256")
                    if cur == PLACEHOLDER:
                        errors.append(f"{s.name} {s.version} still has a PLACEHOLDER sha256 in the manifest; run once with --bootstrap")
                    elif cur != sha:
                        errors.append(f"{s.name} {s.version}: manifest sha256 {str(cur)[:12]}... differs from the published package {sha[:12]}...; "
                                      "released versions are immutable - bump the version")
                    else:
                        vs[s.version] = {**vs[s.version], **{k: v for k, v in rec.items() if k == "requires"}}
                else:
                    res = ops.add_version(m, s.name, s.version, rec, kind=s.kind)
                    added.append(f"{s.name} {s.version} -> dev")
                    _ = res
            except OpError as exc:
                errors.append(f"{s.name} {s.version}: {exc}")

    report = checks.validate_all(m, allow_placeholders=False, artifacts_dir=out)
    errors += [f"manifest: {e}" for e in report.errors]
    triggers_src = manifest_path.parent / store.TRIGGERS_FILE
    store.save_triggers(out / "triggers.json", store.load_triggers(triggers_src))
    text = store.dump_manifest(m)
    store.atomic_write(out / "manifest.yaml", text)
    index = {"schema_version": 1, "generated_at": store.now_iso(),
             "components": {n: {"rings": (c or {}).get("rings") or {}, "versions": sorted(((c or {}).get("versions") or {}))} for n, c in (m.get("components") or {}).items()},
             "automations": {n: {"rings": (a or {}).get("rings") or {}, "status": (a or {}).get("status"), "versions": sorted(((a or {}).get("versions") or {}))}
                             for n, a in (m.get("automations") or {}).items()}}
    store.atomic_write(out / "index.json", json.dumps(index, indent=2, sort_keys=True) + "\n")
    changed = text != manifest_path.read_text(encoding="utf-8")
    if write_manifest and not errors and changed:
        store.atomic_write(Path(write_manifest), text)
    result = {"ok": not errors, "errors": errors, "added": added, "reused": reused, "new_artifacts": new_files, "manifest_changed": changed,
              "warnings": list(getattr(report, "warnings", []) or [])}
    store.atomic_write(out / "build-summary.json", json.dumps(result, indent=2) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=str(ROOT))
    ap.add_argument("--manifest", default=None, help="source-of-truth manifest (default components/automation-registry/manifest.yaml)")
    ap.add_argument("--out", default="registry-out")
    ap.add_argument("--artifacts-dir", default=None, help="checkout of the registry-artifacts branch (contains artifacts/...)")
    ap.add_argument("--new-dir", default=None, help="write NEW zips here (artifacts/...) so CI can push them to registry-artifacts")
    ap.add_argument("--write-manifest", default=None, help="write the regenerated manifest back to this path (when changed and valid)")
    ap.add_argument("--bootstrap", action="store_true")
    ap.add_argument("--bootstrap-rings", choices=["all", "dev"], default="all")
    ap.add_argument("--default-owner", default="automation-platform@example.com")
    ap.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"), help="append changed=true/false, new=N to this file (GitHub Actions)")
    a = ap.parse_args(argv)
    manifest = Path(a.manifest) if a.manifest else Path(a.repo) / "components" / "automation-registry" / "manifest.yaml"
    res = build(Path(a.repo), manifest, Path(a.out), Path(a.artifacts_dir) if a.artifacts_dir else None, a.bootstrap, a.bootstrap_rings,
                a.default_owner, Path(a.write_manifest) if a.write_manifest else None, Path(a.new_dir) if a.new_dir else None)
    for line in res["added"]:
        print("added  ", line)
    for line in res["reused"]:
        print("reused ", line, "(already published, content identical)")
    for line in res["errors"]:
        print("ERROR  ", line, file=sys.stderr)
    if a.github_output:
        with open(a.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if res['manifest_changed'] else 'false'}\nnew={len(res['new_artifacts'])}\nok={'true' if res['ok'] else 'false'}\n")
    print(f"registry built in {a.out}: {len(res['new_artifacts'])} new package(s), manifest {'CHANGED' if res['manifest_changed'] else 'unchanged'}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
