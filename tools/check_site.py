#!/usr/bin/env python3
"""Check a generated site directory (the single-page app): required files, one shell with one tab bar, every view's links resolve to a view or a file,
nothing executable or external inside views, every other HTML file is a redirect stub, install views are complete, observability files are sane.

    python tools/check_site.py site            # exit 1 on any problem
"""
from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

TABS = ["home", "install", "docs", "catalog", "dashboard", "program", "jobs", "machines", "reliability", "errors", "queues", "schedules", "audit", "admin"]
REQUIRED = (["index.html", "404.html", ".nojekyll", "status.json", "assets/site.css", "assets/app.js", "assets/views.js", "assets/jobs.js", "assets/redirect.js",
             "install/catalog.json", "install/bootstrap.ps1", "docs/search-index.json", "docs/assessment.html", "metrics.json", "metrics.prom",
             "fleet-status.json", "search-ops.json", "badges/fleet.svg"] + [f"views/{t}.json" for t in TABS])
REAL_PAGES = {"index.html", "404.html", "docs/assessment.html"}      # every other .html must be a tiny redirect stub into the app
ROUTE_RE = re.compile(r"^/(" + "|".join(TABS) + r")(/[A-Za-z0-9_.\-/]+)?(\?[A-Za-z0-9_.\-=&%~]*)?$")
TAB_LINK = re.compile(r'<a id="tab-([a-z-]+)" role="tab" data-tab="([a-z-]+)" href="#/([a-z-]+)"')


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str, str]] = []   # (tag, attr, value)
        self.ids: set[str] = set()
        self.csp = ""                                  # content of the CSP <meta>
        self.inline_scripts: list[str] = []            # bodies of executable inline <script> blocks
        self.handlers: list[str] = []                  # inline event-handler attributes (onclick=...) anywhere in the page
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
        self.handlers.extend(f"<{tag} {k}=...>" for k in a if k.lower().startswith("on"))
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
    if not m:
        return []
    if "'unsafe-inline'" in m.group(1):
        return [f"{rel}: script-src allows 'unsafe-inline' (scripts must be allowed by SHA-256 only)"]
    if info.handlers:
        return [f"{rel}: inline event handler {info.handlers[0]} would be blocked by the CSP; use addEventListener"]
    allowed = set(re.findall(r"'sha256-([A-Za-z0-9+/=]+)'", m.group(1)))
    out = []
    for body in info.inline_scripts:
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
        if digest not in allowed:
            out.append(f"{rel}: an inline script is not covered by the page's CSP hashes (it would be blocked, or it was injected): {body.strip()[:60]!r}")
    return out


def _load_views(site: Path, problems: list[str]) -> dict:
    views: dict = {}
    for t in TABS:
        f = site / "views" / f"{t}.json"
        if not f.is_file():
            continue
        try:
            v = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            problems.append(f"views/{t}.json is not valid JSON")
            continue
        if not isinstance(v, dict) or not all(isinstance(k, str) and isinstance(x, str) for k, x in v.items()):
            problems.append(f"views/{t}.json is not a map of strings")
            continue
        if not v.get("", "").strip():
            problems.append(f"views/{t}.json has no main view")
        views[t] = v
    return views


def _route_exists(route: str, views: dict) -> bool:
    if not ROUTE_RE.match(route):
        return False
    path = route.split("?")[0].strip("/").split("/")
    tab, sub = path[0], "/".join(path[1:])
    return tab in views and sub in views[tab]


def _check_view(tab: str, sub: str, html_text: str, site: Path, views: dict) -> list[str]:
    name = f"views/{tab}.json[{sub or 'main'}]"
    info = _Page()
    info.feed(html_text)
    out: list[str] = []
    if info.inline_scripts:
        out.append(f"{name}: executable inline <script> in a view (scripts belong in assets/views.js)")
    if info.handlers:
        out.append(f"{name}: inline event handler {info.handlers[0]}")
    for tag, attr, val in info.links:
        v = val.strip()
        if not v or v.startswith(("mailto:", "tel:")):
            continue
        if v.startswith("http://"):
            out.append(f"{name}: insecure http:// link {v[:60]}")
        elif v.startswith("https://"):
            if tag != "a":
                out.append(f"{name}: external {tag} {v[:60]} (the app loads nothing from outside its own origin)")
        elif v.lower().startswith(("javascript:", "data:")):
            out.append(f"{name}: {v.split(':')[0]}: URL")
        elif v.startswith("#"):
            if not v.startswith("#/") or not _route_exists(v[1:], views):
                out.append(f"{name}: link to a route that does not exist: {v[:80]}")
        else:
            path = unquote(urlsplit(v).path)
            tgt = (site / path).resolve()
            if site.resolve() not in tgt.parents and tgt != site.resolve():
                out.append(f"{name}: link escapes the site: {v[:80]}")
            elif not tgt.exists() and not path.startswith(("registry/", "dashboard/evidence/")):
                out.append(f"{name}: broken link {v[:80]}")
    return out


