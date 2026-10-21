#!/usr/bin/env python3
"""Check a generated site directory: required files, every relative link/src resolves (incl. #anchors), install pages are complete.

    python tools/check_site.py site            # exit 1 on any problem
"""
from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

REQUIRED = ["index.html", "404.html", ".nojekyll", "status.json", "assets/site.css", "assets/site.js", "install/index.html", "install/catalog.json",
            "install/bootstrap.ps1", "docs/index.html", "docs/search-index.json", "portal/index.html", "portal/admin.html", "dashboard/index.html",
            "program/index.html", "jobs/index.html", "machines/index.html", "queues/index.html", "schedules/index.html", "audit/index.html",
            "reliability/index.html", "errors/index.html", "metrics.json", "metrics.prom", "fleet-status.json", "search-ops.json", "assets/gsearch.js", "badges/fleet.svg"]


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str, str]] = []   # (tag, attr, value)
        self.ids: set[str] = set()
        self.csp = ""                                  # content of the CSP <meta>
        self.inline_scripts: list[str] = []            # bodies of executable inline <script> blocks
        self._script_buf: list[str] | None = None

    def handle_data(self, data):
        if self._script_buf is not None:
            self._script_buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script_buf is not None:
            self.inline_scripts.append("".join(self._script_buf))
            self._script_buf = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and str(a.get("http-equiv", "")).lower() == "content-security-policy":
            self.csp = a.get("content") or ""
        if tag == "script" and a.get("src") is None and str(a.get("type", "text/javascript")).lower() in ("", "text/javascript", "module"):
            self._script_buf = []
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "a" and a.get("name"):
            self.ids.add(a["name"])
        for attr in ("href", "src"):
            if a.get(attr) is not None and tag in ("a", "link", "script", "img", "iframe", "source"):
                self.links.append((tag, attr, a[attr]))


def parse(path: Path) -> _Page:
    p = _Page()
    p.feed(path.read_text(encoding="utf-8", errors="replace"))
    return p


def _check_inline_scripts(rel: str, info: "_Page") -> list[str]:
    """A page whose script-src has no 'unsafe-inline' may only contain inline scripts whose SHA-256 the CSP lists: anything else would be
    blocked in the browser (a broken page) or - if it came from injected markup - exactly what the policy exists to stop."""
    import base64
    import hashlib
    m = re.search(r"script-src([^;]*)", info.csp)
    if not m or "'unsafe-inline'" in m.group(1):
        return []
    allowed = set(re.findall(r"'sha256-([A-Za-z0-9+/=]+)'", m.group(1)))
    out = []
    for body in info.inline_scripts:
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
        if digest not in allowed:
            out.append(f"{rel}: an inline script is not covered by the page's CSP hashes (it would be blocked, or it was injected): {body.strip()[:60]!r}")
    return out


