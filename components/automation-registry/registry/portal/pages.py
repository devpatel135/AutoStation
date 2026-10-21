"""Catalog (index), automation detail, compatibility and governance pages."""
from __future__ import annotations

from wrapper_core import manifest as wcm
from wrapper_core import versions

from .common import (classification_pill, describe_schedule, describe_slot, describe_windows, esc, health_pill, installer_rel,
                     layout, rel_href, pill, rings_of, safe_url, status_pill)
from . import markdown

INDEX_JS = r"""
(function () {
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card[data-name]'));
  var q = document.getElementById('q'), st = document.getElementById('status'), ringSel = document.getElementById('ring');
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip[data-tag]'));
  var activeTags = {};
  function apply() {
    var text = (q.value || '').toLowerCase(), status = st.value, n = 0;
    cards.forEach(function (c) {
      var ok = (!text || c.getAttribute('data-search').indexOf(text) >= 0) && (!status || c.getAttribute('data-status') === status);
      var tags = (c.getAttribute('data-tags') || '').split(' ');
      Object.keys(activeTags).forEach(function (t) { if (tags.indexOf(t) < 0) ok = false; });
      c.hidden = !ok; if (ok) n++;
    });
    document.getElementById('count').textContent = n + ' of ' + cards.length + ' automations';
  }
  function setRing() {
    var r = ringSel.value;
    cards.forEach(function (c) {
      var a = c.querySelector('[data-role=download]'), v = c.getAttribute('data-v-' + r), href = c.getAttribute('data-href-' + r);
      var sha = c.querySelector('[data-role=sha]'), s = c.getAttribute('data-sha-' + r);
      if (v) { a.textContent = 'Download installer v' + v + ' (' + r + ')'; a.setAttribute('href', href); a.classList.remove('disabled'); sha.textContent = /^0+$/.test(s || '') ? 'package sha256 PLACEHOLDER - not a real release' : 'package sha256 ' + (s || '').slice(0, 16) + '...'; }
      else { a.textContent = 'Not released to ' + r + ' yet'; a.removeAttribute('href'); a.classList.add('disabled'); sha.textContent = ''; }
    });
  }
  chips.forEach(function (ch) { ch.addEventListener('click', function () {
    var t = ch.getAttribute('data-tag');
    if (activeTags[t]) { delete activeTags[t]; ch.classList.remove('on'); } else { activeTags[t] = 1; ch.classList.add('on'); }
    apply(); }); });
  q.addEventListener('input', apply); st.addEventListener('change', apply); ringSel.addEventListener('change', setRing);
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
    attrs = [f'data-name="{esc(name)}"', f'data-search="{esc(search)}"', f'data-status="{esc(entry.get("status", "active"))}"',
             f'data-tags="{esc(" ".join(tags))}"']
    for r in order:
        v = rings.get(r)
        sha = ((versions_.get(str(v)) or {}).get("sha256") if v else "") or ""
        attrs += [f'data-v-{r}="{esc(v or "")}"', f'data-href-{r}="{esc(installer_rel(config, name, v) if v else "")}"', f'data-sha-{r}="{esc(sha)}"']
    dr = _default_ring(entry, order)
    dv = rings.get(dr) if dr else None
    dsha = ((versions_.get(str(dv)) or {}).get("sha256") if dv else "") or ""
    if dv:
        href = installer_rel(config, name, dv)
        dl = (f'<a class="btn primary" data-role="download" href="{esc(href)}" download>Download installer v{esc(dv)} ({esc(dr)})</a>'
              f'<span class="mono" data-role="sha">'
              + ("package sha256 PLACEHOLDER - not a real release" if set(dsha) == {"0"} else f"package sha256 {esc(dsha[:16])}...")
              + '</span>')
    else:
        dl = '<a class="btn disabled" data-role="download">Not released yet</a><span class="mono" data-role="sha"></span>'
    ring_pills = "".join(pill(f"{r} {rings.get(r) or '-'}", "info" if rings.get(r) else "") for r in order)
    reason = (f'<div class="banner warn" style="margin:0;padding:.4rem .7rem;font-size:12px"><strong>Paused:</strong> '
              f'{esc(entry.get("paused_reason") or "no reason recorded")}</div>') if entry.get("status") == "paused" else ""
    last = (health or {}).get("last_run") or {}
    htxt = ""
    if health:
        htxt = f'<dt>Last run</dt><dd>{esc(last.get("status", "-"))} {esc(last.get("started_at", ""))}</dd>' if last else ""
        open_sigs = health.get("signals") or []
        if open_sigs:
            htxt += f'<dt>Signals</dt><dd>{esc(", ".join(sorted({str(s.get("type")) for s in open_sigs})))}</dd>'
    paused_cls = " paused" if entry.get("status") == "paused" else ""
    return f"""<article class="card{paused_cls}" {' '.join(attrs)}>
  <div class="row"><a class="name" href="automation/{esc(name)}.html">{esc(name)}</a><span>{status_pill(entry)} {health_pill(health)}</span></div>
  <p class="summary">{esc(summary)}</p>{reason}
  <dl class="kv">
    <dt>Owner</dt><dd>{esc(owner)}{(' / ' + esc(entry.get('owner_team'))) if entry.get('owner_team') else ''}</dd>
    <dt>Systems</dt><dd>{esc(', '.join(map(str, systems)) or 'none')}</dd>
    <dt>Data</dt><dd>{classification_pill(entry.get('data_classification'))}</dd>
    <dt>Schedule</dt><dd>{esc(describe_schedule(entry))}</dd>{htxt}
  </dl>
  <div class="rings">{ring_pills}</div>
  <div class="dl">{dl}</div>
