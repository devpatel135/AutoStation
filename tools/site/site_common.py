"""Shared pieces of the static site: config, escaping, the page layout and the shared CSS/JS assets.

Note: this folder is deliberately NOT a package called ``site`` (that would shadow Python's stdlib ``site`` module);
``tools/build_site.py`` puts it on ``sys.path`` and imports the ``site_*`` modules by name.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "components" / "wrapper-core"))
sys.path.insert(0, str(ROOT / "components" / "automation-registry"))
sys.path.insert(0, str(ROOT / "components" / "automation-logging"))

from registry.portal import theme  # noqa: E402  (shared visual language with the portal)
from registry.portal.common import installer_rel  # noqa: E402

DEFAULT_REPO = "devpatel135/AutoStation"
DEFAULT_INSTALLER_TEMPLATE = "https://github.com/{repo}/releases/download/installers-latest/{name}-setup.exe"


@dataclass
class SiteConfig:
    repo: str = DEFAULT_REPO
    default_branch: str = "main"
    installer_url_template: str = DEFAULT_INSTALLER_TEMPLATE
    demo: bool = False
    title: str = "AUTOSTATION"
    built_at: str = ""
    commit: str = ""
    notes: list = field(default_factory=list)   # warnings collected while building (shown in status.json)

    @property
    def owner(self) -> str:
        return self.repo.split("/")[0]

    @property
    def name(self) -> str:
        return self.repo.split("/")[1]

    @property
    def pages_url(self) -> str:
        return f"https://{self.owner}.github.io/{self.name}/"

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.repo}"

    @property
    def registry_url(self) -> str:
        return self.pages_url + "registry/"

    def installer_url(self, automation: str, version: str = "") -> str:
        return installer_rel({"installer_url_template": self.installer_url_template, "github_repo": self.repo}, automation, version)

    def blob_url(self, path: str) -> str:
        return f"{self.repo_url}/blob/{self.default_branch}/{path.lstrip('/')}"

    def issue_url(self, template: str, **fields) -> str:
        from urllib.parse import quote
        q = "&".join(f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in fields.items())
        return f"{self.repo_url}/issues/new?template={quote(template, safe='.-_')}" + (f"&{q}" if q else "")


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", re.sub(r"<[^>]+>", "", str(text)).lower()).strip("-")
    return s or "section"


def git_commit() -> str:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"][:12]
    try:
        r = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def now_iso() -> str:
    return os.environ.get("SITE_NOW") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


NAV = [("index.html", "Home", "home"), ("install/index.html", "Install", "install"), ("docs/index.html", "Docs", "docs"),
       ("portal/index.html", "Catalog", "portal"), ("dashboard/index.html", "Dashboard", "dashboard"),
       ("program/index.html", "Program", "program"), ("migration/index.html", "UiPath migration", "migration"),
       ("portal/admin.html", "Admin", "admin")]

EXTRA_CSS = r"""
.hero { padding:1.4rem 0 .6rem; } .hero h1 { font-size:2rem; margin:.3rem 0 .6rem; } .hero p { font-size:15px; color:var(--fg-muted); max-width:62ch; }
.roles { display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:.9rem; margin:1.2rem 0; }
.role { display:flex; flex-direction:column; gap:.4rem; text-decoration:none; color:var(--fg); }
.role:hover { border-color:var(--accent); text-decoration:none; }
.role h3 { margin:0; font-size:1rem; } .role p { margin:0; font-size:12.5px; color:var(--fg-muted); } .role .go { margin-top:auto; font-family:var(--mono); font-size:11px; color:var(--accent); }
.demo-banner { border:1px dashed var(--yellow); background:var(--yellow-bg); color:var(--fg); border-radius:6px; padding:.5rem .9rem; margin-bottom:1rem; font-size:12.5px; }
.crumbs { font-size:12px; color:var(--fg-dim); margin-bottom:.8rem; } .crumbs a { color:var(--fg-muted); }
.docs-layout { display:grid; grid-template-columns:230px 1fr; gap:1.5rem; align-items:start; }
.docs-nav { position:sticky; top:1rem; font-size:13px; max-height:85vh; overflow:auto; }
.docs-nav h4 { font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:.08em; color:var(--fg-dim); margin:.9rem 0 .3rem; }
.docs-nav a { display:block; padding:.18rem .5rem; border-left:2px solid transparent; color:var(--fg-muted); }
.docs-nav a.on { color:var(--accent); border-color:var(--accent); } .docs-nav a:hover { color:var(--fg); text-decoration:none; }
.search-results a { display:block; padding:.4rem .6rem; border:1px solid var(--border); border-radius:6px; margin-top:.35rem; background:var(--surface); color:var(--fg); }
.search-results small { color:var(--fg-dim); display:block; }
.md pre { margin:.4rem 0 1rem; } .md table { font-size:12.5px; } .md h2 { padding-top:.4rem; }
.cmd { position:relative; } .cmd button { position:absolute; top:.4rem; right:.4rem; font-size:11px; padding:.15rem .5rem; }
.steps { counter-reset:s; list-style:none; margin:0 0 1rem; padding:0; } .steps li { counter-increment:s; display:flex; gap:.7rem; margin:.5rem 0; }
.steps li::before { content:counter(s); flex:0 0 1.6rem; height:1.6rem; border-radius:50%; background:var(--accent); color:#fff; font:700 11px var(--mono); display:flex; align-items:center; justify-content:center; margin-top:.1rem; }
.two { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:1rem; }
.warnbox { border:1px solid var(--yellow); background:var(--yellow-bg); border-radius:6px; padding:.6rem .9rem; margin:.8rem 0; font-size:13px; }
.sha { font-family:var(--mono); font-size:11.5px; word-break:break-all; color:var(--fg-muted); }
@media (max-width:760px) { .docs-layout { grid-template-columns:1fr; } .docs-nav { position:static; max-height:none; } .hero h1 { font-size:1.5rem; } }
"""

SITE_JS = r"""
(function () {
  var b = document.getElementById('theme-toggle');
  if (b) { b.addEventListener('click', function () {
    var cur = document.documentElement.getAttribute('data-theme');
    if (!cur) cur = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    var next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('autostation-theme', next); } catch (e) {}
  }); }
  Array.prototype.forEach.call(document.querySelectorAll('[data-copy]'), function (btn) {
    btn.addEventListener('click', function () {
      var el = document.getElementById(btn.getAttribute('data-copy')); if (!el) return;
      var text = el.textContent;
      var done = function () { var o = btn.textContent; btn.textContent = 'Copied'; setTimeout(function () { btn.textContent = o; }, 1200); };
      try { navigator.clipboard.writeText(text).then(done, function () {}); } catch (e) {}
    });
  });
  var sel = document.getElementById('ring-select');
  if (sel) {
    var apply = function () {
      Array.prototype.forEach.call(document.querySelectorAll('[data-ring-text]'), function (el) {
        el.textContent = el.getAttribute('data-ring-text').split('{ring}').join(sel.value);
      });
      Array.prototype.forEach.call(document.querySelectorAll('[data-ring-ver]'), function (el) {
        var v = el.getAttribute('data-ring-ver-' + sel.value); el.textContent = v || 'not released to this ring';
      });
    };
    sel.addEventListener('change', apply); apply();
  }
})();
"""


def write(out: Path, rel: str, text: str) -> None:
    p = Path(out) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_assets(out: Path) -> None:
    css = theme.CSS + EXTRA_CSS
    write(out, "assets/site.css", css)
    write(out, "assets/site.js", SITE_JS)
    write(out, ".nojekyll", "")


def layout(title: str, body: str, *, cfg: SiteConfig, depth: int = 0, active: str = "", description: str = "", extra_head: str = "",
          extra_js: str = "") -> str:
    base = "../" * depth
    links = "".join(f'<a href="{base}{href}"{" class=active aria-current=page" if key == active else ""}>{esc(label)}</a>' for href, label, key in NAV)
    banner = ""
    if cfg.demo and active in ("dashboard", "program", "home"):
        banner = '<div class="demo-banner"><strong>DEMO DATA.</strong> Dashboards on this site are generated from synthetic fleet history until live analytics is enabled (see docs/PIPELINES.md).</div>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} - {esc(cfg.title)}</title>
{f'<meta name="description" content="{esc(description)}">' if description else ''}
<script>{theme.THEME_HEAD_JS}</script>
<link rel="stylesheet" href="{base}assets/site.css">{extra_head}
</head><body><div class="page">
<a class="sr" href="#main">Skip to content</a>
<header class="top">
  <div class="brand">{theme.LOGO_SVG}<div><div class="eyebrow">{esc(cfg.title)} &middot; UiPath replacement platform</div><div style="font-weight:700">{esc(title)}</div></div></div>
  <nav class="main" aria-label="Main">{links}<button id="theme-toggle" type="button" title="Toggle light/dark theme" aria-label="Toggle theme">Theme</button></nav>
</header>
<main id="main">{banner}{body}</main>
<footer class="foot">{esc(cfg.title)} &middot; built {esc(cfg.built_at)} {('&middot; commit ' + esc(cfg.commit)) if cfg.commit else ''} &middot;
<a href="{esc(cfg.repo_url)}">{esc(cfg.repo)}</a> &middot; static site: no server, no tracking.</footer>
</div><script src="{base}assets/site.js"></script>{extra_js}</body></html>
"""


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
