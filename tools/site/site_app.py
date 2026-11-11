"""Turn the generated per-page HTML into ONE single-page app: ``index.html`` (shell + one tab bar) + ``views/<tab>.json`` + a few assets.

The page builders (docs, install center, registry portal, dashboards, operator console) keep producing their pages exactly as before - into the
output directory, which acts as a staging area. ``assemble`` then

* extracts the main content of every page into a *view* (a build-time HTML string, already escaped by the page builders),
* rewrites every link to an in-app hash route (``#/jobs?status=failed``) or a site-root-relative file, so no tab ever leaves the app,
* moves page scripts into ``assets/views.js`` (they register with ``AS.hook`` and run when their view is mounted),
* scopes the standalone dashboard / program CSS under ``.dash`` so it cannot restyle the shell,
* replaces every old page URL with a tiny redirect stub (bookmarks, docs, issues and installers keep working),
* and writes the shell, the merged search index and ``assets/app.js``.

Routes:  ``#/<tab>``  ``#/install/<automation>``  ``#/docs/<DOC>``  ``#/catalog/a/<automation>``  ``#/catalog/compatibility|governance``  ``#/<tab>?key=value``
"""
from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path

import site_common as sc
from site_common import SiteConfig, esc, write

TABS = [("home", "Home"), ("install", "Install"), ("docs", "Docs"), ("catalog", "Catalog"), ("dashboard", "Dashboard"), ("program", "Program"),
        ("jobs", "Jobs"), ("machines", "Machines"), ("reliability", "Reliability"), ("errors", "Errors"), ("queues", "Queues"),
        ("schedules", "Schedules"), ("audit", "Audit"), ("admin", "Admin")]
TAB_NAMES = [t for t, _ in TABS]
SIMPLE = {"dashboard", "program", "jobs", "machines", "reliability", "errors", "queues", "schedules", "audit"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
KEEP_PAGES = {"404.html", "docs/assessment.html"}          # real pages that remain (the 404 page, the hand-authored assessment document)
SUBTABS = [("", "Automations"), ("compatibility", "Compatibility"), ("governance", "Governance")]


# ---------------------------------------------------------------------------------------------------------------------- routes
def route_of(rel: str):
    """(tab, sub) for a generated page path (posix, relative to the site root), or None."""
    p = rel.strip("/")
    if p in ("", "index.html"):
        return ("home", "")
    parts = p.split("/")
    if len(parts) == 2 and parts[1] == "index.html" and (parts[0] in SIMPLE or parts[0] in ("install", "docs")):
        return (parts[0], "")
    if p == "portal/index.html":
        return ("catalog", "")
    if p == "portal/admin.html":
        return ("admin", "")
    if p == "portal/compatibility.html":
        return ("catalog", "compatibility")
    if p == "portal/governance.html":
        return ("catalog", "governance")
    if len(parts) == 3 and parts[0] == "portal" and parts[1] == "automation" and parts[2].endswith(".html") and NAME_RE.match(parts[2][:-5]):
        return ("catalog", "a/" + parts[2][:-5])
    if len(parts) == 2 and parts[0] in ("install", "docs") and parts[1].endswith(".html") and NAME_RE.match(parts[1][:-5]):
        return (parts[0], parts[1][:-5])
    return None


def route_str(tab: str, sub: str = "") -> str:
    return "/" + tab + ("/" + sub if sub else "")


def _query(frag: str) -> str:
    """A fragment that looks like key=value pairs (old jobs deep links) becomes the route query; anything else is an anchor."""
    if not frag:
        return ""
    return frag if "=" in frag else "a=" + frag


def resolve(href: str, page_rel: str, own: tuple | None) -> str:
    """Rewrite one link found on ``page_rel`` (relative to the site root) for use inside the app."""
    h = href.strip()
    if not h or re.match(r"^(https?:|mailto:|data:|javascript:|//)", h, re.I):
        return href
    path, _, frag = h.partition("#")
    path, _, qs = path.partition("?")
    if not path:                                              # same-page anchor
        return "#" + route_str(*own) + ("?" + _query(frag) if frag else "") if own else href
    target = posixpath.normpath(posixpath.join(posixpath.dirname(page_rel), path))
    if target.startswith(".."):
        return href
    r = route_of(target)
    if r:
        query = "&".join(x for x in (qs, _query(frag)) if x)
        return "#" + route_str(*r) + ("?" + query if query else "")
    return target + ("?" + qs if qs else "") + ("#" + frag if frag else "")


_ATTR = re.compile(r'''\b(href|src|action)="([^"]*)"''')


def rewrite_links(html_text: str, page_rel: str, own: tuple | None) -> str:
    return _ATTR.sub(lambda m: f'{m.group(1)}="{esc_attr(resolve(_unesc(m.group(2)), page_rel, own))}"', html_text)


def _unesc(v: str) -> str:
    return v.replace("&amp;", "&").replace("&quot;", '"').replace("&#x27;", "'").replace("&lt;", "<").replace("&gt;", ">")


def esc_attr(v: str) -> str:
    return v.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------------------------------------------------------ extraction
_BODY = re.compile(r"<body[^>]*>(.*)</body>", re.S | re.I)
_SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)
_BANNER = re.compile(r'<div class="banner" role="note">.*?</div>|<div class="ok-note" role="note" style="margin-bottom:1rem">.*?</div>|<div class="demo-banner">.*?</div>', re.S)
_THEME_BTN = re.compile(r'<button[^>]*id="theme-toggle"[^>]*>.*?</button>', re.S)
_THEME_HANDLER = re.compile(r"document\.getElementById\('theme-toggle'\)\.addEventListener\('click',\(\)=>\{.*?\n\}\);\n?", re.S)
_STYLE = re.compile(r"<style>(.*?)</style>", re.S)


