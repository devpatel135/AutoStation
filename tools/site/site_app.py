"""Turn the generated per-page HTML into ONE single-page app: ``index.html`` (shell + one tab bar) + ``views/<tab>.json`` + a few assets.

The page builders (docs, install center, registry portal, dashboards, operator console) keep producing their pages exactly as before - into the
output directory, which acts as a staging area. ``assemble`` then

* extracts the main content of every page into a *view* (a build-time HTML string, already escaped by the page builders),
* rewrites every link to an in-app hash route (``#/jobs?status=failed``) or a site-root-relative file, so no tab ever leaves the app,
* moves page scripts into ``assets/views.js`` (they register with ``AS.hook`` and run when their view is mounted),
* scopes the standalone dashboard / program CSS under ``.dash`` so it cannot restyle the shell,
* replaces every old page URL with a tiny redirect stub (bookmarks, docs, issues and installers keep working),
* and writes the shell, the merged search index and ``assets/app.js``.

Routes (exactly four tabs):  ``#/home``  ``#/install[/<automation>]``  ``#/fleet[/<section>|/a/<automation>]``  ``#/docs[/<DOC>]``  ``?key=value``
Fleet is ONE page: ``#/fleet/runs`` scrolls to its Runs section, ``#/fleet/a/<name>`` opens that automation's row. Install is one page:
``#/install/<name>`` opens that automation's details. Docs has one view per document. Every OLD route and page URL (jobs, machines, catalog,
dashboard, admin ...) is mapped to its place in these four by ``route_of`` / ``OLD_TABS`` (redirect stubs for pages, a router alias for hashes).
"""
from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path

import site_common as sc
import site_simple
from site_common import SiteConfig, esc, write

TABS = [("home", "Home"), ("install", "Install"), ("fleet", "Fleet"), ("docs", "Docs")]
TAB_NAMES = [t for t, _ in TABS]
SINGLE = ("fleet", "install")            # one scrolling view each; the route's second part only focuses something inside it
LAZY_VIEWS = ("runs",)                    # views/<name>.json fetched when its host scrolls into view (the Runs table can be 2000 rows)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
KEEP_PAGES = {"404.html", "docs/assessment.html"}          # real pages that remain (the 404 page, the hand-authored assessment document)
# the pages the builders still produce in the staging area, and the Fleet section each one belongs to
FLEET_OF = {"dashboard": "problems", "program": "value", "jobs": "runs", "machines": "machines", "reliability": "problems", "errors": "problems",
            "queues": "problems", "schedules": "schedules", "audit": "releases"}
PORTAL_OF = {"index": "automations", "admin": "admin", "overview": "", "health": "problems", "fleet": "machines", "audit": "releases",
             "compatibility": "automations"}
# old hash routes (#/jobs, #/catalog/a/x ...) -> the new route; the router applies the same table (see OLD_TABS in app.js)
OLD_TABS = {"jobs": "fleet/runs", "machines": "fleet/machines", "catalog": "fleet/automations", "dashboard": "fleet/problems", "program": "fleet/value",
            "reliability": "fleet/problems", "errors": "fleet/problems", "queues": "fleet/problems", "schedules": "fleet/schedules",
            "audit": "fleet/releases", "admin": "fleet/admin"}


# ---------------------------------------------------------------------------------------------------------------------- routes
def page_key(rel: str):
    """Identity of a staged page (what it is, not where it lands), or None for a path that is not a generated page."""
    p = rel.strip("/")
    if p in ("", "index.html"):
        return "home"
    parts = p.split("/")
    if len(parts) == 2 and parts[1] == "index.html" and (parts[0] in FLEET_OF or parts[0] in ("install", "docs")):
        return parts[0]
    if len(parts) == 2 and parts[0] == "portal" and parts[1].endswith(".html") and parts[1][:-5] in PORTAL_OF | {"governance": ""}:
        return "portal-" + parts[1][:-5]
    if len(parts) == 3 and parts[0] == "portal" and parts[1] == "automation" and parts[2].endswith(".html") and NAME_RE.match(parts[2][:-5]):
        return "portal-auto:" + parts[2][:-5]
    if len(parts) == 2 and parts[0] in ("install", "docs") and parts[1].endswith(".html") and NAME_RE.match(parts[1][:-5]):
        return parts[0] + ":" + parts[1][:-5]
    return None


