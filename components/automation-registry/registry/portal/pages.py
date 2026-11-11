"""Catalog (index), automation detail, compatibility and governance pages."""
from __future__ import annotations

from wrapper_core import manifest as wcm
from wrapper_core import versions


def _fmt_conf(v) -> str:
    try:
        return str(round(float(v), 2))
    except (TypeError, ValueError):
        return str(v) if v is not None else "0"

from .common import (classification_pill, describe_schedule, describe_slot, describe_windows, esc, health_pill, installer_rel,
                     layout, rel_href, pill, rings_of, safe_url, status_pill)
from . import markdown

INDEX_JS = r"""
(function () {
  var cards = Array.prototype.slice.call(document.querySelectorAll('.acard[data-name]'));
  var q = document.getElementById('q');
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip[data-tag]'));
  var activeTags = {};
  var activeFilter = 'all';
  function apply() {
    var text = (q.value || '').toLowerCase(), n = 0;
    cards.forEach(function (c) {
      var status = c.getAttribute('data-status') || '';
      var rings = (c.getAttribute('data-rings') || '').split(' ');
      var tags = (c.getAttribute('data-tags') || '').split(' ');
      var ok = (!text || c.getAttribute('data-search').indexOf(text) >= 0);
      if (activeFilter === 'active') ok = ok && status === 'active';
      else if (activeFilter === 'paused') ok = ok && status === 'paused';
      else if (activeFilter === 'broad') ok = ok && rings.indexOf('broad') >= 0;
      else if (activeFilter === 'ai') ok = ok && tags.indexOf('ai') >= 0;
      Object.keys(activeTags).forEach(function (t) { if (tags.indexOf(t) < 0) ok = false; });
      c.hidden = !ok; if (ok) n++;
    });
    document.getElementById('count').textContent = n + ' of ' + cards.length + ' automations';
  }
  function setRing(r) {
    cards.forEach(function (c) {
      var a = c.querySelector('[data-role=download]'), v = c.getAttribute('data-v-' + r), href = c.getAttribute('data-href-' + r);
      var sha = c.querySelector('[data-role=sha]'), s = c.getAttribute('data-sha-' + r);
      if (v) { a.textContent = '↓ Install v' + v + ' (' + r + ')'; a.setAttribute('href', href); a.classList.remove('disabled'); sha.textContent = /^0+$/.test(s || '') ? 'package sha256 PLACEHOLDER - not a real release' : 'package sha256 ' + (s || '').slice(0, 16) + '…'; }
      else { a.textContent = 'Not released to ' + r + ' yet'; a.removeAttribute('href'); a.classList.add('disabled'); sha.textContent = ''; }
    });
  }
  document.querySelectorAll('.fpill[data-filter]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.fpill[data-filter]').forEach(function (b) { b.classList.remove('on'); });
      btn.classList.add('on');
      activeFilter = btn.getAttribute('data-filter');
      apply();
    });
  });
  document.querySelectorAll('.ring-sel[data-role=ring-sel-global]').forEach(function (sel) {
    sel.addEventListener('change', function () { setRing(sel.value); });
  });
  chips.forEach(function (ch) { ch.addEventListener('click', function () {
    var t = ch.getAttribute('data-tag');
    if (activeTags[t]) { delete activeTags[t]; ch.classList.remove('on'); } else { activeTags[t] = 1; ch.classList.add('on'); }
    apply(); }); });
  q.addEventListener('input', apply);
  apply();
})();
"""


def _default_ring(entry: dict, order: list[str]) -> str | None:
    for r in reversed(order):
        if (entry.get("rings") or {}).get(r):
            return r
    return None


