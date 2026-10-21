#!/usr/bin/env python3
"""Check a generated site directory: required files, every relative link/src resolves (incl. #anchors), install pages are complete.

    python tools/check_site.py site            # exit 1 on any problem
"""
from __future__ import annotations

import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

REQUIRED = ["index.html", "404.html", ".nojekyll", "status.json", "assets/site.css", "assets/site.js", "install/index.html", "install/catalog.json",
            "install/bootstrap.ps1", "docs/index.html", "docs/search-index.json", "portal/index.html", "portal/admin.html", "dashboard/index.html",
            "program/index.html", "jobs/index.html", "machines/index.html", "queues/index.html", "schedules/index.html", "audit/index.html"]


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str, str]] = []   # (tag, attr, value)
        self.ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
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


def check_site(site: Path) -> list[str]:
    site = Path(site)
    problems: list[str] = []
    for rel in REQUIRED:
        if not (site / rel).exists():
            problems.append(f"missing required file: {rel}")
    pages = {p: parse(p) for p in sorted(site.rglob("*.html"))}
    for page, info in pages.items():
        rel = page.relative_to(site).as_posix()
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
                if parts.fragment not in tinfo.ids:
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
    return problems


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