def _theme_scripts() -> set[str]:
    out = {sc.theme.THEME_HEAD_JS.strip(), sc.theme.THEME_TOGGLE_JS.strip()}
    try:
        from automation_logging import dashboard
        out.add(dashboard.THEME_JS.strip())
    except Exception:  # noqa: BLE001
        pass
    return out


def extract(page_html: str) -> dict:
    """{'html': main content, 'scripts': [inline script bodies that belong to the page], 'css': page-specific CSS or ''}"""
    m = _BODY.search(page_html)
    body = m.group(1) if m else page_html
    css = ""
    head = re.search(r"<head>(.*?)</head>", page_html, re.S)
    if 'id="__dash_data__"' in body and "<main" in body:        # the client-rendered fleet dashboard: keep its sub-tab strip, status strip, data block and panels
        a = body.index(">", body.index("<main")) + 1
        panels = body[a:body.rindex("</main>")]
        tabs = re.search(r'<div class="nav-tabs".*?</div>', body, re.S)
        gen = re.search(r'<span id="gentime"[^>]*></span>', body)
        strip = re.search(r'<div class="sstrip"[^>]*></div>', body)
        data = re.search(r'<script type="application/json" id="__dash_data__">.*?</script>', body, re.S)
        content = ('<nav class="dash-nav" aria-label="Dashboard sections">' + (tabs.group(0) if tabs else "") + (gen.group(0) if gen else "") + "</nav>"
                   + (strip.group(0) if strip else "") + (data.group(0) if data else "") + '<div class="main">' + panels + "</div>")
        kind = "dash"
        if head:
            css = "".join(_STYLE.findall(head.group(1)))
    elif "<main" in body:
        a = body.index(">", body.index("<main")) + 1
        content = body[a:body.rindex("</main>")]
        kind = "site"
    elif '<header class="top">' in body:
        content = body[body.index("</header>") + len("</header>"):]
        if '<footer class="foot">' in content:
            content = content[:content.index('<footer class="foot">')]
        kind = "portal"
    else:                                                      # standalone dashboard / program page
        content = re.sub(r"^\s*<div class=\"page\">", "", body.strip())
        content = re.sub(r"</div>\s*(?:<script\b.*?</script>\s*)*$", "", content, flags=re.S)
        kind = "dash"
        if head:
            css = "".join(_STYLE.findall(head.group(1)))
    theme = _theme_scripts()
    scripts: list[str] = []

    def take(sm: re.Match) -> str:
        attrs, code = sm.group("attrs"), sm.group("body")
        if re.search(r'type="application/json"', attrs):
            return sm.group(0)                                 # data block (admin panel): not executable, kept in the view
        code = _THEME_HANDLER.sub("", code)                    # the shell owns the theme button: a second handler (the dashboard has its own copy) would flip it twice
        if "src=" not in attrs and code.strip() and code.strip() not in theme and "autostation-theme" not in code[:200]:
            scripts.append(code.strip())
        return ""

    # page scripts usually sit AFTER the footer (outside the extracted content): collect them from the whole body, then drop them from the view
    for sm in _SCRIPT.finditer(body):
        take(sm)
    content = _SCRIPT.sub(lambda sm: sm.group(0) if re.search(r'type="application/json"', sm.group("attrs")) else "", content)
    content = _BANNER.sub("", content)
    content = _THEME_BTN.sub("", content)
    return {"html": content.strip(), "scripts": scripts, "css": css, "kind": kind}


