"""Docs site: renders ``docs/*.md`` (+ component READMEs) to ``docs/*.html`` with a sidebar and a client-side search index."""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from site_common import ROOT, SiteConfig, esc, harden_html, layout, slug, write

from registry.portal import markdown

# (file stem, group, one-line description) - order matters; unknown docs are appended under "More"
KNOWN = [
    ("GETTING_STARTED", "Start here", "From a process to a running pilot automation"),
    ("UIPATH_MIGRATION", "Start here", "Moving from UiPath: concept map, inventory, parallel run, cutover"),
    ("ACTIVITIES", "Start here", "The activity library: Excel, mail, files, queues, UI, HTTP, database"),
    ("WRAPPER_CORE_API", "Reference", "wrapper-core API: telemetry, retry, LLM seam, parameters"),
    ("AUTOMATION_STANDARD", "Reference", "The rules every automation is reviewed against"),
    ("SPEC", "Reference", "Contracts between components (manifest, logs, broker)"),
    ("ARCHITECTURE", "Reference", "Repo map and roadmap coverage"),
    ("RUNBOOKS", "Operate", "Kill-switch, rollback, triage, rotation, decommission"),
    ("GOVERNANCE", "Operate", "Roles, change control, data classification"),
    ("PIPELINES", "Operate", "GitHub workflows, secrets, promotion on GitHub"),
    ("SITE", "Operate", "How this site is built and published"),
    ("SECURITY", "Security", "Trust boundaries, controls, known gaps"),
    ("VERIFY_IN_YOUR_ENVIRONMENT", "Security", "What CI cannot verify for you"),
]
GROUP_ORDER = ["Start here", "Reference", "Operate", "Security", "Components", "More"]


@dataclass
class Doc:
    stem: str
    title: str
    group: str
    desc: str
    src: str        # repo-relative path
    md: str

    @property
    def page(self) -> str:
        return f"{self.stem}.html"