def _card(name: str, entry: dict, order: list[str], config: dict, health: dict | None) -> str:
    cat = entry.get("catalog") or {}
    rings = entry.get("rings") or {}
    versions_ = entry.get("versions") or {}
    tags = [str(t) for t in cat.get("tags") or []]
    summary = cat.get("summary") or entry.get("description") or ""
    systems = cat.get("systems") or entry.get("systems") or []
    owner = entry.get("owner")
    search = " ".join(str(x) for x in [name, summary, entry.get("description"), owner, entry.get("owner_team"),
                                       " ".join(map(str, systems)), " ".join(tags)]).lower()
    released_rings = [r for r in order if (entry.get("rings") or {}).get(r)]
    ai_tag = "ai" if any(t.lower() in ("ai", "llm", "pattern1", "pattern2", "pattern3", "pattern4") for t in tags) else ""
    search_tags = tags + ([ai_tag] if ai_tag else [])
    attrs = [f'data-name="{esc(name)}"', f'data-search="{esc(search)}"', f'data-status="{esc(entry.get("status", "active"))}"',
             f'data-tags="{esc(" ".join(search_tags))}"', f'data-rings="{esc(" ".join(released_rings))}"']
    for r in order:
        v = rings.get(r)
        sha = ((versions_.get(str(v)) or {}).get("sha256") if v else "") or ""
        attrs += [f'data-v-{r}="{esc(v or "")}"', f'data-href-{r}="{esc(installer_rel(config, name, v) if v else "")}"', f'data-sha-{r}="{esc(sha)}"']
    dr = _default_ring(entry, order)
    dv = rings.get(dr) if dr else None
    dsha = ((versions_.get(str(dv)) or {}).get("sha256") if dv else "") or ""
    sha_display = ("PLACEHOLDER - not a real release" if (dsha and set(dsha) == {"0"}) else (esc(dsha[:16]) + "…" if dsha else "n/a"))
    if dv:
        href = installer_rel(config, name, dv)
        dl_btn = f'<a class="btn primary" data-role="download" href="{esc(href)}" download>↓ Install v{esc(dv)} ({esc(dr)})</a>'
    else:
        dl_btn = '<a class="btn disabled" data-role="download">Not released yet</a>'
    # ring progress pips
    pip_html = "".join(f'<div class="rpip {esc(r) if rings.get(r) else ""}" title="{esc(r)}: {esc(rings.get(r) or "not released")}"></div>' for r in order)
    rl_html = "".join(f'<div class="rl">{esc(r[0])}</div>' for r in order)
    ring_prog = f'<div class="ring-prog"><div class="ring-pips">{pip_html}</div><div class="ring-labels">{rl_html}</div></div>'
    # health dot
    h_status = (health or {}).get("status", "")
    dot_cls = "ok" if h_status == "healthy" else ("warn" if h_status == "degraded" else ("crit" if h_status == "failing" else "mute"))
    open_sigs = (health or {}).get("signals") or []
    sig_tag = (f' <span class="tag warn" style="font-size:9px">{esc(", ".join(sorted({str(s.get("type")) for s in open_sigs[:2]})))}</span>') if open_sigs else ""
    # paused banner
    paused_cls = " paused" if entry.get("status") == "paused" else ""
    reason_html = ""
    if entry.get("status") == "paused":
        reason_html = f'<div class="banner warn" style="margin:0 14px 6px;padding:.35rem .7rem;font-size:11px"><strong>Paused:</strong> {esc(entry.get("paused_reason") or "no reason recorded")}</div>'
    _status_cls = "ok" if entry.get("status") != "paused" else "mute"
    tag_html = "".join(f'<span class="tag {_status_cls}">{esc(entry.get("status", "active"))}</span> ') + "".join(f'<span class="tag sys">{esc(t)}</span> ' for t in tags)
    status_txt = "active" if entry.get("status") != "paused" else "paused"
    status_tag = f'<span class="tag {"mute" if status_txt == "paused" else "ok"}">{esc(status_txt)}</span>'
    tags_html = " ".join(f'<span class="tag sys">{esc(t)}</span>' for t in tags)
    # ring selector for install
    ring_opts = "".join(f'<option value="{esc(r)}"{" selected" if r == dr else ""}>{esc(r)}{(" v" + esc(rings.get(r))) if rings.get(r) else ""}</option>' for r in order)
    ring_sel = (f'<select class="mono" data-role="ring-sel" style="font-size:10px;padding:2px 5px;border:1px solid var(--bd);background:none;color:var(--fm);border-radius:3px">{ring_opts}</select>' if order else "")
    return f"""<article class="acard{paused_cls}" {' '.join(attrs)}>
  <div class="acard-top">
    <div class="hdot {dot_cls}"></div>
    <div class="acard-title">
      <div class="acard-name">{esc(name)}</div>
      <div class="acard-tags">{status_tag}{(" " + tags_html) if tags_html else ""}{sig_tag}</div>
    </div>
    {ring_prog}
  </div>
  {reason_html}<div class="acard-div"></div>
  <div class="acard-meta">
    <div><div class="ml">Schedule</div><div class="mv">{esc(describe_schedule(entry))}</div></div>
    <div><div class="ml">Owner</div><div class="mv">{esc(owner or "-")}</div></div>
    <div><div class="ml">Systems</div><div class="mv">{esc(", ".join(map(str, systems)) or "none")}</div></div>
    <div><div class="ml">Data</div><div class="mv">{classification_pill(entry.get("data_classification"))}</div></div>
  </div>
  <div class="acard-sha"><span>sha256</span><span class="sha-val" data-role="sha">{sha_display}</span>{ring_sel}</div>
  <div class="acard-foot">
    {dl_btn}
    <a class="btn" href="automation/{esc(name)}.html">Details →</a>
    <a class="btn" href="admin.html#{esc(name)}">Admin →</a>
  </div>
</article>"""


def build_index(m: dict, config: dict, health: dict[str, dict], manifest_text: str,
               status_data: dict | None = None) -> str:
    order = rings_of(m)
    autos = m.get("automations") or {}
    active = sum(1 for e in autos.values() if (e or {}).get("status") != "paused")
    fleet = m.get("fleet") or {}
    banner = (f'<div class="banner bad"><strong>Fleet paused.</strong> Every automation halts at its next run. '
              f'Reason: {esc(fleet.get("paused_reason"))}</div>') if fleet.get("paused") else ""
    tags = sorted({str(t) for e in autos.values() for t in ((e or {}).get("catalog") or {}).get("tags") or []})
    chips = "".join(f'<span class="chip" role="button" tabindex="0" data-tag="{esc(t)}">{esc(t)}</span>' for t in tags)
    cards = "".join(_card(n, e or {}, order, config, health.get(n)) for n, e in autos.items()) or '<p class="empty">No automations registered yet.</p>'
    has_ai = any(any(t.lower() in ("ai", "llm", "pattern1", "pattern2", "pattern3", "pattern4")
                     for t in ((e or {}).get("catalog") or {}).get("tags") or []) for e in autos.values())
    ai_pill = f'<button class="fpill" data-filter="ai">AI-augmented</button>' if has_ai else ""
    broad_pill = f'<button class="fpill" data-filter="broad">Broad ring</button>' if "broad" in order else ""
    ring_opts = "".join(f'<option value="{esc(r)}">{esc(r)}</option>' for r in order)
    ring_sel_global = (f'<select id="ring" class="ring-sel mono" data-role="ring-sel-global" style="font-size:10px;padding:2px 5px;border:1px solid var(--bd);background:none;color:var(--fm);border-radius:3px">'
                       f'{ring_opts}</select>' if order else "")
    body = f"""{banner}
<div class="view-head">
  <div><div class="view-title">Catalog</div>
  <div class="view-sub">Download installers · verify package hashes · no administrator rights required</div></div>
</div>
<div class="controls">
  <div class="search-wrap">
    <svg class="search-ic" width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="6.5" cy="6.5" r="4.5" stroke="currentColor" stroke-width="1.5"/><line x1="10" y1="10" x2="14" y2="14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
    <label class="sr" for="q">Search</label>
    <input id="q" type="search" placeholder="Search name, owner, system, tag…">
  </div>
  {ring_sel_global}
  <span id="count" class="mono"></span>
</div>
<div class="filter-pills">
  <button class="fpill on" data-filter="all">All</button>
  <button class="fpill" data-filter="active">Active</button>
  <button class="fpill" data-filter="paused">Paused</button>
  {broad_pill}{ai_pill}
</div>
<div class="chips" aria-label="Tag filter">{chips}</div>
<div class="card-grid">{cards}</div>"""
    return layout("Catalog", body, config=config, active="catalog", manifest_text=manifest_text,
                  extra_js=f"<script>{INDEX_JS}</script>", status_data=status_data)