# ------------------------------------------------------------------------------------------------------------------ CSS scoping
def _split_selectors(prelude: str) -> list[str]:
    out, depth, cur = [], 0, []
    for ch in prelude:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


_ROOT = re.compile(r"^(:root(?::not\([^)]*\)|\[[^\]]*\])*)\s*(.*)$")


def _scope_selector(sel: str, scope: str) -> str:
    if sel in ("body", ".page", "html"):
        return scope
    if sel == "*":
        return scope + " *"
    m = _ROOT.match(sel)
    if m:
        prefix, rest = m.group(1), m.group(2)
        if prefix == ":root" and not rest:
            return scope
        return prefix + " " + scope + (" " + rest if rest else "")
    return scope + " " + sel


def scope_css(css: str, scope: str = ".dash") -> str:
    """Prefix every rule of ``css`` with ``scope`` so a page's own stylesheet cannot leak into the shell (and the shell's cannot be overridden)."""
    out, i, n = [], 0, len(css)
    while i < n:
        j = css.find("{", i)
        if j < 0:
            break
        prelude = css[i:j].strip()
        depth, k = 1, j + 1
        while k < n and depth:
            depth += (css[k] == "{") - (css[k] == "}")
            k += 1
        body = css[j + 1:k - 1]
        if prelude.startswith(("@media", "@supports")):
            out.append(prelude + "{" + scope_css(body, scope) + "}")
        elif prelude.startswith("@"):
            out.append(prelude + "{" + body + "}")
        else:
            if prelude in ("body", "html", ".page"):          # the app shell owns page padding and width (full width), not the view's own stylesheet
                body = re.sub(r"padding\s*:[^;]*;?", "", body)
                if prelude == ".page":
                    body = re.sub(r"(?:max-width|margin)\s*:[^;]*;?", "", body)
            out.append(", ".join(_scope_selector(s, scope) for s in _split_selectors(prelude)) + "{" + body + "}")
        i = k
    return "".join(out)


# ------------------------------------------------------------------------------------------------------------------ pieces
def _subtabs(sub: str) -> str:
    items = []
    for key, label in SUBTABS:
        on = (sub == key) or (key == "" and sub.startswith("a/"))
        items.append(f'<a href="#/catalog{("/" + key) if key else ""}"{" aria-current=page class=on" if on else ""}>{esc(label)}</a>')
    return f'<nav class="subtabs" aria-label="Catalog sections">{"".join(items)}</nav>'