def _check_stub(rel: str, text: str, views: dict) -> list[str]:
    m = re.search(r'data-route="([^"]*)"', text)
    out = []
    route = unquote(m.group(1)).replace("&amp;", "&") if m else ""
    if not m or not _route_exists(route, views):
        out.append(f"{rel}: redirect stub points at a route that does not exist ({m.group(1) if m else 'no data-route'})")
    if len(text) > 2500 or "<nav" in text or "<header" in text:
        out.append(f"{rel}: not a tiny redirect stub - every page except index/404/assessment must redirect into the app")
    return out


def check_site(site: Path) -> list[str]:
    site = Path(site)
    problems: list[str] = []
    for rel in REQUIRED:
        if not (site / rel).exists():
            problems.append(f"missing required file: {rel}")
    views = _load_views(site, problems)
    # ---- the shell: one header, one tab bar with exactly the tabs of the route table, hash-only CSP -------------------------------
    shell = site / "index.html"
    if shell.is_file():
        info = parse(shell)
        problems += _check_inline_scripts("index.html", info)
        if not info.csp:
            problems.append("index.html: no Content-Security-Policy")
        text = shell.read_text(encoding="utf-8")
        found = [t[0] for t in TAB_LINK.findall(text) if t[0] == t[1] == t[2]]
        if found != TABS:
            problems.append(f"index.html: tab bar {found} does not match the route table {TABS}")
        if text.count('id="tabs"') != 1 or text.count('<header class="top">') != 1:
            problems.append("index.html: the app must have exactly one header and one tab bar")
        for tag, attr, val in info.links:
            if val.startswith(("http://", "https://")) and tag in ("script", "link", "iframe", "img"):
                problems.append(f"index.html: external {tag} {val[:60]}")
    # ---- views ----------------------------------------------------------------------------------------------------------------------
    for tab, v in views.items():
        for sub, html_text in v.items():
            problems += _check_view(tab, sub, html_text, site, views)
    # ---- every other HTML file is a redirect stub (or the 404 / the assessment document) ----------------------------------------------
    for page in sorted(site.rglob("*.html")):
        rel = page.relative_to(site).as_posix()
        if rel in REAL_PAGES or rel.startswith("registry/") or "/evidence/" in rel:
            continue
        problems += _check_stub(rel, page.read_text(encoding="utf-8", errors="replace"), views)
    ass = site / "docs" / "assessment.html"
    if ass.is_file():
        problems += _check_inline_scripts("docs/assessment.html", parse(ass))
        if re.search(r"https?://(fonts\.|cdn|unpkg)", ass.read_text(encoding="utf-8", errors="replace")):
            problems.append("docs/assessment.html loads something from outside the site (fonts/CDN)")
    # ---- install views ---------------------------------------------------------------------------------------------------------------
    cat_path = site / "install" / "catalog.json"
    if cat_path.exists():
        try:
            cat = json.loads(cat_path.read_text(encoding="utf-8"))
        except ValueError:
            cat = {}
            problems.append("install/catalog.json is not valid JSON")
        for name, entry in (cat.get("automations") or {}).items():
            text = (views.get("install") or {}).get(name)
            if text is None:
                problems.append(f"install view missing for {name}")
                continue
            url = (entry.get("installer") or {}).get("url", "")
            if not url or url not in text:
                problems.append(f"install view for {name} has no download link to {url or '(no installer url)'}")
            if "SHA-256" not in text:
                problems.append(f"install view for {name} has no SHA-256 block")
            if "Get-FileHash" not in text:
                problems.append(f"install view for {name} has no verify instructions")
    status = site / "status.json"
    if status.exists():
        try:
            json.loads(status.read_text(encoding="utf-8"))
        except ValueError:
            problems.append("status.json is not valid JSON")
    problems += check_observability(site, views)
    return problems


_PROM_LINE = re.compile(r'^(#( HELP| TYPE) [a-z_:][a-z0-9_:]* .+|[a-z_:][a-z0-9_:]*(\{([a-z_][a-z0-9_]*="([^"\\]|\\.)*"(,|(?=\})))*\})? -?[0-9.eE+-]+)$')
_SAFE_REL = re.compile(r"^[A-Za-z0-9_\-/.#=&%?~]+$")


def check_observability(site: Path, views: dict | None = None) -> list[str]:
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
        if not isinstance(items, list) or len(items) > 2000 or idx.stat().st_size > 800_000:
            out.append("search-ops.json is not a bounded list")
        else:
            for it in items:
                u = it.get("u") if isinstance(it, dict) else None
                if not isinstance(u, str) or not _SAFE_REL.match(u) or ".." in u or ":" in u or u.startswith("//"):
                    out.append(f"search-ops.json: unsafe or malformed link {str(u)[:60]!r}")
                    break
                if u.startswith("/"):                      # an in-app route (the published form): it must exist
                    if views and not _route_exists(u, views):
                        out.append(f"search-ops.json: link target is not a route of the app: {u[:80]}")
                        break
                else:                                      # a staged page path (before the app is assembled): the page must exist
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
    print(f"checked {n} html file(s) in {site}: {len(probs)} problem(s)")
    return 1 if probs else 0


if __name__ == "__main__":
    raise SystemExit(main())