def check_site(site: Path) -> list[str]:
    site = Path(site)
    problems: list[str] = []
    for rel in REQUIRED:
        if not (site / rel).exists():
            problems.append(f"missing required file: {rel}")
    pages = {p: parse(p) for p in sorted(site.rglob("*.html"))}
    for page, info in pages.items():
        rel = page.relative_to(site).as_posix()
        problems += _check_inline_scripts(rel, info)
        for tag, attr, val in info.links:
            v = val.strip()
            if not v or v.startswith(("http://", "https://", "mailto:", "data:", "tel:")):
                if v.startswith("http://"):
                    problems.append(f"{rel}: insecure http:// link {v[:60]}")
                if tag in ("script",) and v.startswith(("http://", "https://")):
                    problems.append(f"{rel}: external script {v[:60]} (the site must not load third-party scripts)")
                continue
            if v.lower().startswith("javascript:"):
                problems.append(f"{rel}: javascript: URL")
                continue
            parts = urlsplit(v)
            path_part = unquote(parts.path)
            target = page if not path_part else (page.parent / path_part).resolve()
            if path_part.endswith("/") or (target.is_dir() if path_part else False):
                target = target / "index.html"
            try:
                inside = site.resolve() in target.resolve().parents or target.resolve() == site.resolve()
            except OSError:
                inside = False
            if not inside:
                problems.append(f"{rel}: link escapes the site: {v}")
                continue
            if not target.exists():
                problems.append(f"{rel}: broken link {v}")
                continue
            if parts.fragment and target.suffix == ".html":
                tinfo = pages.get(target) or parse(target)
                if "=" not in parts.fragment and parts.fragment not in tinfo.ids:   # "#run=<id>" style fragments are client-side view state
                    problems.append(f"{rel}: anchor #{parts.fragment} not found in {target.relative_to(site).as_posix()}")
    # install pages
    cat_path = site / "install" / "catalog.json"
    if cat_path.exists():
        try:
            cat = json.loads(cat_path.read_text(encoding="utf-8"))
        except ValueError:
            cat = {}
            problems.append("install/catalog.json is not valid JSON")
        for name, entry in (cat.get("automations") or {}).items():
            page = site / "install" / f"{name}.html"
            if not page.exists():
                problems.append(f"install page missing for {name}")
                continue
            text = page.read_text(encoding="utf-8")
            url = (entry.get("installer") or {}).get("url", "")
            if not url or url not in text:
                problems.append(f"install/{name}.html has no download link to {url or '(no installer url)'}")
            if "SHA-256" not in text:
                problems.append(f"install/{name}.html has no SHA-256 block")
            if "Get-FileHash" not in text:
                problems.append(f"install/{name}.html has no verify instructions")
    status = site / "status.json"
    if status.exists():
        try:
            json.loads(status.read_text(encoding="utf-8"))
        except ValueError:
            problems.append("status.json is not valid JSON")
    problems += check_observability(site)
    return problems


_PROM_LINE = re.compile(r'^(#( HELP| TYPE) [a-z_:][a-z0-9_:]* .+|[a-z_:][a-z0-9_:]*(\{([a-z_][a-z0-9_]*="([^"\\]|\\.)*"(,|(?=\})))*\})? -?[0-9.eE+-]+)$')
_SAFE_REL = re.compile(r"^[A-Za-z0-9_\-/.#=&%]+$")


def check_observability(site: Path) -> list[str]:
    """metrics.json / metrics.prom / fleet-status.json / badges / search-ops.json: well-formed, bounded, nothing executable."""
    out: list[str] = []
    for name in ("metrics.json", "fleet-status.json"):
        f = site / name
        if f.exists():
            try:
                json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                out.append(f"{name} is not valid JSON")
    prom = site / "metrics.prom"
    if prom.exists():
        for n, line in enumerate(prom.read_text(encoding="utf-8").splitlines(), 1):
            if line and not _PROM_LINE.match(line):
                out.append(f"metrics.prom:{n}: not valid Prometheus text exposition: {line[:60]}")
    idx = site / "search-ops.json"
    if idx.exists():
        try:
            items = json.loads(idx.read_text(encoding="utf-8"))
        except ValueError:
            items, _ = [], out.append("search-ops.json is not valid JSON")
        if not isinstance(items, list) or len(items) > 1000 or idx.stat().st_size > 400_000:
            out.append("search-ops.json is not a bounded list")
        else:
            for it in items:
                u = it.get("u") if isinstance(it, dict) else None
                if not isinstance(u, str) or not _SAFE_REL.match(u) or u.startswith("/") or ".." in u or ":" in u:
                    out.append(f"search-ops.json: unsafe or malformed link {str(u)[:60]!r}")
                    break
                tgt = u.split("#")[0]
                if tgt and not (site / tgt).exists():
                    out.append(f"search-ops.json: link target does not exist: {tgt}")
                    break
    for svg in sorted((site / "badges").glob("*.svg")) if (site / "badges").is_dir() else []:
        t = svg.read_text(encoding="utf-8").lower()
        if "<script" in t or "javascript:" in t or re.search(r"\son[a-z]+=", t):
            out.append(f"badges/{svg.name}: contains script or event handlers")
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    site = Path(args[0]) if args else Path("site")
    probs = check_site(site)
    for p in probs:
        print("PROBLEM", p)
    n = len(list(site.rglob("*.html"))) if site.exists() else 0
    print(f"checked {n} html page(s) in {site}: {len(probs)} problem(s)")
    return 1 if probs else 0


if __name__ == "__main__":
    raise SystemExit(main())
