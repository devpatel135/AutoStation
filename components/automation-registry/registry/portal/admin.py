"""Fleet admin panel (``admin.html``).

There is no server. Operators pick an action; the page shows the *exact* manifest diff, the CLI command, the CI variable
set for the ``ops-action`` pipeline and ready-made GitLab links (run-pipeline, new-MR, edit-file). Nothing changes until
the pipeline runs and - for manifest changes - the merge request is approved.

All diffs/commands/variables are computed here, at build time, by applying the real ``registry.ops`` functions to a deep
copy of the manifest, so the browser never re-implements business rules. The only dynamic part is the operator's typed
reason, substituted for ``SENTINEL`` by the page script.
"""
from __future__ import annotations

import difflib
import re
import json
import urllib.parse

from wrapper_core import versions

from .. import ops
from ..ops import OpError
from .common import (esc, health_pill, json_for_script, layout, pill, rings_of, safe_url, status_pill)

SENTINEL = "__REASON__"
REASON_ARG = "{{REASON_ARG}}"


def _q(v: str) -> str:
    return urllib.parse.quote(str(v), safe="")


def build_links(config: dict, ci: dict, title: str, description: str, branch: str) -> dict:
    """Python reference implementation of the links the page script builds (kept in sync by tests)."""
    base = f"{config['gitlab_url'].rstrip('/')}/{config['project_path'].strip('/')}"
    params = dict(ci)
    params["BRANCH"] = branch
    pipeline = f"{base}/-/pipelines/new?ref={_q(config.get('ops_ref', 'main'))}" + "".join(
        f"&var[{k}]={_q(v)}" for k, v in params.items() if v != "")
    mr = (f"{base}/-/merge_requests/new?merge_request[source_branch]={_q(branch)}"
          f"&merge_request[target_branch]={_q(config.get('default_branch', 'main'))}"
          f"&merge_request[title]={_q(title)}&merge_request[description]={_q(description)}")
    edit = f"{base}/-/edit/{_q(config.get('default_branch', 'main'))}/manifest.yaml"
    return {"pipeline": pipeline, "merge_request": mr, "edit": edit}


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_WF_RE = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")


def github_config(cfg: dict) -> dict:
    """Validate the GitHub-mode settings (they end up in links); fall back to GitLab mode if anything looks off."""
    out = dict(cfg)
    if out.get("platform") != "github":
        out["platform"] = "gitlab"
        return out
    ok = (_REPO_RE.match(str(out.get("github_repo", ""))) and _WF_RE.match(str(out.get("ops_workflow", "")))
          and _PATH_RE.match(str(out.get("manifest_path", ""))) and ".." not in str(out.get("manifest_path", "")))
    if not ok:
        out["platform"] = "gitlab"
    return out


def build_github_links(config: dict, ci: dict, title: str, description: str, branch: str) -> dict:
    """Python reference implementation of the GitHub links the page script builds (kept in sync by tests)."""
    cfg = github_config(config)
    base = f"https://github.com/{cfg['github_repo']}"
    flags = " ".join(f"-f {k.lower()}={json.dumps(str(v))}" for k, v in {**ci, "BRANCH": branch}.items() if v != "")
    return {
        "workflow": f"{base}/actions/workflows/{_q(cfg['ops_workflow'])}",
        "gh_command": f"gh workflow run {cfg['ops_workflow']} --repo {cfg['github_repo']} --ref {cfg.get('ops_ref', 'main')} {flags}",
        "pull_request": (f"{base}/compare/{_q(cfg.get('default_branch', 'main'))}...{_q(branch)}?quick_pull=1"
                         f"&title={_q(title)}&body={_q(description)}"),
        "edit": f"{base}/edit/{_q(cfg.get('default_branch', 'main'))}/" + "/".join(_q(p) for p in cfg["manifest_path"].split("/")),
    }


def cli_command(action: str, target: str | None, flags: list[tuple[str, str]], with_reason: bool = True) -> str:
    parts = ["python -m registry", action] + ([target] if target else [])
    parts += [f"{k} {v}" for k, v in flags]
    return " ".join(parts) + (REASON_ARG if with_reason else "")