def _versions_table(name: str, entry: dict, order: list[str]) -> str:
    rings = entry.get("rings") or {}
    rows = []
    for v, rec in sorted((entry.get("versions") or {}).items(), key=lambda kv: versions.parse(kv[0]), reverse=True):
        in_rings = [r for r in order if rings.get(r) == v]
        req = (rec or {}).get("requires") or {}
        rows.append(f"<tr><td><code>{esc(v)}</code></td><td>{''.join(pill(r, 'info') for r in in_rings) or pill('not deployed')}</td>"
                    f"<td class=mono>{esc(req.get('wrapper_core', '-'))}<br>{esc(req.get('execution_framework', '-'))}</td>"
                    f"<td class=mono>{esc((rec or {}).get('sha256', ''))}</td><td class=mono>{esc((rec or {}).get('url', ''))}</td></tr>")
    return ('<div class="tbl-wrap"><table><tr><th>Version</th><th>Rings</th><th>Requires (wrapper-core / framework)</th><th>sha256</th><th>Package</th></tr>'
            + "".join(rows) + "</table></div>") if rows else '<p class="empty">No versions registered.</p>'


def _kv_table(d: dict) -> str:
    if not d:
        return '<p class="empty">Defaults.</p>'
    return "<table>" + "".join(f"<tr><td><code>{esc(k)}</code></td><td>{esc(v)}</td></tr>" for k, v in d.items()) + "</table>"