def route_of(rel: str):
    """(tab, sub) an old generated page path (posix, relative to the site root) lands on in the app, or None."""
    key = page_key(rel)
    if key is None:
        return None
    if key == "home":
        return ("home", "")
    if key in FLEET_OF:
        return ("fleet", FLEET_OF[key])
    if key in ("install", "docs"):
        return (key, "")
    if key.startswith("portal-auto:"):
        return ("fleet", "a/" + key.split(":", 1)[1])
    if key == "portal-governance":
        return ("docs", "GOVERNANCE")
    if key.startswith("portal-"):
        return ("fleet", PORTAL_OF.get(key[7:], ""))
    tab, _, name = key.partition(":")
    return (tab, name)


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
_PORTAL_STRIP = re.compile(r'<div class="status-strip">.*?<div class="fleet-badge[^>]*>.*?</div></div>', re.S)
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
    elif '<nav class="site-nav"' in body and '<div class="page">' in body:   # the registry portal's own pages (own top bar + status strip): keep strip and body, drop the bar
        a = body.index(">", body.index('<div class="page">')) + 1
        b = body.index('<footer class="foot">', a) if '<footer class="foot">' in body[a:] else body.rindex("</div>")
        inner = body[a:b]
        strip = re.search(r'<div class="status-strip">.*?<div class="fleet-badge[^>]*>.*?</div></div>', body, re.S)
        content = (strip.group(0) if strip else "") + inner
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
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)           # comments: one in front of a selector would otherwise become part of it
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
:root { --tabs-h:2.9rem; }
.tabs { display:flex; gap:.2rem; overflow-x:auto; position:sticky; top:0; z-index:10; background:var(--bg); border-bottom:1px solid var(--border); margin:-.4rem 0 1.2rem; padding-top:.3rem; scrollbar-width:thin; }
.tabs a { padding:.7rem 1.2rem; white-space:nowrap; color:var(--fg-muted); border-bottom:2px solid transparent; font-size:15px; }
.tabs a:hover { color:var(--fg); text-decoration:none; background:var(--surface); }
.tabs a[aria-selected="true"] { color:var(--accent); border-bottom-color:var(--accent); font-weight:600; }
.tabs a:focus-visible, .jump a:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
header.top .tools { margin-left:auto; display:flex; align-items:center; gap:.4rem; flex-wrap:wrap; }
#view { outline:none; min-height:50vh; }
#view[aria-busy="true"] { opacity:.6; }
.embed { width:100%; height:80vh; border:1px solid var(--border); border-radius:8px; background:var(--surface); }
.dash { padding:0; }
.status-banner { display:flex; align-items:center; gap:.7rem; padding:.9rem 1.1rem; border-radius:8px; border:1px solid var(--border); border-left-width:5px; margin:0 0 1rem; background:var(--surface); }
.status-banner.green { border-left-color:var(--green); } .status-banner.amber { border-left-color:var(--yellow); } .status-banner.red { border-left-color:var(--red); } .status-banner.grey { border-left-color:var(--fg-dim); }
.status-banner b { font-size:1.25rem; }
.lead { color:var(--fg-muted); font-size:15px; margin:.2rem 0 .8rem; }
.advisory { font-size:12px; color:var(--fg-dim); margin:.4rem 0 1rem; }
/* FULL WIDTH: the tab bar, header and every view span the viewport; long prose keeps a readable measure inside the wide container */
body { padding-left:clamp(1rem,2.5vw,2.5rem); padding-right:clamp(1rem,2.5vw,2.5rem); }
.page { max-width:none; margin:0; width:100%; padding:0 0 40px; }   /* the shared portal theme reserves room for a fixed top bar this app does not have */
header.top { display:flex; align-items:center; gap:1rem; flex-wrap:wrap; padding:.9rem 0 .7rem; }
header.top .brand { display:flex; align-items:center; gap:.7rem; min-width:0; }
header.top .brand svg { width:2.3rem; height:auto; flex:none; }
/* The train chugs in place, constantly: wheels turn, steam puffs rise and fade, the body bobs, the rail ties slide under it.
   Pure CSS (no script, no external file); anyone with 'reduce motion' set gets the still logo. */