def _diff(before: str, after: str) -> list[str]:
    out = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=2))
    return [ln for ln in out if not ln.startswith(("---", "+++"))]


def _action(m: dict, action: str, target: str, label: str, *, needs_reason: bool, params: dict | None = None,
            flags: list[tuple[str, str]] | None = None) -> dict | None:
    try:
        res, before, after = ops.preview(m, ops.apply_action, action, target, SENTINEL, params or {})
    except OpError:
        return None
    if not res.manifest_changed:
        return None
    ci = {"ACTION": action, "TARGET": target if action not in ("fleet-pause", "fleet-resume") else "",
          "PARAMS": json.dumps(params or {}, sort_keys=True, separators=(",", ":"))}
    return {"id": f"{action}:{label}", "label": label, "kind": "manifest", "needs_reason": needs_reason,
            "cmd": cli_command(action, target if target and action not in ("fleet-pause", "fleet-resume") else None, flags or []),
            "ci": ci, "diff": _diff(before, after), "title": res.mr_title, "summary": res.summary}


def build_actions(m: dict) -> dict:
    order = rings_of(m)
    per: dict[str, list[dict]] = {}
    for name, entry in (m.get("automations") or {}).items():
        entry = entry or {}
        acts: list[dict | None] = []
        if entry.get("status") == "paused":
            acts.append(_action(m, "resume", name, "Resume", needs_reason=True))
            acts.append(_action(m, "decommission", name, "Decommission (remove from registry)", needs_reason=True))
        else:
            acts.append(_action(m, "pause", name, "Pause (kill-switch)", needs_reason=True))
        for to in order[1:]:
            v = (entry.get("rings") or {}).get(order[order.index(to) - 1])
            if v:
                acts.append(_action(m, "promote", name, f"Promote {v} to {to}", needs_reason=False, params={"to": to},
                                    flags=[("--to", to)]))
        for ring in order:
            for v in sorted((entry.get("versions") or {}), key=versions.parse, reverse=True):
                acts.append(_action(m, "rollback", name, f"Roll back {ring}+ to {v}", needs_reason=True,
                                    params={"ring": ring, "to": v}, flags=[("--ring", ring), ("--to", v)]))
        poll = [s["id"] for s in ((entry.get("schedule") or {}).get("slots") or []) if s.get("type") == "poll"]
        if poll and entry.get("status") != "paused":
            acts.append({"id": "trigger-add", "label": "Run now / retry (trigger)", "kind": "trigger", "needs_reason": False,
                         "slots": poll, "cmd": "", "ci": {"ACTION": "trigger-add", "TARGET": name, "PARAMS": "{}"},
                         "diff": [], "title": f"registry: trigger {name}", "summary": []})
        per[name] = [a for a in acts if a]
    fleet = (m.get("fleet") or {})
    fleet_acts = [a for a in (_action(m, "fleet-resume", "", "Resume the whole fleet", needs_reason=True) if fleet.get("paused")
                              else _action(m, "fleet-pause", "", "PAUSE THE WHOLE FLEET (kill-switch)", needs_reason=True),) if a]
    return {"automations": per, "fleet": fleet_acts}