</article>"""


def build_index(m: dict, config: dict, health: dict[str, dict], manifest_text: str) -> str:
    order = rings_of(m)
    autos = m.get("automations") or {}
    active = sum(1 for e in autos.values() if (e or {}).get("status") != "paused")
    fleet = m.get("fleet") or {}
    banner = (f'<div class="banner bad"><strong>Fleet paused.</strong> Every automation halts at its next run. '
              f'Reason: {esc(fleet.get("paused_reason"))}</div>') if fleet.get("paused") else ""
    tags = sorted({str(t) for e in autos.values() for t in ((e or {}).get("catalog") or {}).get("tags") or []})
    chips = "".join(f'<span class="chip" role="button" tabindex="0" data-tag="{esc(t)}">{esc(t)}</span>' for t in tags)
    ring_opts = "".join(f'<option value="{esc(r)}"{" selected" if r == "broad" else ""}>{esc(r)}</option>' for r in order)
    cards = "".join(_card(n, e or {}, order, config, health.get(n)) for n, e in autos.items()) or '<p class="empty">No automations registered yet.</p>'
    body = f"""{banner}
<div class="tiles">
  <div class="tile"><div class="n">{len(autos)}</div><div class="l">Automations</div></div>
  <div class="tile"><div class="n" style="color:var(--green)">{active}</div><div class="l">Active</div></div>
  <div class="tile"><div class="n" style="color:var(--yellow)">{len(autos) - active}</div><div class="l">Paused</div></div>
</div>
<h2>Automation catalog</h2>
<p class="summary">Download the installer for the ring you belong to (pilot users: pilot). Installers need no administrator rights.
Verify the package hash shown on the card against the signed installer's details.</p>
<div class="controls">
  <label class="sr" for="q">Search</label><input id="q" type="search" placeholder="Search name, owner, system, tag...">
  <label class="sr" for="status">Status</label><select id="status"><option value="">All statuses</option><option value="active">Active</option><option value="paused">Paused</option></select>
  <label for="ring" class="mono">ring</label><select id="ring">{ring_opts}</select>
  <span id="count" class="mono"></span>
</div>
<div class="chips" aria-label="Tag filter">{chips}</div>
<div class="grid">{cards}</div>"""
    return layout("Automation catalog", body, config=config, active="catalog", manifest_text=manifest_text,
                  extra_js=f"<script>{INDEX_JS}</script>")


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
                 pending_triggers: list[dict], manifest_text: str) -> str:
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
    return layout(name, body, config=config, active="catalog", depth=1, manifest_text=manifest_text)


def build_compat(m: dict, config: dict, manifest_text: str) -> str:
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
    return layout("Compatibility", body, config=config, active="compat", manifest_text=manifest_text)


def build_governance(md_text: str | None, config: dict, manifest_text: str) -> str:
    if md_text:
        body = f'<div class="md">{markdown.render(md_text)}</div>'
    else:
        body = ('<div class="banner warn"><strong>No governance document is published yet.</strong> Add <code>docs/GOVERNANCE.md</code> '
                "to the repository (who can create automations, who approves registry entries, required security review before pilot, "
                "data-classification rules) and it appears here on the next Pages build.</div>")
    return layout("Governance", body, config=config, active="gov", manifest_text=manifest_text)