def build_detail(name: str, entry: dict, m: dict, config: dict, health: dict | None, history: list[dict],
                 pending_triggers: list[dict], manifest_text: str,
                 status_data: dict | None = None) -> str:
    order = rings_of(m)
    cat = entry.get("catalog") or {}
    sch = entry.get("schedule") or {}
    tz = sch.get("timezone")
    slot_rows = "".join(
        f"<tr><td><code>{esc(s.get('id'))}</code></td><td>{esc(s.get('type'))}</td><td>{esc(describe_slot(s, tz))}</td>"
        f"<td class=mono>{esc(', '.join(f'{k}={v}' for k, v in (s.get('params') or {}).items()) or '-')}</td></tr>" for s in sch.get("slots") or [])
    exp = sch.get("expected_duration_ms") or {}
    links = []
    for ln in cat.get("links") or []:
        title, url = (ln, ln) if isinstance(ln, str) else (ln.get("title", ln.get("url")), ln.get("url"))
        ok = safe_url(url)
        links.append(f'<li><a href="{esc(ok)}" rel="noopener noreferrer">{esc(title)}</a></li>' if ok else f"<li>{esc(title)} ({esc(url)})</li>")
    trig_rules = "".join(
        f"<tr><td><code>{esc(t.get('on_automation'))}</code></td><td>{esc(t.get('on_status', 'any'))}</td><td>{esc(t.get('slot', '-'))}</td>"
        f"<td class=mono>{esc(', '.join(f'{k}={v}' for k, v in (t.get('params') or {}).items()) or '-')}</td></tr>" for t in entry.get("triggers") or [])
    pend = "".join(f"<tr><td class=mono>{esc(t.get('id'))}</td><td>{esc(t.get('slot'))}</td><td class=mono>{esc(t.get('params'))}</td>"
                   f"<td>{esc(t.get('created_at'))}</td><td>{esc(t.get('source'))}</td></tr>" for t in pending_triggers)
    hist = "".join(f"<tr><td>{esc(h['date'])}</td><td><code>{esc(h['action'])}</code></td><td>{esc(h['detail'])}</td></tr>" for h in reversed(history))
    paused = (f'<div class="banner warn"><strong>Paused:</strong> {esc(entry.get("paused_reason") or "no reason recorded")}. '
              "Machines halt this automation at their next run.</div>") if entry.get("status") == "paused" else ""
    hl = ""
    if health:
        last = health.get("last_run") or {}
        hl = ("<h2>Health</h2><p>" + health_pill(health) + "</p>"
              + (f'<p class="summary">Last run: {esc(last.get("status"))} at {esc(last.get("started_at"))}. {esc(last.get("synopsis", ""))}</p>' if last else "")
              + ("<table><tr><th>Signal</th><th>Severity</th><th>Detail</th></tr>" + "".join(
                  f"<tr><td>{esc(s.get('type'))}</td><td>{esc(s.get('severity'))}</td><td>{esc(s.get('detail'))}</td></tr>"
                  for s in health.get("signals") or []) + "</table>" if health.get("signals") else ""))
    dr = _default_ring(entry, order)
    dv = (entry.get("rings") or {}).get(dr) if dr else None
    dl = (f'<a class="btn primary" href="{esc(rel_href(config, installer_rel(config, name, dv), "../"))}" download>Download installer v{esc(dv)} ({esc(dr)})</a> '
          f'<a class="btn" href="{esc(rel_href(config, installer_rel(config, name, dv), "../"))}.sha256">sha256 file</a>') if dv else ""
    body = f"""<p><a href="../index.html">&larr; Catalog</a></p>
<div class="row" style="display:flex;gap:.8rem;align-items:center;flex-wrap:wrap"><h1 style="font-family:var(--mono)">{esc(name)}</h1>{status_pill(entry)} {health_pill(health)}</div>
<p class="summary" style="margin-top:.4rem">{esc(cat.get('summary') or entry.get('description'))}</p>
{paused}
<div class="actions" style="margin:.8rem 0">{dl} <a class="btn" href="../admin.html#{esc(name)}">Operator actions</a></div>
<h2>Overview</h2>
<dl class="kv">
<dt>Owner</dt><dd>{esc(entry.get('owner'))}</dd><dt>Team</dt><dd>{esc(entry.get('owner_team') or cat.get('owner_team') or '-')}</dd>
<dt>Process owner</dt><dd>{esc(cat.get('process_owner') or '-')}</dd>
<dt>Systems</dt><dd>{esc(', '.join(map(str, cat.get('systems') or entry.get('systems') or [])) or 'none')}</dd>
<dt>Data</dt><dd>{classification_pill(entry.get('data_classification'))} <span class="mono">LLM approved up to: {esc(entry.get('llm_allowed_classification') or 'n/a')}</span></dd>
<dt>Effort saved</dt><dd>{esc(cat.get('minutes_per_item', '-'))} min per item</dd>
<dt>Notify on complete</dt><dd>{esc('yes' if entry.get('notify_on_complete') else 'no')}</dd>
<dt>Tags</dt><dd>{''.join(pill(t) for t in cat.get('tags') or []) or '-'}</dd></dl>
<h3>Description</h3><p>{esc(entry.get('description'))}</p>
{('<h3>Links</h3><ul>' + ''.join(links) + '</ul>') if links else ''}
<h2>Versions by ring</h2>
<div class="rings" style="margin-bottom:.8rem">{''.join(pill(f"{r}: {(entry.get('rings') or {}).get(r) or 'not released'}", 'info') for r in order)}</div>
{_versions_table(name, entry, order)}
<h2>Schedule</h2>
<div class="tbl-wrap"><table><tr><th>Slot</th><th>Type</th><th>When</th><th>Params</th></tr>{slot_rows or '<tr><td colspan=4 class=empty>No slots</td></tr>'}</table></div>
<dl class="kv"><dt>Expected duration</dt><dd>{esc(exp.get('baseline', '-'))} ms (flag SLOW above x{esc(exp.get('warn_threshold', '-'))})</dd>
<dt>No runs during</dt><dd>{esc(describe_windows(entry) or 'no maintenance windows')}</dd></dl>
<h2>Error handling</h2>{_kv_table(entry.get('error_handling') or {})}
<h2>Triggers</h2>
{('<h3>Event rules (start this automation when...)</h3><table><tr><th>Automation</th><th>Status</th><th>Slot</th><th>Params</th></tr>' + trig_rules + '</table>') if trig_rules else '<p class="empty">No event rules.</p>'}
{('<h3>Pending on-demand triggers</h3><table><tr><th>Id</th><th>Slot</th><th>Params</th><th>Created</th><th>Source</th></tr>' + pend + '</table>') if pend else ''}
{hl}
<h2>Changelog</h2>
{('<table><tr><th>Date</th><th>Action</th><th>Detail</th></tr>' + hist + '</table>') if hist else '<p class="empty">No recorded operator actions.</p>'}"""
    return layout(name, body, config=config, active="catalog", depth=1, manifest_text=manifest_text,
                  status_data=status_data)


def build_compat(m: dict, config: dict, manifest_text: str, status_data: dict | None = None) -> str:
    order = rings_of(m)
    comps = m.get("components") or {}
    matrix = m.get("compatibility") or []
    mrows = "".join(f"<tr><td class=mono>{esc(r.get('wrapper_core'))}</td><td class=mono>{esc(r.get('execution_framework'))}</td></tr>" for r in matrix)
    ring_rows = []
    for r in order:
        w = wcm.target_version(comps.get("wrapper-core", {}), r)
        f = wcm.target_version(comps.get("execution-framework", {}), r)
        ok = bool(w and f and wcm.compatible(m, str(w), str(f)))
        ring_rows.append(f"<tr><td><strong>{esc(r)}</strong></td><td><code>{esc(w or '-')}</code></td><td><code>{esc(f or '-')}</code></td>"
                         f"<td>{pill('supported', 'ok') if ok else pill('NOT SUPPORTED', 'bad')}</td></tr>")
    auto_head = "".join(f"<th>{esc(r)}</th>" for r in order)
    auto_rows = []
    for name, e in (m.get("automations") or {}).items():
        cells = []
        for r in order:
            v = ((e or {}).get("rings") or {}).get(r)
            rec = (((e or {}).get("versions") or {}).get(str(v)) if v else None) or {}
            req = rec.get("requires") or {}
            w = wcm.target_version(comps.get("wrapper-core", {}), r)
            f = wcm.target_version(comps.get("execution-framework", {}), r)
            good = bool(v) and versions.satisfies(str(w), req.get("wrapper_core")) and versions.satisfies(str(f), req.get("execution_framework"))
            cells.append(f"<td>{(pill(v, 'ok') if good else pill(v, 'bad')) if v else '-'}</td>")
        auto_rows.append(f"<tr><td><a href=\"automation/{esc(name)}.html\">{esc(name)}</a></td>{''.join(cells)}</tr>")
    body = f"""<h2>Components per ring</h2>
<div class="tbl-wrap"><table><tr><th>Ring</th><th>wrapper-core</th><th>execution-framework</th><th>Matrix check</th></tr>{''.join(ring_rows)}</table></div>
<h2>Supported pairs</h2>
<div class="tbl-wrap"><table><tr><th>wrapper-core</th><th>execution-framework</th></tr>{mrows or '<tr><td colspan=2 class=empty>No matrix defined (anything goes).</td></tr>'}</table></div>
<h2>Automation versions per ring</h2>
<p class="summary">Red = the automation's <code>requires</code> are not satisfied by that ring's component versions (CI blocks such a manifest).</p>
<div class="tbl-wrap"><table><tr><th>Automation</th>{auto_head}</tr>{''.join(auto_rows)}</table></div>"""
    return layout("Compatibility", body, config=config, active="fleet", manifest_text=manifest_text,
                  status_data=status_data)