def _redirect_stub(rel: str, tab: str, sub: str) -> str:
    depth = rel.count("/")
    up = "../" * depth
    to = f"{up}index.html"
    route = route_str(tab, sub)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'self\'; base-uri \'none\'; form-action \'none\'">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Moved - AutoStation</title>'
            f'<noscript><meta http-equiv="refresh" content="0;url={esc(to)}#{esc(route)}"></noscript></head><body>'
            f'<p>This page moved into the AutoStation app: <a href="{esc(to)}#{esc(route)}">open it</a>.</p>'
            f'<script src="{up}assets/redirect.js" data-to="{esc(to)}" data-route="{esc(route)}"></script></body></html>\n')


REDIRECT_JS = r"""/* Old page URL -> the same view inside the single-page app. Keeps bookmarks and links in docs/issues working. */
(function () {
  'use strict';
  var s = document.currentScript; if (!s) return;
  var to = s.getAttribute('data-to') || 'index.html', route = s.getAttribute('data-route') || '/home';
  if (!/^(\.\.\/)*index\.html$/.test(to) || !/^\/[A-Za-z0-9_\-\/.~]*$/.test(route)) return;
  var frag = (location.hash || '').replace(/^#/, '');
  var q = frag ? (/^[A-Za-z0-9_.\-]+=/.test(frag) ? '?' + frag : '?a=' + encodeURIComponent(frag)) : '';
  location.replace(new URL(to, location.href).href + '#' + route + q);
})();
"""

APP_CSS = r"""
.tabs { display:flex; gap:.15rem; overflow-x:auto; position:sticky; top:0; z-index:10; background:var(--bg); border-bottom:1px solid var(--border); margin:-.4rem 0 1rem; padding-top:.3rem; scrollbar-width:thin; }
.tabs a { padding:.5rem .8rem; white-space:nowrap; color:var(--fg-muted); border-bottom:2px solid transparent; font-size:13px; }
.tabs a:hover { color:var(--fg); text-decoration:none; background:var(--surface); }
.tabs a[aria-selected="true"] { color:var(--accent); border-bottom-color:var(--accent); font-weight:600; }
.tabs a:focus-visible, .subtabs a:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
header.top .tools { margin-left:auto; display:flex; align-items:center; gap:.4rem; flex-wrap:wrap; }
#view { outline:none; min-height:50vh; }
#view[aria-busy="true"] { opacity:.6; }
.subtabs { display:flex; gap:.3rem; margin:0 0 1rem; flex-wrap:wrap; }
.subtabs a { padding:.25rem .7rem; border:1px solid var(--border); border-radius:999px; font-size:12px; color:var(--fg-muted); }
.subtabs a.on { background:var(--accent-dim); color:var(--accent); border-color:var(--accent); }
.embed { width:100%; height:80vh; border:1px solid var(--border); border-radius:8px; background:var(--surface); }
.dash { padding:0; }
/* the fleet dashboard brings its own sub-tab strip and status strip, built to be fixed to the viewport: in the app they sit in the flow under the shell's tab bar, full width */
#view .dash { --nh:44px; --sh:34px; }
#view .dash nav.dash-nav { position:static; top:auto; left:auto; right:auto; height:var(--nh); padding-inline:.5rem; border:1px solid var(--bd); border-radius:8px 8px 0 0; z-index:1; }
#view .dash .sstrip { position:static; top:auto; left:auto; right:auto; height:auto; min-height:var(--sh); padding:.3rem .75rem; margin:0 0 1rem; border:1px solid var(--bd); border-top:0; border-radius:0 0 8px 8px; }
#view .dash .main { padding:0 0 2rem; max-width:none; margin:0; }
.status-banner { display:flex; align-items:center; gap:.7rem; padding:.7rem 1rem; border-radius:8px; border:1px solid var(--border); border-left-width:4px; margin:0 0 1rem; background:var(--surface); }
.status-banner.green { border-left-color:var(--green); } .status-banner.amber { border-left-color:var(--yellow); } .status-banner.red { border-left-color:var(--red); } .status-banner.grey { border-left-color:var(--fg-dim); }
.status-banner b { font-size:1rem; }
.rowlink { display:grid; grid-template-columns:7rem 1fr auto; gap:.6rem; align-items:center; padding:.4rem .6rem; border:1px solid var(--border); border-radius:6px; margin:.3rem 0; background:var(--surface); color:var(--fg); }
/* FULL WIDTH: the tab bar, header and every view span the viewport; long prose keeps a readable measure inside the wide container */
body { padding-left:clamp(1rem,2.5vw,2.5rem); padding-right:clamp(1rem,2.5vw,2.5rem); }
.page { max-width:none; margin:0; width:100%; }
#view [id] { scroll-margin-top:3.6rem; }
.tw { overflow-x:auto; max-width:100%; }
.md p, .md li, .md blockquote, .hero p, .summary, .panel > p, .warnbox, .steps li > div, .panel li, p.sub, .banner { max-width:90ch; }
.docs-layout { grid-template-columns:minmax(210px,270px) minmax(0,1fr); gap:clamp(1rem,2vw,2.5rem); }
.docs-nav { top:3.6rem; }
.md pre, .md table { max-width:100%; }
.grid { grid-template-columns:repeat(auto-fill,minmax(min(100%,340px),1fr)); }
.split > #drawer { margin:1rem 0 0; }
@media (min-width:1100px) {
  .split:has(> #drawer:not([hidden])) { display:grid; grid-template-columns:minmax(0,1fr) minmax(22rem,30rem); gap:1rem; align-items:start; }
  .split > #drawer { position:sticky; top:3.6rem; max-height:calc(100vh - 4.6rem); overflow:auto; margin:0; }
}
@media (min-width:1800px) { .tiles { grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); } }
@media (max-width:760px) { header.top .tools { margin-left:0; width:100%; } .gs input { width:100%; max-width:none; } .gs { display:block; margin:0; width:100%; flex:1; } }
"""


