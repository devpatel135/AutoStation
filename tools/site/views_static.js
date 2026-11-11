/* Live panels of the Home and Fleet views. They read the JSON files the build publishes next to the app (fleet-status.json, metrics.json,
   dashboard/signals.json, dashboard/runs-latest.json) and draw with createElement/textContent only: telemetry-derived text is never parsed
   as HTML. When a file is missing (no fleet data yet) the panel says so in plain words. */
(function () {
  'use strict';
  var NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
  var ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
  var SEV = { critical: 4, high: 3, medium: 2, low: 1 };
  /* plain-language names for the signal types the analytics produce (unknown types are shown as they are, escaped) */
  var PLAIN = {
    FAILED: 'A run failed', MISSED: 'A scheduled run did not happen', SILENT: 'A machine stopped reporting', HUNG: 'A run hung',
    VERSION_DRIFT: 'A machine is not on the latest version', ERROR_BUDGET_BURN: 'Failing more often than allowed', FLAKY: 'Keeps switching between pass and fail',
    DURATION_ANOMALY: 'Taking much longer than usual', REGRESSION_AFTER_RELEASE: 'Started failing after a release', REMEDIATION_FAILING: 'The automatic fix keeps failing',
    PERSISTENT_MANUAL_STEP: 'Waiting for a person', MACHINE_UNHEALTHY: 'A machine keeps failing', ITEM_FAILURES: 'Some items failed', PAUSED: 'Paused'
  };

  function el(tag, text, cls) {
    var e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = String(text);
    if (cls) e.className = cls;
    return e;
  }
  function link(text, route, cls) { var a = el('a', text, cls); a.href = '#' + route; return a; }
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
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
  /* the site stylesheet knows these pill kinds: ok (green), warn (amber), bad (red), info, sys (neutral) */
  function runKind(status) { return status === 'success' ? 'ok' : status === 'partial' ? 'warn' : (status === 'failed' || status === 'hung' || status === 'self_heal_failed') ? 'bad' : 'sys'; }
  function sevKind(sev) { return sev === 'critical' || sev === 'high' ? 'bad' : sev === 'medium' ? 'warn' : 'sys'; }
  function newest(runs) { return runs.slice().sort(function (a, b) { return String(b.started_at || '').localeCompare(String(a.started_at || '')); }); }
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

  /* the one-sentence verdict used on Home and at the top of Fleet */
  function verdict(fleet, nSignals) {
    var status = fleet && ['green', 'amber', 'red'].indexOf(fleet.status) >= 0 ? fleet.status : 'grey';
    var text;
    if (status === 'green') text = 'Everything is running';
    else if (status === 'grey') text = 'Waiting for the first run';
    else if (nSignals > 0) text = (status === 'red' ? 'Action needed: ' : '') + plural(nSignals, 'problem needs attention', 'problems need attention');
    else text = status === 'red' ? 'Action needed' : 'Needs attention';
    var detail = '';
    if (status === 'grey') detail = 'Nothing is shown yet because no installed automation has reported. Install one from the Install tab and let it run once; results appear here within 30 minutes.';
    else if (fleet && fleet.counts) {
      var c = fleet.counts, parts = [];
      ['passing', 'degraded', 'failing', 'paused', 'no data'].forEach(function (k) { if (c[k]) parts.push(c[k] + ' ' + k); });
      detail = parts.join(' - ') + (fleet.generated_at ? '  (as of ' + when(fleet.generated_at) + ')' : '');
    }
    return { status: status, text: text, detail: detail };
  }
  function banner(v) {
    var b = el('div', null, 'status-banner ' + v.status), t = el('div');
    t.appendChild(el('b', v.text));
    if (v.detail) t.appendChild(el('div', v.detail, 'sub'));
    b.appendChild(t);
    return b;
  }
  function signalTitle(s) { var t = String(s.type || ''); return PLAIN[t] || t.slice(0, 60) || 'Problem'; }
  function sortedSignals(sig) {
    var sigs = sig && Array.isArray(sig.signals) ? sig.signals : [];
    return sigs.slice().sort(function (a, b) { return (SEV[b.severity] || 0) - (SEV[a.severity] || 0); });
  }
  function totals(metrics) {
    var out = { names: 0, runs: 0, failed: 0 };
    if (!metrics || !metrics.automations) return out;
    Object.keys(metrics.automations).forEach(function (k) { var a = metrics.automations[k] || {}; out.names++; out.runs += a.runs || 0; out.failed += a.failed || 0; });
    return out;
  }

  /* ------------------------------------------------------------------------------------------------------------------ Home */
  AS.hook('home', function () {
    var host = document.getElementById('fleet-now');
    if (!host) return;
    Promise.all([getJSON('fleet-status.json'), getJSON('dashboard/signals.json'), getJSON('metrics.json')]).then(function (r) {
      var fleet = r[0], sigs = sortedSignals(r[1]), t = totals(r[2]);
      clear(host);
      var v = verdict(fleet, sigs.length);
      host.appendChild(banner(v));
      if (v.status === 'grey') return;
      var tiles = el('div', null, 'tiles');
      var tile = function (n, l) { var d = el('div', null, 'tile'); d.appendChild(el('div', n, 'n')); d.appendChild(el('div', l, 'l')); tiles.appendChild(d); };
      tile(t.names, 'Automations reporting'); tile(t.runs, 'Runs (30 days)'); tile(t.runs ? pct(1 - t.failed / t.runs) : '-', 'Success rate'); tile(sigs.length, 'Open problems');
      host.appendChild(tiles);
      if (sigs.length) {
        host.appendChild(el('h2', 'Needs attention'));
        var ul = el('ul', null, 'attn');
        sigs.slice(0, 5).forEach(function (s) {
          var li = el('li'), name = safeName(s.automation);
          li.appendChild(pill(String(s.severity || 'low'), sevKind(s.severity)));
          li.appendChild(el('span', signalTitle(s) + (name ? ' - ' + name : '')));
          ul.appendChild(li);
        });
        host.appendChild(ul);
        host.appendChild(link('See all ' + sigs.length + ' on the Fleet page', '/fleet/problems'));
      }
    });
  });

  /* ------------------------------------------------------------------------------------------------------------------ Fleet */
  AS.hook('fleet', function (root) {
    var byName = function (list, n) { return list.filter(function (x) { return x.automation === n; }); };
    Promise.all([getJSON('fleet-status.json'), getJSON('dashboard/signals.json'), getJSON('dashboard/runs-latest.json'), getJSON('metrics.json')]).then(function (r) {
      var fleet = r[0], sigs = sortedSignals(r[1]), runs = r[2] && Array.isArray(r[2].runs) ? newest(r[2].runs) : [], metrics = (r[3] && r[3].automations) || {}, t = totals(r[3]);
      var v = verdict(fleet, sigs.length);

      var fb = document.getElementById('fleet-banner');
      if (fb) { clear(fb); fb.appendChild(banner(v)); }

      /* Problems: a short list, most serious first; the long-form analysis stays one click away */
      var pl = document.getElementById('problems-list');
      if (pl) {
        clear(pl);
        if (!sigs.length) pl.appendChild(el('p', v.status === 'grey' ? 'Nothing to show yet.' : 'No problems right now.', 'sub'));
        sigs.slice(0, 25).forEach(function (s) {
          var d = el('div', null, 'p ' + String(s.severity || 'low').replace(/[^a-z]/g, ''));
          d.appendChild(pill(String(s.severity || 'low'), sevKind(s.severity)));
          var name = safeName(s.automation), host = typeof s.host === 'string' ? s.host.slice(0, 60) : '';
          d.appendChild(el('span', signalTitle(s) + (name ? ' - ' + name : '') + (host ? ' on ' + host : ''), 't'));
          if (name) d.appendChild(link('Its runs', '/fleet/runs?auto=' + encodeURIComponent(name)));
          else d.appendChild(el('span', ''));
          if (s.detail) d.appendChild(el('div', String(s.detail).slice(0, 240), 'd'));
          pl.appendChild(d);
        });
        if (sigs.length > 25) pl.appendChild(el('p', '+ ' + (sigs.length - 25) + ' more', 'sub'));
      }

      /* one-sentence headline under every section title */
      var rows = function (sel) { var tb = root.querySelector(sel + ' tbody'); return tb ? tb.rows.length : 0; };
      var leads = {
        problems: sigs.length ? plural(sigs.length, 'open problem', 'open problems') + ', most serious first.' : 'No problems right now.',
        automations: plural(rows('#autos') / 2, 'automation', 'automations') + '; click a name for versions, schedule and recent runs.',
        runs: t.runs ? plural(t.runs, 'run', 'runs') + ' in the last 30 days, ' + t.failed + ' failed.' : 'No runs have been reported yet.',
        machines: rows('#sec-machines') ? plural(rows('#sec-machines'), 'machine', 'machines') + ' reporting.' : 'No machine has reported yet.',
        schedules: rows('#sec-schedules') ? plural(rows('#sec-schedules'), 'scheduled slot', 'scheduled slots') + ', with the next run times.' : 'Nothing is scheduled yet.',
        releases: 'What was released to which ring, who changed what, and whether the registry is signed.',
        admin: 'Pause, resume, promote, roll back or run now. Each action produces the exact command to run.',
        value: ''
      };
      Array.prototype.forEach.call(root.querySelectorAll('[data-lead]'), function (p) { var k = p.getAttribute('data-lead'); if (Object.prototype.hasOwnProperty.call(leads, k)) p.textContent = leads[k]; });
      var vl = root.querySelector('[data-lead="value"]'); if (vl) vl.textContent = 'hours saved and cost (click to open)';

      /* Automations: success rate and last run per row, recent runs in the row's details */
      Array.prototype.forEach.call(root.querySelectorAll('tr[data-auto]'), function (tr) {
        var name = tr.getAttribute('data-auto'), m = metrics[name], mine = byName(runs, name);
        var ok = tr.querySelector('[data-cell="ok"]'), last = tr.querySelector('[data-cell="last"]');
        if (ok && m && typeof m.success_ratio === 'number') ok.textContent = pct(m.success_ratio) + ' (' + (m.runs || 0) + ')';
        if (last && mine.length) { clear(last); last.appendChild(pill(String(mine[0].status || '?'), runKind(mine[0].status))); last.appendChild(document.createTextNode(' ' + when(mine[0].started_at))); }
        var rec = root.querySelector('.auto-recent[data-recent="' + name.replace(/"/g, '') + '"]');
        if (rec) {
          clear(rec);
          rec.appendChild(el('h3', 'Recent runs'));
          if (!mine.length) rec.appendChild(el('p', 'None yet.', 'sub'));
          else rec.appendChild(table(['Status', 'Machine', 'Started', 'Duration', ''], mine.slice(0, 6).map(function (x) {
            var id = ID_RE.test(String(x.run_id || '')) ? x.run_id : '';
            return [pill(String(x.status || '?'), runKind(x.status)), String(x.host || '-').slice(0, 60), when(x.started_at), dur(x.duration_ms), id ? link('details', '/fleet/runs?run=' + encodeURIComponent(id)) : ''];
          })));
        }
      });
    });

    /* row toggles (no inline handlers: the CSP forbids them) */
    Array.prototype.forEach.call(root.querySelectorAll('button.rowbtn[aria-controls]'), function (b) {
      b.addEventListener('click', function () {
        var d = document.getElementById(b.getAttribute('aria-controls')); if (!d) return;
        var open = d.hasAttribute('hidden');
        if (open) d.removeAttribute('hidden'); else d.setAttribute('hidden', '');
        b.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
    });

    /* the jump menu highlights the section you are in */
    var jumps = Array.prototype.slice.call(root.querySelectorAll('.jump a[data-jump]'));
    var secs = jumps.map(function (a) { return document.getElementById('sec-' + a.getAttribute('data-jump')); });
    var ticking = false;
    function mark() {
      ticking = false;
      var cur = -1;
      secs.forEach(function (s, i) { if (s && s.getBoundingClientRect().top <= 170) cur = i; });
      jumps.forEach(function (a, i) { if (i === cur) a.classList.add('on'); else a.classList.remove('on'); });
    }
    function onScroll() { if (!ticking) { ticking = true; window.requestAnimationFrame(mark); } }
    if (window.__asFleetScroll) window.removeEventListener('scroll', window.__asFleetScroll);   /* one scroll listener, however often the view is mounted */
    window.__asFleetScroll = onScroll;
    window.addEventListener('scroll', onScroll, { passive: true });
    jumps.forEach(function (a) {                                     /* a click on the current section still scrolls to it */
      a.addEventListener('click', function () { if (location.hash === a.getAttribute('href')) { var s = document.getElementById('sec-' + a.getAttribute('data-jump')); if (s) s.scrollIntoView(); } });
    });
    mark();
  });
})();