def build_governance(md_text: str | None, config: dict, manifest_text: str,
                     status_data: dict | None = None) -> str:
    if md_text:
        body = f'<div class="md">{markdown.render(md_text)}</div>'
    else:
        body = ('<div class="banner warn"><strong>No governance document is published yet.</strong> Add <code>docs/GOVERNANCE.md</code> '
                "to the repository (who can create automations, who approves registry entries, required security review before pilot, "
                "data-classification rules) and it appears here on the next Pages build.</div>")
    return layout("Governance", body, config=config, active="gov", manifest_text=manifest_text,
                  status_data=status_data)


# ── helpers for new pages ──

def _spark_svg(values: list, w: int = 80, h: int = 22, bad: bool = False) -> str:
    vals = [float(v or 0) for v in values]
    cls = "spark-svg bad" if bad else "spark-svg"
    if not vals or max(vals) == 0:
        return f'<svg class="{esc(cls)}" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true"><line x1="0" y1="{h-2}" x2="{w}" y2="{h-2}"/></svg>'
    mx = max(vals)
    step = w / max(1, len(vals) - 1)
    pts = " ".join(f"{i * step:.1f},{h - 2 - (v / mx) * (h - 4):.1f}" for i, v in enumerate(vals))
    return f'<svg class="{esc(cls)}" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true"><polyline points="{pts}"/></svg>'


def _daily_buckets(name: str, run_list: list[dict], days: int = 10) -> tuple[list[int], list[int]]:
    """Return (runs_per_day, failures_per_day) for the last ``days`` days, oldest first."""
    from collections import defaultdict
    runs_d: dict = defaultdict(int)
    fail_d: dict = defaultdict(int)
    for r in run_list:
        if r.get("automation") != name:
            continue
        ts = str(r.get("started_at") or "")[:10]
        if ts:
            runs_d[ts] += 1
            if r.get("status") not in ("success", "partial", "skipped_maintenance", "halted_paused"):
                fail_d[ts] += 1
    all_days = sorted(set(list(runs_d) + list(fail_d)))[-days:]
    return [runs_d.get(d, 0) for d in all_days], [fail_d.get(d, 0) for d in all_days]


def _run_strip(name: str, run_list: list[dict], n: int = 10) -> str:
    mine = sorted((r for r in run_list if r.get("automation") == name),
                  key=lambda r: str(r.get("started_at") or ""))
    recent = mine[-n:]
    pips = []
    for r in recent:
        s = str(r.get("status") or "")
        cls = "success" if s == "success" else ("partial" if s == "partial" else ("hung" if s == "hung" else "failed" if s in ("failed", "self_heal_failed") else ""))
        pips.append(f'<div class="rs-pip {esc(cls)}" title="{esc(s)} {esc(str(r.get("started_at", ""))[:10])}"></div>')
    return f'<div class="run-strip" title="Last {len(pips)} runs (newest right)">' + "".join(pips) + "</div>"


def _hosts_from_data(run_list: list[dict], signals: list[dict]) -> dict[str, dict]:
    """Derive a {host: {ring, automations, last_seen, signals}} map from run + signal data."""
    hosts: dict[str, dict] = {}
    for r in run_list:
        h = r.get("host")
        if not h:
            continue
        rec = hosts.setdefault(h, {"ring": r.get("ring", ""), "automations": set(), "last_seen": "", "signals": []})
        if r.get("automation"):
            rec["automations"].add(str(r["automation"]))
        ts = str(r.get("started_at") or "")
        if ts > rec["last_seen"]:
            rec["last_seen"] = ts
            if r.get("ring"):
                rec["ring"] = r["ring"]
    for s in signals:
        h = s.get("host")
        if h and h in hosts:
            hosts[h]["signals"].append(s)
    return hosts