.train .tr-wheel { transform-box:fill-box; transform-origin:center; animation:tr-spin .8s linear infinite; }
.train .tr-body { animation:tr-bob .4s ease-in-out infinite alternate; }
.train .tr-puff { transform-box:fill-box; transform-origin:center; opacity:0; animation:tr-puff 2.4s ease-out infinite; }
.train .tr-puff.p2 { animation-delay:.8s; }
.train .tr-puff.p3 { animation-delay:1.6s; }
.train .tr-rail { animation:tr-rail .3s linear infinite; }
@keyframes tr-spin { to { transform:rotate(360deg); } }
@keyframes tr-bob { from { transform:translateY(0); } to { transform:translateY(-.4px); } }
@keyframes tr-puff { 0% { opacity:0; transform:translate(0,0) scale(.5); } 15% { opacity:.75; } 100% { opacity:0; transform:translate(5px,-9px) scale(1.7); } }
@keyframes tr-rail { to { stroke-dashoffset:-4.4; } }
@media (prefers-reduced-motion: reduce) { .train .tr-wheel, .train .tr-body, .train .tr-puff, .train .tr-rail { animation:none; } .train .tr-puff { opacity:0; } }
header.top .eyebrow { font:600 10px var(--mono); letter-spacing:.12em; text-transform:uppercase; color:var(--accent); }
#view [id], #view tr[data-auto] { scroll-margin-top:7rem; }
.tw { overflow-x:auto; max-width:100%; }
.md p, .md li, .md blockquote, .panel > p, .warnbox, .steps li > div, .panel li, p.sub { max-width:90ch; }
/* Home */
.home h1, .install h1 { font-size:1.9rem; margin:.2rem 0 .4rem; }
.quick { display:flex; gap:.7rem; flex-wrap:wrap; margin:1.2rem 0; }
.quick .btn { padding:.7rem 1.4rem; font-size:15px; }
.fleet-now .tiles { margin:.6rem 0 1rem; }
.attn { list-style:none; margin:.4rem 0 0; padding:0; }
.attn li { display:flex; gap:.6rem; align-items:baseline; padding:.45rem .6rem; border:1px solid var(--border); border-radius:6px; margin:.3rem 0; background:var(--surface); }
/* Install */
.iitem { border:1px solid var(--border); border-radius:10px; background:var(--surface); padding:1rem 1.2rem; margin:.9rem 0; }
.irow { display:flex; gap:1rem; align-items:center; justify-content:space-between; flex-wrap:wrap; }
.iname { font:600 1.1rem var(--mono); } .isum { color:var(--fg-muted); margin:.2rem 0 .4rem; max-width:80ch; } .irings .pill { margin-right:.25rem; }
.btn.big { padding:.75rem 1.5rem; font-size:15px; }
details.idetails, details.more, details.hint { margin:.7rem 0 0; }
details > summary { cursor:pointer; color:var(--accent); font-size:13.5px; }
details.idetails > summary:focus-visible, details.more > summary:focus-visible, details.hint > summary:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.ibody { margin-top:.8rem; }
/* Fleet */
.jump { position:sticky; top:var(--tabs-h); z-index:9; display:flex; gap:.3rem; overflow-x:auto; background:var(--bg); border-bottom:1px solid var(--border); padding:.4rem 0; margin:0 0 1rem; scrollbar-width:thin; }
.jump a { padding:.3rem .8rem; border-radius:999px; font-size:13px; color:var(--fg-muted); white-space:nowrap; border:1px solid transparent; }
.jump a:hover { text-decoration:none; color:var(--fg); background:var(--surface); }
.jump a.on { background:var(--accent-dim); color:var(--accent); border-color:var(--accent); }
.fsec { margin:0 0 2.2rem; scroll-margin-top:7rem; }
.fsec > h2 { margin:.2rem 0 .1rem; font-size:1.25rem; }
details.coll > summary { list-style:none; display:flex; align-items:baseline; gap:.8rem; cursor:pointer; }
details.coll > summary::-webkit-details-marker { display:none; }
details.coll > summary h2 { display:inline; margin:0; font-size:1.25rem; color:var(--fg); }
details.coll > summary::before { content:"+"; color:var(--accent); font-weight:700; width:1rem; }
details.coll[open] > summary::before { content:"-"; }
.problems .p { display:grid; grid-template-columns:auto 1fr auto; gap:.2rem .8rem; align-items:baseline; padding:.6rem .8rem; border:1px solid var(--border); border-left-width:4px; border-radius:6px; margin:.4rem 0; background:var(--surface); }
.problems .p.critical, .problems .p.high { border-left-color:var(--red); } .problems .p.medium { border-left-color:var(--yellow); } .problems .p.low { border-left-color:var(--fg-dim); }
.problems .p .t { font-weight:600; } .problems .p .d { grid-column:2 / 4; color:var(--fg-muted); font-size:13px; }
.rowbtn { background:none; border:0; color:var(--accent); font:600 13.5px var(--mono); cursor:pointer; padding:0; text-align:left; }
.rowbtn::before { content:"+ "; } .rowbtn[aria-expanded="true"]::before { content:"- "; }
tr.detail > td { background:var(--surface2); padding:1rem 1.2rem; }
.acts { display:flex; gap:.5rem; flex-wrap:wrap; margin:.8rem 0 0; }
h3.grp { margin:1.2rem 0 .3rem; font-size:1rem; color:var(--fg-muted); }
.docs-layout { grid-template-columns:minmax(210px,270px) minmax(0,1fr); gap:clamp(1rem,2vw,2.5rem); }
.docs-nav { top:6.5rem; }
.md pre, .md table { max-width:100%; }
.grid { grid-template-columns:repeat(auto-fill,minmax(min(100%,340px),1fr)); }
.split > #drawer { margin:1rem 0 0; }
@media (min-width:1100px) {
  .split:has(> #drawer:not([hidden])) { display:grid; grid-template-columns:minmax(0,1fr) minmax(22rem,30rem); gap:1rem; align-items:start; }
  .split > #drawer { position:sticky; top:7rem; max-height:calc(100vh - 8rem); overflow:auto; margin:0; }
}
@media (min-width:1800px) { .tiles { grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); } }
#view :not(pre) > code { overflow-wrap:anywhere; }
#view .md p, #view .md li, #view .md blockquote, #view .panel > p, #view .panel li, #view .summary, #view .isum, #view .lead { overflow-wrap:break-word; }
@media (max-width:760px) { .docs-layout { grid-template-columns:minmax(0,1fr); } .docs-nav { position:static; max-height:none; } .tbl-wrap, .md table { max-width:100%; overflow-x:auto; display:block; }
  .tabs a { padding:.65rem .9rem; font-size:14px; } .problems .p { grid-template-columns:1fr; } .problems .p .d { grid-column:1; } .irow { align-items:flex-start; } }
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
{banner}
<main id="view" role="tabpanel" tabindex="-1"><noscript><p>AutoStation is a static single-page app and needs JavaScript. The registry and installers do not: <a href="registry/manifest.yaml">registry/manifest.yaml</a>.</p></noscript><p>Loading&hellip;</p></main>
<footer class="foot">{esc(cfg.title)} &middot; built {esc(cfg.built_at)} {('&middot; commit ' + esc(cfg.commit)) if cfg.commit else ''} &middot;
<a href="{esc(cfg.repo_url)}">{esc(cfg.repo)}</a> &middot; <a href="status.json">status.json</a> &middot; static site: no server, no tracking.</footer>
</div><script src="assets/app.js?v={bid}"></script><script src="assets/jobs.js?v={bid}"></script><script src="assets/views.js?v={bid}"></script></body></html>
"""


def page_404(cfg: SiteConfig) -> str:
    home = cfg.pages_url
    body = ('<h1>Page not found</h1><p>That page does not exist (or has not been built yet). Go to the '
            f'<a href="{esc(home)}">AutoStation home</a> - Install, Fleet and Docs are the three places to go from there.</p>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">{sc.csp_meta(sc.theme.THEME_HEAD_JS)}'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Not found - AutoStation</title>'
            f'<script>{sc.theme.THEME_HEAD_JS}</script><link rel="stylesheet" href="{esc(home)}assets/site.css"></head>'
            f'<body><div class="page"><main>{body}</main></div></body></html>\n')


# ------------------------------------------------------------------------------------------------------------------ assemble
def assemble(out: Path, cfg: SiteConfig, manifest: dict | None = None, docs_info: dict | None = None) -> dict:
    """Compose the four views (home, install, fleet, docs) + the lazy Runs view from the staged pages, replace every old page with a redirect stub."""
    out = Path(out)
    manifest = manifest or {}
    staged: dict[str, str] = {}
    hooks: list[tuple[str, str]] = []
    stubs: list[tuple[str, str, str]] = []
    unknown: list[str] = []
    script_keys = {"jobs": "lazy:runs", "portal-admin": "fleet"}          # whose page scripts the app still needs, and when they run
    for f in sorted(out.rglob("*.html")):
        rel = f.relative_to(out).as_posix()
        if rel in KEEP_PAGES or rel.startswith("registry/") or "/evidence/" in rel:
            continue
        r = route_of(rel)
        text = f.read_text(encoding="utf-8")
        if not r:
            unknown.append(rel)
            continue
        key = page_key(rel)
        ex = extract(text)
        if key.startswith("portal-"):                                   # the portal's own status strip (fixed to its old top bar) has no place in the app
            ex["html"] = _PORTAL_STRIP.sub("", ex["html"], count=1)
        html_text = rewrite_links(ex["html"], rel, r)
        if ex["css"]:
            html_text = f"<style>{scope_css(ex['css'])}</style><div class=\"dash\">{html_text}</div>"
        staged[key] = html_text
        if key in script_keys:
            hooks.extend((script_keys[key], code) for code in ex["scripts"])
        elif key == "docs" or key.startswith("docs:"):
            hooks.extend((f"docs/{r[1]}" if r[1] else "docs/", code) for code in ex["scripts"])
        stubs.append((rel, r[0], r[1]))

    views: dict[str, dict[str, str]] = {t: {} for t in TAB_NAMES}
    views["home"][""] = site_simple.home_view(cfg, manifest)
    views["install"][""] = site_simple.install_view(cfg, manifest, {k.split(":", 1)[1]: v for k, v in staged.items() if k.startswith("install:")})
    views["fleet"][""] = site_simple.fleet_view(cfg, manifest, staged)
    for k, v in staged.items():
        if k == "docs":
            views["docs"][""] = v
        elif k.startswith("docs:"):
            views["docs"][k.split(":", 1)[1]] = v
    runs_body, runs_hint = site_simple.strip_chrome(staged.get("jobs", ""))
    runs_view = (site_simple._hint(runs_hint) + runs_body) if runs_body else (
        '<p class="sub">Nothing to show yet: runs appear after an installed automation reports. Install one from the Install tab and let it run once.</p>')

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
    write(out, "views/runs.json", json.dumps({"": runs_view}, separators=(",", ":"), ensure_ascii=True))

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

    merge_search(out, manifest, docs_info or {})
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
            u = f"/fleet/a/{name}"
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