def build_id(cfg: SiteConfig) -> str:
    import hashlib
    return hashlib.sha256(f"{cfg.built_at}|{cfg.commit}".encode("utf-8")).hexdigest()[:12]


def shell(cfg: SiteConfig, status: dict | None = None) -> str:
    links = "".join(f'<a id="tab-{k}" role="tab" data-tab="{k}" href="#/{k}" aria-selected="false" tabindex="-1">{esc(label)}</a>' for k, label in TABS)
    csp = sc.csp_meta(sc.theme.THEME_HEAD_JS)
    bid = build_id(cfg)
    banner = ""
    if cfg.demo:
        banner = ('<div class="demo-banner"><strong>DEMO DATA.</strong> Dashboards on this site are generated from synthetic fleet history until live '
                  "analytics is enabled (see docs/PIPELINES.md).</div>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
{csp}
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AutoStation</title>
<meta name="description" content="AutoStation: install, build, operate and govern unattended automations - scripted, governed, self-healing.">
<meta name="build" content="{bid}">
<script>{sc.theme.THEME_HEAD_JS}</script>
<link rel="stylesheet" href="assets/site.css?v={bid}">
</head><body><div class="page">
<a class="sr" href="#view" data-skip>Skip to content</a>
<header class="top">
  <div class="brand">{sc.theme.LOGO_SVG}<div><div class="eyebrow">{esc(cfg.title)} &middot; unattended automation platform</div><div style="font-weight:700">AutoStation</div></div></div>
  <div class="tools"><form id="gs-form" class="gs" role="search"><input id="gsearch" type="search" maxlength="80" placeholder="Search automations, runs, errors, docs  ( / )" aria-label="Search automations, runs, errors and docs" autocomplete="off"><ul id="gs-results" hidden></ul></form><button id="theme-toggle" type="button" title="Toggle light/dark theme" aria-label="Toggle theme">Theme</button></div>
</header>
<nav id="tabs" class="tabs" role="tablist" aria-label="Sections">{links}</nav>
{sc.TELEMETRY_BANNER}{banner}
<main id="view" role="tabpanel" tabindex="-1"><noscript><p>AutoStation is a static single-page app and needs JavaScript. The registry and installers do not: <a href="registry/manifest.yaml">registry/manifest.yaml</a>.</p></noscript><p>Loading&hellip;</p></main>
<footer class="foot">{esc(cfg.title)} &middot; built {esc(cfg.built_at)} {('&middot; commit ' + esc(cfg.commit)) if cfg.commit else ''} &middot;
<a href="{esc(cfg.repo_url)}">{esc(cfg.repo)}</a> &middot; <a href="status.json">status.json</a> &middot; static site: no server, no tracking.</footer>
</div><script src="assets/app.js?v={bid}"></script><script src="assets/jobs.js?v={bid}"></script><script src="assets/views.js?v={bid}"></script></body></html>
"""


def page_404(cfg: SiteConfig) -> str:
    home = cfg.pages_url
    body = ('<h1>Page not found</h1><p>That page does not exist (or has not been built yet). Go to the '
            f'<a href="{esc(home)}">AutoStation home</a> - the install center, docs, catalog and dashboards are all tabs on that one page.</p>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">{sc.csp_meta(sc.theme.THEME_HEAD_JS)}'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Not found - AutoStation</title>'
            f'<script>{sc.theme.THEME_HEAD_JS}</script><link rel="stylesheet" href="{esc(home)}assets/site.css"></head>'
            f'<body><div class="page"><main>{body}</main></div></body></html>\n')


# ------------------------------------------------------------------------------------------------------------------ assemble
def assemble(out: Path, cfg: SiteConfig, manifest: dict | None = None, docs_info: dict | None = None) -> dict:
    out = Path(out)
    views: dict[str, dict[str, str]] = {t: {} for t in TAB_NAMES}
    hooks: list[tuple[str, str]] = []
    stubs: list[tuple[str, str, str]] = []
    unknown: list[str] = []
    for f in sorted(out.rglob("*.html")):
        rel = f.relative_to(out).as_posix()
        if rel in KEEP_PAGES or rel.startswith("registry/") or "/evidence/" in rel:
            continue
        r = route_of(rel)
        text = f.read_text(encoding="utf-8")
        if not r:
            unknown.append(rel)
            continue
        tab, sub = r
        ex = extract(text)
        html_text = rewrite_links(ex["html"], rel, r)
        if ex["css"]:
            html_text = f"<style>{scope_css(ex['css'])}</style><div class=\"dash\">{html_text}</div>"
        if tab == "catalog":
            html_text = _subtabs(sub) + html_text
        views[tab][sub] = html_text
        key = tab + ("/" + sub if sub else "/")
        for code in ex["scripts"]:
            hooks.append((key, code))
        stubs.append((rel, tab, sub))

    # the hand-authored assessment document stays a real file and is shown inside the Docs tab
    if (out / "docs" / "assessment.html").is_file() and "" in views["docs"]:
        m = re.search(r'<nav class="docs-nav".*?</nav>', views["docs"][""], re.S)
        nav = m.group(0).replace(' class="on"', "") if m else ""
        nav = nav.replace('<a href="#/docs/assessment">', '<a href="#/docs/assessment" class="on">')
        views["docs"]["assessment"] = ('<div class="crumbs"><a href="#/home">Home</a> / <a href="#/docs">Docs</a> / Architecture assessment</div>'
                                       f'<div class="docs-layout">{nav}<div><iframe class="embed" src="docs/assessment.html" title="Architecture assessment" '
                                       'loading="lazy" sandbox="allow-scripts"></iframe></div></div>')

    # redirect stubs replace every old page URL; unknown pages (should not exist) are reported and also redirected home
    for rel, tab, sub in stubs:
        write(out, rel, _redirect_stub(rel, tab, sub))
    for rel in unknown:
        sc.BUILD_WARNINGS.append(f"unmapped page {rel} redirected to #/home")
        write(out, rel, _redirect_stub(rel, "home", ""))

    for tab in TAB_NAMES:
        write(out, f"views/{tab}.json", json.dumps(views[tab], separators=(",", ":"), ensure_ascii=True, sort_keys=True))

    # view scripts: each registers a hook that runs after its view is mounted
    parts = ["/* generated: scripts that belong to individual views (see tools/site/site_app.py). Each runs after its view is inserted. */"]
    for key, code in hooks:
        parts.append(f"AS.hook({json.dumps(key)}, function () {{\n{code}\n}});")
    static = (Path(__file__).parent / "views_static.js").read_text(encoding="utf-8")
    write(out, "assets/views.js", "\n".join(parts) + "\n" + static)
    write(out, "assets/redirect.js", REDIRECT_JS)
    write(out, "assets/app.js", (Path(__file__).parent / "app.js").read_text(encoding="utf-8"))
    write(out, "assets/jobs.js", (Path(__file__).parent / "jobs.js").read_text(encoding="utf-8"))   # always present: the shell loads it
    css = out / "assets" / "site.css"
    if css.is_file() and APP_CSS not in css.read_text(encoding="utf-8"):
        write(out, "assets/site.css", css.read_text(encoding="utf-8") + APP_CSS)
    for old in ("assets/gsearch.js", "assets/site.js"):
        (out / old).unlink(missing_ok=True)

    merge_search(out, manifest or {}, docs_info or {})
    write(out, "index.html", shell(cfg))
    write(out, "404.html", page_404(cfg))
    return {"views": {t: sorted(v) for t, v in views.items()}, "stubs": [s[0] for s in stubs] + unknown}


def merge_search(out: Path, manifest: dict, docs_info: dict) -> None:
    """search-ops.json -> in-app routes, plus docs sections and the catalog; entries that map to no route are dropped."""
    p = out / "search-ops.json"
    try:
        items = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []
    except ValueError:
        items = []
    fixed = []
    for it in items if isinstance(items, list) else []:
        u = str(it.get("u", ""))
        if u.startswith("/"):
            fixed.append(it)
            continue
        path, _, frag = u.partition("#")
        r = route_of(posixpath.normpath(path))
        if r:
            fixed.append({**it, "u": route_str(*r) + ("?" + _query(frag) if frag else "")})
    seen = {it["u"] for it in fixed}
    for name, e in (manifest.get("automations") or {}).items():
        if NAME_RE.match(str(name)):
            u = f"/catalog/a/{name}"
            if u not in seen:
                fixed.append({"t": "catalog", "n": str(name), "s": sc_clean((e or {}).get("description") or "automation", 100), "u": u})
    sidx = out / "docs" / "search-index.json"
    try:
        entries = json.loads(sidx.read_text(encoding="utf-8")) if sidx.is_file() else []
    except ValueError:
        entries = []
    for e in entries[:1500]:
        doc = str(e.get("doc", ""))[:-5]
        anchor = str(e.get("anchor", ""))
        if NAME_RE.match(doc) and re.match(r"^[A-Za-z0-9._-]*$", anchor):
            fixed.append({"t": "doc", "n": sc_clean(f"{e.get('title', doc)} - {e.get('heading', '')}", 110), "s": sc_clean(e.get("text", ""), 100),
                          "u": f"/docs/{doc}" + (f"?a={anchor}" if anchor else "")})
    write(out, "search-ops.json", json.dumps(fixed[:2000], separators=(",", ":"), sort_keys=True))


def sc_clean(v, n: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(v or "")).strip()[:n]