def build_overview(m: dict, config: dict, health: dict[str, dict], signals: list[dict],
                   run_list: list[dict], manifest_text: str,
                   status_data: dict | None = None) -> str:
    order = rings_of(m)
    autos = m.get("automations") or {}
    fleet = m.get("fleet") or {}
    total_runs = len(run_list)
    success_runs = sum(1 for r in run_list if r.get("status") == "success")
    rate = f"{100 * success_runs / total_runs:.1f}%" if total_runs else "n/a"
    total_items = sum((r.get("items") or {}).get("processed", 0) if isinstance(r.get("items"), dict) else 0 for r in run_list)
    active_autos = sum(1 for e in autos.values() if (e or {}).get("status") != "paused")
    pending_promo = []
    for name, entry in autos.items():
        e = entry or {}
        rings = e.get("rings") or {}
        vs = [rings.get(r) for r in order if rings.get(r)]
        if len(set(vs)) > 1 or (vs and len(vs) < len(order)):
            prev = order[[i for i, r in enumerate(order) if rings.get(r)][-1] - 1] if len(order) > 1 else None
            target = next((r for r in reversed(order) if not rings.get(r)), None)
            if target:
                pending_promo.append((name, f"{prev or 'dev'} → {target}"))
    fleet_banner = (f'<div class="banner bad"><strong>Fleet paused:</strong> {esc(fleet.get("paused_reason"))}. '
                    'Every automation halts at its next run.</div>') if fleet.get("paused") else ""
    # signals table
    sig_rows = "".join(
        f'<tr><td class="mono" style="font-size:11px">{esc(s.get("automation", "—"))}</td>'
        f'<td><span class="tag {("crit" if s.get("severity") in ("critical","high") else "warn")}">{esc(s.get("type"))}</span></td>'
        f'<td><span class="tag {("crit" if s.get("severity") == "critical" else "warn")}">{esc(s.get("severity"))}</span></td>'
        f'<td class="mono" style="font-size:11px">{esc(s.get("host", "—"))}</td>'
        f'<td style="font-size:11px">{esc(s.get("detail", ""))}</td>'
        f'<td style="font-size:11px;color:var(--fd)">{esc(str(s.get("first_seen", s.get("window", "")))[:10])}</td></tr>'
        for s in signals[:20]) or f'<tr><td colspan="6" class="empty">No open signals.</td></tr>'
    # promote-ready table
    promo_rows = "".join(
        f'<tr><td class="mono" style="font-size:11px">{esc(n)}</td><td style="font-size:11px">{esc(t)}</td>'
        f'<td><a class="btn primary" href="admin.html#{esc(n)}">Prepare →</a></td></tr>'
        for n, t in pending_promo) or f'<tr><td colspan="3" class="empty">No automations mid-rollout.</td></tr>'
    # recent runs table (last 25)
    recent = sorted(run_list, key=lambda r: str(r.get("started_at") or ""), reverse=True)[:25]
    run_rows = "".join(
        f'<tr><td style="font-size:11px;color:var(--fd)">{esc(str(r.get("started_at", ""))[:16])}</td>'
        f'<td class="mono" style="font-size:11px">{esc(r.get("automation", "—"))}</td>'
        f'<td class="mono" style="font-size:11px">{esc(r.get("host", "—"))}</td>'
        f'<td><span class="tag {("ok" if r.get("status") == "success" else "warn" if r.get("status") == "partial" else "crit")}">{esc(r.get("status", "—"))}</span></td>'
        f'<td style="font-size:11px">{esc(str(round((r.get("duration_ms") or 0) / 1000, 1)) + "s") if r.get("duration_ms") else "—"}</td>'
        f'<td style="font-size:11px;color:var(--fm)">{esc((r.get("synopsis") or "")[:80])}</td></tr>'
        for r in recent) or f'<tr><td colspan="6" class="empty">No runs recorded.</td></tr>'
    hours_saved = (status_data or {}).get("hours_saved")
    hours_tile = (f'<div class="tile"><div class="l">Est. hours saved</div><div class="n ok">{int(hours_saved)}</div><div class="s">catalog estimate</div></div>'
                  if hours_saved is not None else "")
    body = f"""{fleet_banner}
<div class="view-head"><div>
  <div class="view-title">Overview</div>
  <div class="view-sub">Fleet pulse · advisory data · manifest digest in footer</div>
</div></div>
<div class="tiles">
  <div class="tile"><div class="l">Runs</div><div class="n">{total_runs}</div><div class="s">{esc(rate)} success</div></div>
  <div class="tile"><div class="l">Items processed</div><div class="n ac">{total_items if total_items else "—"}</div></div>
  {hours_tile}<div class="tile"><div class="l">Active automations</div><div class="n ok">{active_autos}</div></div>
  <div class="tile"><div class="l">Open signals</div><div class="n {"warn" if signals else "ok"}">{len(signals)}</div></div>
</div>
<h2>Open compliance signals</h2>
<div class="tbl-wrap"><table>
  <thead><tr><th>Automation</th><th>Signal</th><th>Severity</th><th>Host</th><th>Detail</th><th>First seen</th></tr></thead>
  <tbody>{sig_rows}</tbody>
</table></div>
<h2>Promote-ready</h2>
<div class="tbl-wrap"><table>
  <thead><tr><th>Automation</th><th>Transition</th><th>Action</th></tr></thead>
  <tbody>{promo_rows}</tbody>
</table></div>
<h2>Recent runs</h2>
<div class="tbl-wrap"><table>
  <thead><tr><th>Started</th><th>Automation</th><th>Host</th><th>Status</th><th>Duration</th><th>Synopsis</th></tr></thead>
  <tbody>{run_rows}</tbody>
</table></div>"""
    return layout("Overview", body, config=config, active="overview", manifest_text=manifest_text,
                  status_data=status_data)


