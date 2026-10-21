#!/usr/bin/env python3
"""Assemble a CycloneDX 1.5 SBOM (JSON) for everything an installer ships or the build pulls in.

    python tools/make_sbom.py --out sbom.cdx.json [--version X]

Sources, all read from this repo (nothing is downloaded, so it is deterministic and offline-safe):
  * pinned runtime bundles: Node (tools/build_framework_bundle.py), embedded Python and its SHA-256 (.github/workflows/installer-dev.yml),
    MinGit and its SHA-256 (tools/fetch_mingit.py), playwright-core (components/execution-framework/package.json)
  * npm lockfile packages when components/execution-framework/package-lock.json exists
  * Python requirement specs (docker/requirements.txt and the dependencies in each pyproject.toml)
  * the platform components themselves (wrapper-core, execution-framework, automations) with their versions
Unpinned requirement specs are listed with their range, never pretended to be exact.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
SPEC_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*((?:[<>=!~]=?|===)[^;#]*)?")


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def purl(kind: str, name: str, version: str = "") -> str:
    safe = re.sub(r"[^A-Za-z0-9._@/-]", "-", name)
    return f"pkg:{kind}/{safe}" + (f"@{version}" if version else "")


def component(name: str, version: str, kind: str, ctype: str = "library", sha256: str = "", url: str = "", note: str = "", scope: str = "required") -> Dict[str, Any]:
    c: Dict[str, Any] = {"type": ctype, "bom-ref": purl(kind, name, version) if kind else f"{name}@{version}", "name": name, "version": version or "unspecified",
                         "scope": scope}
    if kind:
        c["purl"] = purl(kind, name, version)
    if sha256 and re.fullmatch(r"[0-9a-f]{64}", sha256):
        c["hashes"] = [{"alg": "SHA-256", "content": sha256}]
    props = []
    if url:
        props.append({"name": "autostation:download-url", "value": url})
    if note:
        props.append({"name": "autostation:note", "value": note})
    if props:
        c["properties"] = props
    return c


def parse_requirements(text: str) -> List[Dict[str, str]]:
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        m = SPEC_RE.match(line)
        if m:
            out.append({"name": m.group(1).lower(), "spec": (m.group(2) or "").strip()})
    return out


def parse_pyproject_deps(text: str) -> List[Dict[str, str]]:
    m = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)^\]", text)
    if not m:
        return []
    items = re.findall(r"""["']([^"']+)["']""", m.group(1))
    return parse_requirements("\n".join(items))


def pinned_version(spec: str) -> str:
    m = re.fullmatch(r"(?:==|===)\s*([A-Za-z0-9._+!-]+)", spec or "")
    return m.group(1) if m else ""


def node_bundle(root: Path) -> List[Dict[str, Any]]:
    out = []
    m = re.search(r'DEFAULT_NODE\s*=\s*"([0-9.]+)"', read(root / "tools" / "build_framework_bundle.py"))
    if m:
        out.append(component("node", m.group(1), "generic", "application",
                             url=f"https://nodejs.org/dist/v{m.group(1)}/node-v{m.group(1)}-win-x64.zip",
                             note="Windows x64 runtime bundled into execution-framework; verified against nodejs.org SHASUMS256.txt at build time"))
    pkg = read(root / "components" / "execution-framework" / "package.json")
    try:
        deps = (json.loads(pkg).get("dependencies") or {}) if pkg else {}
    except ValueError:
        deps = {}
    for name, ver in sorted(deps.items()):
        out.append(component(name, str(ver).lstrip("^~="), "npm", note="pinned in components/execution-framework/package.json"))
    return out


def lockfile_packages(root: Path) -> List[Dict[str, Any]]:
    text = read(root / "components" / "execution-framework" / "package-lock.json")
    if not text:
        return []
    try:
        pk = json.loads(text).get("packages") or {}
    except ValueError:
        return []
    out = []
    for path, info in sorted(pk.items()):
        if not path.startswith("node_modules/") or not isinstance(info, dict):
            continue
        name = path.split("node_modules/")[-1]
        c = component(name, str(info.get("version") or ""), "npm", scope="optional" if info.get("dev") else "required")
        integ = str(info.get("integrity") or "")
        if integ.startswith("sha512-"):
            c["properties"] = c.get("properties", []) + [{"name": "autostation:npm-integrity", "value": integ}]
        out.append(c)
    return out


def python_embed(root: Path) -> List[Dict[str, Any]]:
    wf = read(root / ".github" / "workflows" / "installer-dev.yml")
    url = re.search(r"PY_EMBED_URL:\s*(\S+)", wf)
    sha = re.search(r"PY_EMBED_SHA256:\s*([0-9a-f]{64})", wf)
    if not url:
        return []
    ver = re.search(r"python-([0-9.]+)-embed", url.group(1))
    return [component("python-embed", ver.group(1) if ver else "", "generic", "application", sha.group(1) if sha else "", url.group(1),
                      "embeddable CPython bundled into every installer; SHA-256 verified by the installer workflow")]


def mingit(root: Path) -> List[Dict[str, Any]]:
    text = read(root / "tools" / "fetch_mingit.py")
    ver, sha, tag = (re.search(r'VERSION\s*=\s*"([0-9.]+)"', text), re.search(r'SHA256\s*=\s*"([0-9a-f]{64})"', text),
                     re.search(r'TAG\s*=\s*"([A-Za-z0-9._-]+)"', text))
    if not ver:
        return []
    url = f"https://github.com/git-for-windows/git/releases/download/{tag.group(1)}/MinGit-{ver.group(1)}-64-bit.zip" if tag else ""
    return [component("mingit", ver.group(1), "generic", "application", sha.group(1) if sha else "", url,
                      "git + ssh bundled for the ssh log transport; SHA-256 pinned in tools/fetch_mingit.py")]


def python_requirements(root: Path) -> List[Dict[str, Any]]:
    found: Dict[str, Dict[str, str]] = {}
    for path in [root / "components" / "automation-builder" / "docker" / "requirements.txt"]:
        for r in parse_requirements(read(path)):
            found.setdefault(r["name"], r)
    for path in sorted((root / "components").glob("*/pyproject.toml")):
        for r in parse_pyproject_deps(read(path)):
            found.setdefault(r["name"], r)
    out = []
    for name in sorted(found):
        spec = found[name]["spec"]
        c = component(name, pinned_version(spec), "pypi", note=f"version range {spec}" if spec and not pinned_version(spec) else "")
        out.append(c)
    return out


def platform_components(root: Path) -> List[Dict[str, Any]]:
    out = []
    for comp in ("wrapper-core", "execution-framework"):
        v = read(root / "components" / comp / "VERSION").strip()
        if v:
            out.append(component(comp, v, "generic", "application", note="AutoStation platform component"))
    for yaml_path in sorted((root / "components" / "automations").glob("*/automation.yaml")):
        m = re.search(r"(?m)^version:\s*([0-9][0-9A-Za-z.+-]*)", read(yaml_path))
        if m:
            out.append(component(yaml_path.parent.name, m.group(1), "generic", "application", note="AutoStation automation payload"))
    return out


def build(root: Path = ROOT, version: str = "") -> Dict[str, Any]:
    comps = platform_components(root) + node_bundle(root) + python_embed(root) + mingit(root) + python_requirements(root) + lockfile_packages(root)
    seen, unique = set(), []
    for c in comps:
        if c["bom-ref"] not in seen:
            seen.add(c["bom-ref"])
            unique.append(c)
    ident = uuid.uuid5(uuid.NAMESPACE_URL, "autostation-sbom:" + (version or "") + ":" + json.dumps([c["bom-ref"] for c in unique]))   # same inputs + same build -> same serial
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "serialNumber": f"urn:uuid:{ident}", "version": 1,
            "metadata": {"component": {"type": "application", "name": "AutoStation", "version": version or "unspecified", "bom-ref": "autostation"},
                         "tools": {"components": [{"type": "application", "name": "tools/make_sbom.py"}]}},
            "components": unique}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--version", default="")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    doc = build(Path(a.root), a.version)
    Path(a.out).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {a.out}: {len(doc['components'])} component(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