ADMIN_JS = r"""
(function () {
  var data = JSON.parse(document.getElementById('admin-data').textContent);
  var SENT = data.sentinel, cfg = data.config, cur = null;
  function $(id) { return document.getElementById(id); }
  function shq(s, sh) { return sh === 'ps' ? "'" + s.replace(/'/g, "''") + "'" : '"' + s.replace(/(["\\$`])/g, '\\$1') + '"'; }
  function yq(s) { return /^[A-Za-z_][A-Za-z0-9_.\/@ -]*$/.test(s) && s === s.trim() && !/(: | #)/.test(s) && !/^(true|false|null|yes|no|on|off|~)$/i.test(s) ? s : JSON.stringify(s); }
  function reason() { return ($('reason').value || '').replace(/\s+/g, ' ').trim().slice(0, 300); }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
  function copyBtn(text, label) {
    var b = document.createElement('button'); b.type = 'button'; b.textContent = label || 'Copy';
    b.addEventListener('click', function () {
      var done = function () { b.textContent = 'Copied'; setTimeout(function () { b.textContent = label || 'Copy'; }, 1200); };
      try { navigator.clipboard.writeText(text).then(done, function () {}); } catch (e) {
        var t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select();
        try { document.execCommand('copy'); done(); } catch (e2) {} document.body.removeChild(t); }
    });
    return b;
  }
  function section(title) { var d = document.createElement('div'); var h = document.createElement('h3'); h.textContent = title; d.appendChild(h); return d; }
  function link(href, text, primary) { var a = document.createElement('a'); a.className = 'btn' + (primary ? ' primary' : ''); a.textContent = text; a.setAttribute('href', href); a.setAttribute('rel', 'noopener noreferrer'); a.setAttribute('target', '_blank'); return a; }
  function q(v) { return encodeURIComponent(v); }
  function render() {
    if (!cur) return;
    var d = cur.def, r = reason(), out = $('out'); clear(out);
    var needs = d.needs_reason && !r;
    var rShown = r || '<reason>';
    var params = d.ci.PARAMS, cmd = d.cmd, title = d.title, diff = d.diff, summary = d.summary;
    var sh = $('shell').value;
    if (d.kind === 'trigger') {
      var slot = $('slot').value, kv = {}, flags = '';
      ($('params').value || '').split(/\r?\n/).forEach(function (line) { var i = line.indexOf('='); if (i > 0) { kv[line.slice(0, i).trim()] = line.slice(i + 1).trim(); } });
      Object.keys(kv).forEach(function (k) { flags += ' --param ' + shq(k + '=' + kv[k], sh); });
      cmd = 'python -m registry trigger-add ' + cur.target + ' --slot ' + slot + flags;
      params = JSON.stringify({ slot: slot, params: kv });
      diff = ['+ (triggers branch) triggers.json: append', '+   automation: ' + cur.target, '+   slot: ' + slot, '+   params: ' + JSON.stringify(kv)];
      summary = ['No manifest change and no merge request: committed straight to the protected "triggers" branch, then Pages is republished.', 'The machine picks it up at its next poll run.'];
    } else {
      cmd = cmd.split('{{REASON_ARG}}').join(r ? ' --reason ' + shq(r, sh) : '');
    }
    title = title.split(SENT).join((r || '...').slice(0, 60));
    var s0 = section('What will happen'); var ul = document.createElement('ul'); ul.style.marginLeft = '1.2rem';
    summary.forEach(function (t) { var li = document.createElement('li'); li.textContent = t.split(SENT).join(rShown); ul.appendChild(li); });
    s0.appendChild(ul); out.appendChild(s0);
    if (needs) { var w = document.createElement('div'); w.className = 'banner warn'; w.textContent = 'A reason is required for this action. It is shown to users and kept in history/CHANGELOG.md.'; out.appendChild(w); }
    var s1 = section(d.kind === 'trigger' ? 'Trigger entry' : 'Manifest diff (exact)'); var pre = document.createElement('pre');
    diff.forEach(function (line) {
      var sp = document.createElement('span'); sp.className = line.charAt(0) === '+' ? 'add' : line.charAt(0) === '-' ? 'del' : line.charAt(0) === '@' ? 'hunk' : '';
      sp.textContent = line.split(SENT).join(yq(rShown)) + '\n'; pre.appendChild(sp); });
    s1.appendChild(pre); out.appendChild(s1);
    var s2 = section('Option A - CLI (in a clone of the registry repo, then open a merge request)');
    var p2 = document.createElement('pre'); p2.textContent = cmd; s2.appendChild(p2); s2.appendChild(copyBtn(cmd)); out.appendChild(s2);
    var ci = { ACTION: d.ci.ACTION, TARGET: d.ci.TARGET, REASON: r, PARAMS: params };
    var branch = 'ops/' + d.ci.ACTION + '-' + (d.ci.TARGET || 'fleet') + '-' + cur.id;
    var s3 = section(cfg.platform === 'github' ? 'Option B - ops-action workflow (GitHub Actions inputs)' : 'Option B - ops-action pipeline (CI variables)'); var tb = document.createElement('table');
    var vars = Object.keys(ci); if (d.kind !== 'trigger') { vars.push('BRANCH'); ci.BRANCH = branch; } var text = '';
    vars.forEach(function (k) { if (ci[k] === '') return; var tr = document.createElement('tr'), a = document.createElement('td'), b = document.createElement('td');
      a.textContent = k; b.textContent = ci[k]; b.className = 'mono'; tr.appendChild(a); tr.appendChild(b); tb.appendChild(tr); text += k + '=' + ci[k] + '\n'; });
    s3.appendChild(tb); s3.appendChild(copyBtn(text, 'Copy variables')); out.appendChild(s3);
    if (cfg.platform === 'github') {
      var gh = 'https://github.com/' + cfg.github_repo, ghdesc = 'ops-action ' + d.ci.ACTION + ' ' + (d.ci.TARGET || 'fleet') + '\nReason: ' + (r || '(none)');
      var ghc = 'gh workflow run ' + cfg.ops_workflow + ' --repo ' + cfg.github_repo + ' --ref ' + cfg.ops_ref;
      vars.forEach(function (k) { if (ci[k] !== '') ghc += ' -f ' + k.toLowerCase() + '=' + shq(ci[k], sh); });
      var s3g = section('GitHub CLI (same thing as the workflow form)'); var p3 = document.createElement('pre'); p3.textContent = ghc; s3g.appendChild(p3); s3g.appendChild(copyBtn(ghc)); out.appendChild(s3g);
      var s4g = section('GitHub links'); var rowg = document.createElement('div'); rowg.className = 'actions';
      if (!needs) {
        rowg.appendChild(link(gh + '/actions/workflows/' + q(cfg.ops_workflow), 'Open the ops-action workflow (Run workflow, paste the inputs above)', true));
        if (d.kind !== 'trigger') { rowg.appendChild(link(gh + '/compare/' + q(cfg.default_branch) + '...' + q(branch) + '?quick_pull=1&title=' + q(title) + '&body=' + q(ghdesc), 'Open the pull request (after the workflow pushed the branch)')); }
      }
      rowg.appendChild(link(gh + '/edit/' + q(cfg.default_branch) + '/' + cfg.manifest_path.split('/').map(q).join('/'), 'Edit manifest.yaml on GitHub (manual fallback)'));
      s4g.appendChild(rowg); out.appendChild(s4g);
      $('composer').hidden = false;
      return;
    }
    var base = cfg.gitlab_url.replace(/\/+$/, '') + '/' + cfg.project_path.replace(/^\/+|\/+$/g, '');
    var pl = base + '/-/pipelines/new?ref=' + q(cfg.ops_ref); vars.forEach(function (k) { if (ci[k] !== '') pl += '&var[' + k + ']=' + q(ci[k]); });
    var desc = 'ops-action ' + d.ci.ACTION + ' ' + (d.ci.TARGET || 'fleet') + '\nReason: ' + (r || '(none)');
    var mr = base + '/-/merge_requests/new?merge_request[source_branch]=' + q(branch) + '&merge_request[target_branch]=' + q(cfg.default_branch) + '&merge_request[title]=' + q(title) + '&merge_request[description]=' + q(desc);
    var edit = base + '/-/edit/' + q(cfg.default_branch) + '/manifest.yaml';
    var s4 = section('GitLab links'); var row = document.createElement('div'); row.className = 'actions';
    if (!needs) { row.appendChild(link(pl, 'Run the pipeline (prefilled)', true)); if (d.kind !== 'trigger') { row.appendChild(link(mr, 'Open the merge request (after the pipeline pushed the branch)')); } }
    row.appendChild(link(edit, 'Edit manifest.yaml in GitLab (manual fallback)'));
    s4.appendChild(row); out.appendChild(s4);
    $('composer').hidden = false;
  }
  function pick(target, idx) {
    var defs = target === '__fleet__' ? data.fleet : data.automations[target]; var def = defs[idx]; if (!def) return;
    cur = { target: target === '__fleet__' ? '' : target, def: def, id: Date.now().toString(36) };
    $('ctitle').textContent = (target === '__fleet__' ? 'Fleet' : target) + ' - ' + def.label;
    $('trigger-fields').hidden = def.kind !== 'trigger';
    if (def.kind === 'trigger') { var s = $('slot'); clear(s); def.slots.forEach(function (id) { var o = document.createElement('option'); o.value = id; o.textContent = id; s.appendChild(o); }); }
    render(); $('composer').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  Array.prototype.forEach.call(document.querySelectorAll('[data-prepare]'), function (b) {
    b.addEventListener('click', function () {
      var t = b.getAttribute('data-prepare'), sel = document.querySelector('select[data-for="' + t + '"]');
      if (sel) pick(t, parseInt(sel.value, 10)); });
  });
  ['reason', 'shell', 'slot', 'params'].forEach(function (id) { $(id).addEventListener('input', render); $(id).addEventListener('change', render); });
  if (location.hash.length > 1) { var row = document.getElementById(decodeURIComponent(location.hash.slice(1))); if (row) row.style.outline = '2px solid var(--accent)'; }
})();
"""