def build_health(m: dict, config: dict, health: dict[str, dict], signals: list[dict],
                 run_list: list[dict], manifest_text: str,
                 status_data: dict | None = None) -> str:
    autos = m.get("automations") or {}
    # per-automation health cards
    cards = []
    for name in autos:
        h = health.get(name)
        if not h:
            continue
        status = h.get("status", "unknown")
        last = h.get("last_run") or {}
        synopsis = esc((last.get("synopsis") or "")[:140])
        open_sigs = h.get("signals") or []
        sig_html = "".join(
            f'<span class="tag {("crit" if s.get("severity") in ("critical","high") else "warn")}" style="font-size:9px">'
            f'{esc(s.get("type"))}</span> ' for s in open_sigs[:3])
        runs_d, fail_d = _daily_buckets(name, run_list)
        spark_html = (f'<div class="spark-row">'
                      f'<div class="spark-item"><div class="spark-label">Runs/day</div>{_spark_svg(runs_d)}</div>'
                      f'<div class="spark-item"><div class="spark-label">Failures/day</div>{_spark_svg(fail_d, bad=True)}</div>'
                      f'</div>') if runs_d else ""
        cards.append(
            f'<div class="hcard {esc(status)}">'
            f'<div class="hcard-head"><div><div class="hcard-name">{esc(name)}</div>'
            f'<div style="font-size:11px;color:var(--fd);margin-top:2px">{esc((autos.get(name) or {}).get("owner", ""))}</div></div>'
            f'<span class="tag {"ok" if status == "healthy" else "warn" if status == "degraded" else "crit"}">{esc(status)}</span></div>'
            + _run_strip(name, run_list)
            + (f'<div style="font-size:12px;color:var(--fm);margin:5px 0;line-height:1.5">{synopsis}</div>' if synopsis else "")
            + spark_html
            + (f'<div style="margin-top:5px">{sig_html}</div>' if sig_html else "")
            + '</div>')
    cards_html = "\n".join(cards) or '<p class="empty">No health data available.</p>'
    # signals table with diagnosis
    sig_rows = "".join(
        f'<tr><td><span class="tag {("crit" if s.get("severity") == "critical" else "warn" if s.get("severity") == "high" else "mute")}">{esc(s.get("severity"))}</span></td>'
        f'<td class="mono" style="font-size:11px">{esc(s.get("type"))}</td>'
        f'<td class="mono" style="font-size:11px">{esc(s.get("automation", "—"))}</td>'
        f'<td class="mono" style="font-size:11px">{esc(s.get("host", "—"))}</td>'
        f'<td style="font-size:11px">{esc(s.get("detail", ""))}</td>'
        f'<td style="font-size:11px;color:var(--fm)">{esc(((s.get("diagnosis") or {}).get("root_cause") or "")[:80])}'
        f'{(" <em style=font-size:10px>(" + esc(_fmt_conf((s.get("diagnosis") or {}).get("confidence"))) + " conf · advisory)</em>") if (s.get("diagnosis") or {}).get("confidence") else ""}</td></tr>'
        for s in signals[:30]) or f'<tr><td colspan="6" class="empty">No open signals.</td></tr>'
    # item telemetry
    telem_rows = []
    for name in autos:
        mine = [r for r in run_list if r.get("automation") == name]
        if not mine:
            continue
        total = len(mine)
        items_all = [(r.get("items") or {}) for r in mine if isinstance(r.get("items"), dict)]
        proc = sum(i.get("processed", 0) for i in items_all)
        fail = sum(i.get("failed", 0) for i in items_all)
        durations = sorted(r["duration_ms"] for r in mine if r.get("duration_ms"))
        p50 = f"{round(durations[len(durations)//2]/1000,1)}s" if durations else "—"
        p95 = f"{round(durations[int(len(durations)*.95)]/1000,1)}s" if len(durations) > 1 else "—"
        telem_rows.append(
            f'<tr><td class="mono" style="font-size:11px">{esc(name)}</td>'
            f'<td>{total}</td><td>{proc if proc else "—"}</td><td>{fail if fail else "—"}</td>'
            f'<td>{p50}</td><td>{p95}</td></tr>')
    telem_html = "".join(telem_rows) or f'<tr><td colspan="6" class="empty">No run data.</td></tr>'
    body = f"""<div class="view-head"><div>
  <div class="view-title">Health</div>
  <div class="view-sub">Run history · compliance signals · LLM diagnosis · item telemetry · advisory data</div>
</div></div>
<h2>Automation health</h2>
{cards_html}
<h2>Active signals</h2>
<div class="tbl-wrap"><table>
  <thead><tr><th>Severity</th><th>Signal</th><th>Automation</th><th>Host</th><th>Detail</th><th>LLM diagnosis (advisory)</th></tr></thead>
  <tbody>{sig_rows}</tbody>
</table></div>
<h2>Item telemetry</h2>
<div class="tbl-wrap"><table>
  <thead><tr><th>Automation</th><th>Runs</th><th>Items processed</th><th>Items failed</th><th>Duration p50</th><th>Duration p95</th></tr></thead>
  <tbody>{telem_html}</tbody>
</table></div>"""
    return layout("Health", body, config=config, active="health", manifest_text=manifest_text,
                  status_data=status_data)