def _title(md: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", md, re.M)
    return re.sub(r"[`*_]", "", m.group(1)).strip() if m else fallback


def collect(repo: Path) -> list[Doc]:
    repo = Path(repo)
    docs: list[Doc] = []
    seen = set()
    for stem, group, desc in KNOWN:
        f = repo / "docs" / f"{stem}.md"
        if f.is_file():
            md = f.read_text(encoding="utf-8")
            docs.append(Doc(stem, _title(md, stem.replace("_", " ").title()), group, desc, f"docs/{stem}.md", md))
            seen.add(stem)
    for f in sorted((repo / "docs").glob("*.md")) if (repo / "docs").is_dir() else []:
        if f.stem not in seen:
            md = f.read_text(encoding="utf-8")
            docs.append(Doc(f.stem, _title(md, f.stem), "More", "", f"docs/{f.name}", md))
    comp = repo / "components"
    if comp.is_dir():
        for d in sorted(p for p in comp.iterdir() if p.is_dir()):
            r = d / "README.md"
            if r.is_file():
                md = r.read_text(encoding="utf-8")
                docs.append(Doc(f"components-{d.name}", _title(md, d.name), "Components", f"components/{d.name}/README.md", f"components/{d.name}/README.md", md))
    return docs


def rewrite_links(md: str, doc: Doc, by_src: dict[str, Doc], cfg: SiteConfig) -> str:
    """Turn relative markdown links into site pages (known docs) or GitHub blob links (everything else)."""
    base_dir = Path(doc.src).parent

    def fix(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        if re.match(r"^(https?://|mailto:|#)", url):
            return m.group(0)
        path, _, frag = url.partition("#")
        if not path:
            return m.group(0)
        norm = (base_dir / path).as_posix()
        parts: list[str] = []
        for seg in norm.split("/"):
            if seg == "..":
                if parts:
                    parts.pop()
            elif seg not in ("", "."):
                parts.append(seg)
        target = "/".join(parts)
        if target in by_src:
            return f"[{label}]({by_src[target].page}{('#' + frag) if frag else ''})"
        return f"[{label}]({cfg.blob_url(target)}{('#' + frag) if frag else ''})"

    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", fix, md)


def _add_heading_ids(html_text: str) -> tuple[str, list[tuple[str, str]]]:
    used: dict[str, int] = {}
    heads: list[tuple[str, str]] = []

    def sub(m: re.Match) -> str:
        level, inner = m.group(1), m.group(2)
        base = slug(inner)
        n = used.get(base, 0)
        used[base] = n + 1
        sid = base if n == 0 else f"{base}-{n}"
        heads.append((sid, re.sub(r"<[^>]+>", "", inner)))
        return f'<h{level} id="{sid}">{inner}</h{level}>'

    return re.sub(r"<h([1-4])>(.*?)</h\1>", sub, html_text), heads


def _plain(md_chunk: str) -> str:
    t = re.sub(r"```.*?```", " ", md_chunk, flags=re.S)
    t = re.sub(r"[`*_>#|]", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def search_entries(doc: Doc) -> list[dict]:
    out = []
    sections = re.split(r"(?m)^(#{1,4}\s+.*)$", doc.md)
    heading, buf = doc.title, sections[0]
    used: dict[str, int] = {}

    def flush(h: str, text: str):
        base = slug(h)
        n = used.get(base, 0)
        used[base] = n + 1
        out.append({"doc": doc.page, "title": doc.title, "heading": h, "anchor": base if n == 0 else f"{base}-{n}", "text": _plain(text)[:400]})

    for i in range(1, len(sections), 2):
        flush(heading, buf)
        heading, buf = re.sub(r"^#+\s+", "", sections[i]).strip(), sections[i + 1] if i + 1 < len(sections) else ""
    flush(heading, buf)
    return [e for e in out if e["text"] or e["heading"]]


SEARCH_JS = r"""
(function () {
  var box = document.getElementById('docsearch'), res = document.getElementById('docresults'); if (!box || !res) return;
  var index = null, loading = false;
  function load(cb) { if (index) return cb(); if (loading) return; loading = true;
    fetch('search-index.json').then(function (r) { return r.json(); }).then(function (j) { index = j; cb(); })
      .catch(function () { res.textContent = 'Search needs the site to be served over http(s).'; }); }
  function run() {
    var q = box.value.trim().toLowerCase(); res.textContent = ''; if (q.length < 2) return;
    var terms = q.split(/\s+/), hits = [];
    index.forEach(function (e) {
      var hay = (e.heading + ' ' + e.text + ' ' + e.title).toLowerCase(), score = 0;
      for (var i = 0; i < terms.length; i++) { if (hay.indexOf(terms[i]) < 0) return; score += (e.heading.toLowerCase().indexOf(terms[i]) >= 0 ? 3 : 1); }
      hits.push([score, e]); });
    hits.sort(function (a, b) { return b[0] - a[0]; });
    hits.slice(0, 12).forEach(function (h) { var e = h[1], a = document.createElement('a'), s = document.createElement('small');
      a.setAttribute('href', e.doc + '#' + e.anchor); a.textContent = e.title + ' - ' + e.heading; s.textContent = e.text.slice(0, 140); a.appendChild(s); res.appendChild(a); });
    if (!hits.length) res.textContent = 'No matches.';
  }
  box.addEventListener('input', function () { load(run); });
})();
"""


def _nav(docs: list[Doc], current: str | None, depth_prefix: str = "") -> str:
    by_group: dict[str, list[Doc]] = {}
    for d in docs:
        by_group.setdefault(d.group, []).append(d)
    parts = ['<nav class="docs-nav" aria-label="Documentation"><a href="index.html"' + (' class="on"' if current is None else "") + ">All docs</a>"]
    for g in GROUP_ORDER:
        if g in by_group:
            parts.append(f"<h4>{esc(g)}</h4>")
            for d in by_group[g]:
                parts.append(f'<a href="{esc(depth_prefix + d.page)}"' + (' class="on"' if d.stem == current else "") + f">{esc(d.title)}</a>")
    parts.append('<h4>Original</h4><a href="assessment.html">Architecture assessment</a></nav>')
    return "".join(parts)


def build_docs_site(out: Path, repo: Path, cfg: SiteConfig) -> dict:
    out, repo = Path(out), Path(repo)
    docs = collect(repo)
    by_src = {d.src: d for d in docs}
    index_entries: list[dict] = []
    pages: list[str] = []
    for d in docs:
        md = rewrite_links(d.md, d, by_src, cfg)
        rendered, heads = _add_heading_ids(markdown.render(md))
        index_entries += search_entries(d)
        edit = cfg.repo_url + f"/edit/{cfg.default_branch}/{d.src}"
        body = (f'<div class="crumbs"><a href="../index.html">Home</a> / <a href="index.html">Docs</a> / {esc(d.title)}</div>'
                f'<div class="docs-layout">{_nav(docs, d.stem)}<article class="md">{rendered}'
                f'<hr><p class="sub"><a href="{esc(edit)}" rel="noopener">Edit this page on GitHub</a> &middot; source <code>{esc(d.src)}</code></p></article></div>')
        write(out, f"docs/{d.page}", layout(d.title, body, cfg=cfg, depth=1, active="docs", description=d.desc))
        pages.append(f"docs/{d.page}")
    # index
    cards = []
    for g in GROUP_ORDER:
        mine = [d for d in docs if d.group == g]
        if not mine:
            continue
        cards.append(f'<h2>{esc(g)}</h2><div class="grid">' + "".join(
            f'<a class="card role" href="{esc(d.page)}"><h3>{esc(d.title)}</h3><p>{esc(d.desc)}</p></a>' for d in mine) + "</div>")
    body = (f'<div class="crumbs"><a href="../index.html">Home</a> / Docs</div><h1>Documentation</h1>'
            '<div class="docs-layout">' + _nav(docs, None) + '<div><div class="field"><label for="docsearch">Search the docs</label>'
            '<input id="docsearch" type="search" placeholder="e.g. kill-switch, retry, installer, queue" autocomplete="off"></div>'
            '<div id="docresults" class="search-results" aria-live="polite"></div>' + "".join(cards) + "</div></div>")
    write(out, "docs/index.html", layout("Documentation", body, cfg=cfg, depth=1, active="docs", extra_js=f"<script>{SEARCH_JS}</script>"))
    pages.append("docs/index.html")
    write(out, "docs/search-index.json", json.dumps(index_entries, separators=(",", ":")) + "\n")
    asm = repo / "docs" / "assessment.html"
    if asm.is_file():
        write(out, "docs/assessment.html", harden_html(asm.read_text(encoding="utf-8")))
        pages.append("docs/assessment.html")
    return {"pages": pages, "docs": [d.stem for d in docs], "search_entries": len(index_entries)}