def _skew(entry: dict, order: list[str]) -> bool:
    r = entry.get("rings") or {}
    vs = [r.get(x) for x in order if r.get(x)]
    return len(set(vs)) > 1 or len(vs) < len(order)


def build_admin(m: dict, config: dict, health: dict[str, dict], signals: list[dict], manifest_text: str,
                pending_triggers: list[dict]) -> str:
    order = rings_of(m)
    autos = m.get("automations") or {}
    actions = build_actions(m)
    cfg = dict(config)
    for key in ("gitlab_url",):
        if not safe_url(cfg.get(key, "")) or not str(cfg[key]).startswith(("http://", "https://")):
            cfg[key] = "https://gitlab.example.internal"
    cfg = github_config(cfg)
    data = {"sentinel": SENTINEL, "config": {k: cfg.get(k) for k in ("gitlab_url", "project_path", "default_branch", "ops_ref", "platform",
                                                                       "github_repo", "manifest_path", "ops_workflow")},
            "automations": actions["automations"], "fleet": actions["fleet"]}
    fleet = m.get("fleet") or {}
    paused = [n for n, e in autos.items() if (e or {}).get("status") == "paused"]
    unhealthy = [n for n, h in health.items() if h.get("status") in ("degraded", "failing")]
    pending_promotion = [n for n, e in autos.items() if _skew(e or {}, order)]
    tiles = (f'<div class="tile"><div class="n" style="color:{"var(--red)" if fleet.get("paused") else "var(--green)"}">'
             f'{"PAUSED" if fleet.get("paused") else "RUNNING"}</div><div class="l">Fleet</div></div>'
             f'<div class="tile"><div class="n">{len(autos)}</div><div class="l">Automations</div></div>'
             f'<div class="tile"><div class="n" style="color:var(--yellow)">{len(paused)}</div><div class="l">Paused</div></div>'
             f'<div class="tile"><div class="n">{len(pending_promotion)}</div><div class="l">Mid-rollout</div></div>'
             f'<div class="tile"><div class="n" style="color:{"var(--red)" if unhealthy else "var(--green)"}">{len(unhealthy) if health else "-"}</div>'
             f'<div class="l">Unhealthy</div></div>'
             f'<div class="tile"><div class="n">{len(signals) if signals else ("0" if health else "-")}</div><div class="l">Open signals</div></div>'
             f'<div class="tile"><div class="n">{len(pending_triggers)}</div><div class="l">Queued triggers</div></div>')
    fleet_banner = (f'<div class="banner bad"><strong>Fleet is paused:</strong> {esc(fleet.get("paused_reason"))}</div>' if fleet.get("paused") else "")

    def select(key: str, defs: list[dict]) -> str:
        opts = "".join(f'<option value="{i}">{esc(d["label"])}</option>' for i, d in enumerate(defs))
        return (f'<select data-for="{esc(key)}" aria-label="Action for {esc(key)}">{opts}</select> '
                f'<button type="button" data-prepare="{esc(key)}">Prepare</button>') if defs else '<span class="mono">no actions</span>'

    rows = []
    for name, entry in autos.items():
        entry = entry or {}
        rr = entry.get("rings") or {}
        rows.append(
            f'<tr id="{esc(name)}"><td><a href="automation/{esc(name)}.html" class="mono">{esc(name)}</a><br>{status_pill(entry)} {health_pill(health.get(name))}'
            f'{("<br><span class=mono>" + esc(entry.get("paused_reason")) + "</span>") if entry.get("status") == "paused" else ""}</td>'
            + "".join(f"<td class=mono>{esc(rr.get(r) or '-')}</td>" for r in order)
            + f'<td>{pill("mid-rollout", "warn") if _skew(entry, order) else pill("aligned", "ok")}</td>'
            f'<td>{select(name, actions["automations"].get(name, []))}</td></tr>')
    comp_rows = "".join(
        f'<tr><td class=mono>{esc(n)}</td>' + "".join(f"<td class=mono>{esc(((c or {}).get('rings') or {}).get(r) or '-')}</td>" for r in order)
        + f'<td>{pill("mid-rollout", "warn") if _skew(c or {}, order) else pill("aligned", "ok")}</td></tr>'
        for n, c in (m.get("components") or {}).items())
    ring_head = "".join(f"<th>{esc(r)}</th>" for r in order)
    body = f"""{fleet_banner}
<div class="banner"><strong>How this panel works.</strong> There is no server. Choose an action, review the exact diff, then run the
<code>ops-action</code> pipeline (prefilled link) or paste the CLI command. Manifest changes open a merge request - <em>the approval is the
governance gate</em>. On-demand triggers skip the MR (no manifest change) and go to a protected <code>triggers</code> branch.</div>
<h2>Fleet compliance summary</h2><div class="tiles">{tiles}</div>
<h3>Shared components</h3>
<div class="tbl-wrap"><table><tr><th>Component</th>{ring_head}<th>State</th><th></th></tr>{comp_rows}</table></div>
<h2>Fleet kill-switch</h2>
<div class="panel {'accent' if actions['fleet'] else ''}"><p>Halts every automation at its next self-heal check (no per-machine action). Use for incidents; prefer pausing one automation when possible.</p>
{select('__fleet__', actions['fleet'])}</div>
<h2>Automations</h2>
<div class="tbl-wrap"><table><tr><th>Automation</th>{ring_head}<th>Rollout</th><th>Action</th></tr>{''.join(rows) or '<tr><td colspan=9 class=empty>None registered.</td></tr>'}</table></div>
<div id="composer" class="panel accent" hidden>
  <h2 style="margin-top:0" id="ctitle">Action</h2>
  <div class="field"><label for="reason">Reason (single line, max 300 chars)</label><input id="reason" type="text" maxlength="300" autocomplete="off" placeholder="e.g. upstream data format changed - see INC-1234"></div>
  <div id="trigger-fields" hidden>
    <div class="field"><label for="slot">Poll slot</label><select id="slot"></select></div>
    <div class="field"><label for="params">Parameters (one k=v per line; become AUTOMATION_PARAM_*)</label><textarea id="params" rows="3" placeholder="date_range=yesterday"></textarea></div>
  </div>
  <div class="field"><label for="shell">Shell quoting for the CLI command</label><select id="shell"><option value="bash">bash / sh</option><option value="ps">PowerShell</option></select></div>
  <div id="out"></div>
</div>
<script type="application/json" id="admin-data">{json_for_script(data)}</script>"""
    return layout("Fleet admin", body, config=config, active="admin", manifest_text=manifest_text, extra_js=f"<script>{ADMIN_JS}</script>")