def build_fleet(m: dict, config: dict, health: dict[str, dict], signals: list[dict],
                run_list: list[dict], manifest_text: str,
                status_data: dict | None = None) -> str:
    from wrapper_core import manifest as wcm
    from wrapper_core import versions
    order = rings_of(m)
    comps = m.get("components") or {}
    hosts = _hosts_from_data(run_list, signals)
    # machine cards
    machine_cards = []
    for host, info in sorted(hosts.items()):
        ring = info.get("ring", "")
        autos_list = sorted(info.get("automations", []))
        last = info.get("last_seen", "")[:16]
        sigs = info.get("signals", [])
        worst_sig = max((s for s in sigs), key=lambda s: {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(str(s.get("severity")), 0), default=None)
        sig_html = ""
        if worst_sig:
            sev = str(worst_sig.get("severity", ""))
            cls = "crit" if sev in ("critical", "high") else "warn"
            sig_html = (f'<div class="msig {cls}"><div class="sig-dot"></div>'
                        f'{esc(worst_sig.get("type", ""))} · {esc(worst_sig.get("detail", "")[:60])}</div>')
        else:
            sig_html = '<div class="msig ok"><div class="sig-dot"></div>Nominal</div>'
        autos_html = "".join(f'<div class="achip">{esc(a)}</div>' for a in autos_list[:8])
        machine_cards.append(
            f'<div class="mcard">'
            f'<div class="mcard-head">'
            f'<div class="mname">{esc(host)}</div>'
            f'{"<div class=mring " + esc(ring) + ">" + esc(ring) + "</div>" if ring else ""}'
            f'</div>'
            f'<div class="mhb">Last seen: <span>{esc(last)}</span></div>'
            f'<div class="mautos">{autos_html}</div>'
            f'{sig_html}</div>')
    machines_html = ("\n".join(machine_cards) if machine_cards else
                     '<p class="empty">No host data. Machines appear here once they send a run log.</p>')
    # compat matrix
    ring_rows = []
    for r in order:
        w = wcm.target_version(comps.get("wrapper-core", {}), r)
        f = wcm.target_version(comps.get("execution-framework", {}), r)
        ok = bool(w and f and wcm.compatible(m, str(w), str(f)))
        ring_rows.append(f'<tr><td><strong>{esc(r)}</strong></td><td><code>{esc(w or "-")}</code></td>'
                         f'<td><code>{esc(f or "-")}</code></td>'
                         f'<td>{pill("supported", "ok") if ok else pill("NOT SUPPORTED", "bad")}</td></tr>')
    auto_head = "".join(f"<th>{esc(r)}</th>" for r in order)
    auto_rows = []
    for name, e in (m.get("automations") or {}).items():
        cells = []
        for r in order:
            v = ((e or {}).get("rings") or {}).get(r)
            rec = (((e or {}).get("versions") or {}).get(str(v)) if v else None) or {}
            req = rec.get("requires") or {}
            w = wcm.target_version(comps.get("wrapper-core", {}), r)
            fw = wcm.target_version(comps.get("execution-framework", {}), r)
            good = bool(v) and versions.satisfies(str(w), req.get("wrapper_core")) and versions.satisfies(str(fw), req.get("execution_framework"))
            cells.append(f"<td>{(pill(v, 'ok') if good else pill(v, 'bad')) if v else '-'}</td>")
        auto_rows.append(f'<tr><td><a href="automation/{esc(name)}.html" class="mono" style="font-size:11px">{esc(name)}</a></td>{"".join(cells)}</tr>')
    body = f"""<div class="view-head"><div>
  <div class="view-title">Fleet</div>
  <div class="view-sub">{len(hosts)} enrolled machines · component versions · compatibility matrix</div>
</div></div>
<h2>Machines</h2>
<div class="machine-grid">{machines_html}</div>
<h2>Component versions per ring</h2>
<div class="tbl-wrap"><table>
  <thead><tr><th>Ring</th><th>wrapper-core</th><th>execution-framework</th><th>Matrix check</th></tr></thead>
  <tbody>{"".join(ring_rows) or "<tr><td colspan=4 class=empty>No components defined.</td></tr>"}</tbody>
</table></div>
<h2>Automation versions per ring</h2>
<p class="summary" style="margin-bottom:.6rem">Red = automation <code>requires</code> not satisfied by that ring's component versions.</p>
<div class="tbl-wrap"><table>
  <thead><tr><th>Automation</th>{auto_head}</tr></thead>
  <tbody>{"".join(auto_rows) or "<tr><td colspan=9 class=empty>None registered.</td></tr>"}</tbody>
</table></div>"""
    return layout("Fleet", body, config=config, active="fleet", manifest_text=manifest_text,
                  status_data=status_data)


def build_audit(gov_text: str | None, history: list[dict], config: dict, manifest_text: str,
                status_data: dict | None = None) -> str:
    hist_rows = "".join(
        f'<tr><td style="font-size:11px">{esc(h["date"])}</td>'
        f'<td><code>{esc(h["action"])}</code></td>'
        f'<td class="mono" style="font-size:11px">{esc(h.get("target", ""))}</td>'
        f'<td style="font-size:11px">{esc(h["detail"])}</td></tr>'
        for h in reversed(history)) or f'<tr><td colspan="4" class="empty">No recorded operator actions.</td></tr>'
    if gov_text:
        gov_html = f'<div class="md">{markdown.render(gov_text)}</div>'
    else:
        gov_html = ('<div class="banner warn"><strong>No governance document published yet.</strong> Add <code>docs/GOVERNANCE.md</code> '
                    'and it appears here on the next build. Document: who may create automations, who approves registry entries, '
                    'security review requirements, data-classification rules, and token rotation cadences.</div>')
    body = f"""<div class="view-head"><div>
  <div class="view-title">Audit</div>
  <div class="view-sub">Operator changelog · governance · LLM decision audit via <code>python -m automation_logging audit</code></div>
</div></div>
<h2>Operator changelog</h2>
<div class="tbl-wrap"><table>
  <thead><tr><th>Date</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead>
  <tbody>{hist_rows}</tbody>
</table></div>
<h2>LLM classification audit</h2>
<div class="banner" style="margin-bottom:.8rem">
  Pattern 1/2/3 LLM decision records live in the <strong>automation-logging archive</strong>, not here.
  Run <code>python -m automation_logging audit --archive ./archive --days 30 --format table</code> locally
  or see the fleet dashboard for the full decision trail with confidence scores and reasoning.
</div>
<h2>Governance</h2>
{gov_html}"""
    return layout("Audit", body, config=config, active="audit", manifest_text=manifest_text,
                  status_data=status_data)
