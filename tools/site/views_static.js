/* Live panels of individual views (Home: fleet right now; Catalog: reliability per automation and the automation's recent runs).
   They read the JSON files the build publishes next to the app (fleet-status.json, metrics.json, dashboard/signals.json, dashboard/runs-latest.json) and
   draw with createElement/textContent only: telemetry-derived text is never parsed as HTML. When a file is missing (no fleet data yet) the panel says so. */
(function () {
  'use strict';
  var NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
  var ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
  var SEV = { critical: 4, high: 3, medium: 2, low: 1 };

  function el(tag, text, cls) {
    var e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = String(text);
    if (cls) e.className = cls;
    return e;
  }
  function link(text, route, cls) {
    var a = el('a', text, cls);
    a.href = '#' + route;
    return a;
  }
  function pill(text, kind) { return el('span', text, 'pill ' + (kind || '')); }
  function getJSON(url) {
    return fetch(AS.url(url), { credentials: 'same-origin' }).then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); }).catch(function () { return null; });
  }
  function pct(x) { return typeof x === 'number' && isFinite(x) ? (Math.round(x * 1000) / 10) + '%' : '-'; }
  function dur(ms) {
    if (typeof ms !== 'number' || !isFinite(ms) || ms < 0) return '-';
    var s = Math.round(ms / 1000);
    return s < 90 ? s + ' s' : s < 5400 ? Math.round(s / 60) + ' min' : (Math.round(s / 360) / 10) + ' h';
  }
  function when(iso) { return typeof iso === 'string' ? iso.replace('T', ' ').replace(/\.\d+Z$|Z$/, ' UTC').slice(0, 24) : '-'; }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function safeName(v) { return typeof v === 'string' && NAME_RE.test(v) ? v : ''; }
  function runKind(status) { return status === 'success' ? 'ok' : status === 'partial' ? 'medium' : (status === 'failed' || status === 'hung' || status === 'self_heal_failed') ? 'failing' : 'paused'; }

  function table(headers, rows) {
    var t = el('table'), tr = el('tr');
    headers.forEach(function (h) { tr.appendChild(el('th', h)); });
    t.appendChild(tr);
    rows.forEach(function (r) {
      var row = el('tr');
      r.forEach(function (c) { var td = el('td'); if (c && c.nodeType) td.appendChild(c); else td.textContent = c === null || c === undefined ? '' : String(c); row.appendChild(td); });
      t.appendChild(row);
    });
    var wrap = el('div', null, 'tw'); wrap.appendChild(t); return wrap;
  }

  /* ------------------------------------------------------------------------------------------------ Home: the fleet right now */
  AS.hook('home/', function () {
    var host = document.getElementById('fleet-now');
    if (!host) return;
    Promise.all([getJSON('fleet-status.json'), getJSON('dashboard/signals.json'), getJSON('dashboard/runs-latest.json'), getJSON('metrics.json')]).then(function (r) {
      var fleet = r[0], sig = r[1], runs = r[2], metrics = r[3];
      clear(host);
      if (!fleet && !runs && !metrics) {
        var w = el('div', null, 'status-banner grey');
        w.appendChild(el('div', null));
        w.firstChild.appendChild(el('b', 'Waiting for the first run'));
        w.firstChild.appendChild(el('div', 'Nothing is charted yet because no installed automation has reported. Install one from the Install tab and let it run once - results appear here within 30 minutes.', 'sub'));
        host.appendChild(w);
        return;
      }
      var status = fleet && ['green', 'amber', 'red'].indexOf(fleet.status) >= 0 ? fleet.status : 'grey';
      var label = { green: 'Fleet healthy', amber: 'Needs attention', red: 'Action needed', grey: 'No fleet verdict yet' }[status];
      var banner = el('div', null, 'status-banner ' + status);
      var txt = el('div'); txt.appendChild(el('b', label));
      if (fleet && fleet.counts) {
        var c = fleet.counts, parts = [];
        ['passing', 'degraded', 'failing', 'paused', 'no data'].forEach(function (k) { if (c[k]) parts.push(c[k] + ' ' + k); });
        txt.appendChild(el('div', parts.join(' - ') + (fleet.generated_at ? '  (as of ' + when(fleet.generated_at) + ')' : ''), 'sub'));
      }
      (fleet && Array.isArray(fleet.reasons) ? fleet.reasons.slice(0, 3) : []).forEach(function (x) { txt.appendChild(el('div', String(x).slice(0, 160), 'sub')); });
      banner.appendChild(txt); host.appendChild(banner);

      var tiles = el('div', null, 'tiles');
      var tile = function (n, l) { var d = el('div', null, 'tile'); d.appendChild(el('div', n, 'n')); d.appendChild(el('div', l, 'l')); tiles.appendChild(d); };
      if (metrics && metrics.automations) {
        var total = 0, failed = 0, budget = 0, names = Object.keys(metrics.automations);
        names.forEach(function (k) { var a = metrics.automations[k] || {}; total += a.runs || 0; failed += a.failed || 0; if (typeof a.error_budget_remaining === 'number' && a.error_budget_remaining <= 0) budget++; });
        tile(names.length, 'Automations reporting'); tile(total, 'Runs (30 days)');
        tile(total ? pct(1 - failed / total) : '-', 'Success rate'); tile(budget, 'Error budgets exhausted');
      }
      var sigs = sig && Array.isArray(sig.signals) ? sig.signals : [];
      tile(sigs.length, 'Open signals');
      host.appendChild(tiles);

      if (sigs.length) {
        host.appendChild(el('h2', 'Open signals'));
        var srt = sigs.slice().sort(function (a, b) { return (SEV[b.severity] || 0) - (SEV[a.severity] || 0); }).slice(0, 6);
        host.appendChild(table(['Severity', 'Signal', 'Automation', 'Detail'], srt.map(function (s) {
          var name = safeName(s.automation);
          return [pill(String(s.severity || 'low'), String(s.severity || 'low')), String(s.type || '').slice(0, 40),
                  name ? link(name, '/jobs?auto=' + encodeURIComponent(name)) : String(s.automation || '-').slice(0, 60), String(s.detail || '').slice(0, 160)];
        })));
        host.appendChild(link('All signals and diagnoses on the Dashboard', '/dashboard'));
      }
      var rs = runs && Array.isArray(runs.runs) ? runs.runs.slice().sort(function (a, b) { return String(b.started_at || '').localeCompare(String(a.started_at || '')); }).slice(0, 8) : [];
      if (rs.length) {
        host.appendChild(el('h2', 'Latest runs'));
        host.appendChild(table(['Status', 'Automation', 'Machine', 'Started', 'Duration', ''], rs.map(function (x) {
          var id = ID_RE.test(String(x.run_id || '')) ? x.run_id : '';
          return [pill(String(x.status || '?'), runKind(x.status)), String(x.automation || '').slice(0, 80), String(x.host || '-').slice(0, 60), when(x.started_at), dur(x.duration_ms),
                  id ? link('details', '/jobs?run=' + encodeURIComponent(id)) : ''];
        })));
        host.appendChild(link('All runs on the Jobs tab', '/jobs'));
      }
    });
  });

  /* ------------------------------------------------------------------------------------------ Catalog: reliability + recent runs */
  AS.hook('catalog', function (root) {
    var cards = Array.prototype.slice.call(root.querySelectorAll('article.card[data-name]'));
    var live = document.getElementById('live-runs');
    if (!cards.length && !live) return;
    Promise.all([getJSON('metrics.json'), getJSON('dashboard/runs-latest.json')]).then(function (r) {
      var metrics = (r[0] && r[0].automations) || {}, runs = (r[1] && Array.isArray(r[1].runs)) ? r[1].runs : [];
      cards.forEach(function (card) {
        var name = card.getAttribute('data-name'), m = metrics[name], dl = card.querySelector('dl.kv');
        if (!m || !dl) return;
        var dt = el('dt', 'Reliability'), dd = el('dd', pct(m.success_ratio) + ' over ' + (m.runs || 0) + ' runs; error budget left ' + pct(m.error_budget_remaining) + ' (SLO ' + pct(m.slo_target) + ')');
        dl.appendChild(dt); dl.appendChild(dd);
      });
      if (!live) return;
      var name = live.getAttribute('data-automation');
      clear(live);
      live.appendChild(el('h2', 'Live health'));
      var m = metrics[name];
      if (!m) { live.appendChild(el('p', 'No run telemetry for this automation yet.', 'sub')); }
      else {
        var tiles = el('div', null, 'tiles');
        var tile = function (n, l) { var d = el('div', null, 'tile'); d.appendChild(el('div', n, 'n')); d.appendChild(el('div', l, 'l')); tiles.appendChild(d); };
        tile(pct(m.success_ratio), 'Success (30 d)'); tile(pct(m.error_budget_remaining), 'Error budget left'); tile(m.runs || 0, 'Runs');
        tile(dur(m.duration_ms && m.duration_ms.p95), 'p95 duration'); tile(typeof m.mttr_seconds === 'number' ? dur(m.mttr_seconds * 1000) : '-', 'MTTR');
        live.appendChild(tiles);
      }
      var mine = runs.filter(function (x) { return x.automation === name; }).sort(function (a, b) { return String(b.started_at || '').localeCompare(String(a.started_at || '')); }).slice(0, 10);
      live.appendChild(el('h3', 'Recent runs'));
      if (!mine.length) live.appendChild(el('p', 'None yet.', 'sub'));
      else live.appendChild(table(['Status', 'Machine', 'Started', 'Duration', 'Synopsis', ''], mine.map(function (x) {
        var id = ID_RE.test(String(x.run_id || '')) ? x.run_id : '';
        return [pill(String(x.status || '?'), runKind(x.status)), String(x.host || '-').slice(0, 60), when(x.started_at), dur(x.duration_ms), String(x.synopsis || '').slice(0, 120),
                id ? link('details', '/jobs?run=' + encodeURIComponent(id)) : ''];
      })));
      var nav = el('p');
      [['All jobs', '/jobs?auto=' + encodeURIComponent(name)], ['Reliability', '/reliability'], ['Errors', '/errors'], ['Schedules', '/schedules']].forEach(function (l, i) {
        if (i) nav.appendChild(document.createTextNode(' · ')); nav.appendChild(link(l[0], l[1]));
      });
      live.appendChild(nav);
    });
  });
})();
