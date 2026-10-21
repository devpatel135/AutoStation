/* Jobs page: client-side filter / sort / paging over the server-rendered table, a details drawer fed by runs-detail.json,
   and deep links in the URL hash (#q=..&status=..&auto=..&host=..&ring=..&sort=ts:desc&page=2&run=<id>).
   Safety: every value that came from telemetry is put on the page with textContent / setAttribute only (no HTML parsing, no dynamic code). */
(function () {
  'use strict';
  var table = document.getElementById('jobs');
  if (!table) return;
  var tbody = table.tBodies[0];
  var allRows = Array.prototype.slice.call(tbody.rows);
  var PAGE = 50;
  var F = { q: document.getElementById('f-q'), status: document.getElementById('f-status'), auto: document.getElementById('f-auto'),
            host: document.getElementById('f-host'), ring: document.getElementById('f-ring') };
  var state = { sort: 'ts', dir: 'desc', page: 1, run: '' };
  var ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
  var detail = null, detailPromise = null;

  document.getElementById('jobfilter').addEventListener('submit', function (e) { e.preventDefault(); });

  function readHash() {
    var p = new URLSearchParams((location.hash || '').replace(/^#/, ''));
    ['q', 'status', 'auto', 'host', 'ring'].forEach(function (k) {
      var el = F[k], v = (p.get(k) || '').slice(0, 100);
      if (!el) return;
      if (el.tagName === 'SELECT') { var ok = Array.prototype.some.call(el.options, function (o) { return o.value === v; }); el.value = ok ? v : ''; }
      else el.value = v;
    });
    var s = (p.get('sort') || 'ts:desc').split(':');
    state.sort = ['status', 'auto', 'host', 'ring', 'ts', 'dur'].indexOf(s[0]) >= 0 ? s[0] : 'ts';
    state.dir = s[1] === 'asc' ? 'asc' : 'desc';
    state.page = Math.max(1, parseInt(p.get('page') || '1', 10) || 1);
    state.run = ID_RE.test(p.get('run') || '') ? p.get('run') : '';
  }

  function writeHash() {
    var p = new URLSearchParams();
    ['q', 'status', 'auto', 'host', 'ring'].forEach(function (k) { if (F[k] && F[k].value) p.set(k, F[k].value); });
    if (state.sort !== 'ts' || state.dir !== 'desc') p.set('sort', state.sort + ':' + state.dir);
    if (state.page > 1) p.set('page', String(state.page));
    if (state.run) p.set('run', state.run);
    var h = p.toString();
    try { history.replaceState(null, '', location.pathname + location.search + (h ? '#' + h : '')); } catch (e) { /* file:// */ }
  }

  function render() {
    var q = F.q.value.trim().toLowerCase();
    var rows = allRows.filter(function (r) {
      var d = r.dataset;
      return (!q || d.q.indexOf(q) >= 0) && (!F.status.value || d.status === F.status.value) && (!F.auto.value || d.auto === F.auto.value) &&
             (!F.host.value || d.host === F.host.value) && (!F.ring.value || d.ring === F.ring.value);
    });
    var numeric = state.sort === 'ts' || state.sort === 'dur';
    rows.sort(function (a, b) {
      var x = a.dataset[state.sort], y = b.dataset[state.sort];
      var c = numeric ? (Number(x) - Number(y)) : String(x).localeCompare(String(y));
      return state.dir === 'asc' ? c : -c;
    });
    var pages = Math.max(1, Math.ceil(rows.length / PAGE));
    if (state.page > pages) state.page = pages;
    var from = (state.page - 1) * PAGE;
    allRows.forEach(function (r) { r.hidden = true; });
    rows.slice(from, from + PAGE).forEach(function (r) { r.hidden = false; tbody.appendChild(r); });
    document.getElementById('job-count').textContent = rows.length + ' of ' + allRows.length + ' run(s)';
    document.getElementById('pageinfo').textContent = 'Page ' + state.page + ' of ' + pages;
    document.getElementById('prev').disabled = state.page <= 1;
    document.getElementById('next').disabled = state.page >= pages;
    Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th) {
      if (th.dataset.sort) th.setAttribute('aria-sort', th.dataset.sort === state.sort ? (state.dir === 'asc' ? 'ascending' : 'descending') : 'none');
    });
    writeHash();
  }

  Object.keys(F).forEach(function (k) {
    F[k].addEventListener(F[k].tagName === 'SELECT' ? 'change' : 'input', function () { state.page = 1; render(); });
  });
  Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th) {
    if (!th.dataset.sort) return;
    th.style.cursor = 'pointer';
    th.tabIndex = 0;
    var go = function () {
      if (state.sort === th.dataset.sort) state.dir = state.dir === 'asc' ? 'desc' : 'asc'; else { state.sort = th.dataset.sort; state.dir = 'asc'; }
      state.page = 1; render();
    };
    th.addEventListener('click', go);
    th.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
  });
  document.getElementById('prev').addEventListener('click', function () { state.page--; render(); });
  document.getElementById('next').addEventListener('click', function () { state.page++; render(); });

  // ---- drawer ---------------------------------------------------------------------------------------------------------
  var drawer = document.getElementById('drawer'), dbody = document.getElementById('drawer-body'), dtitle = document.getElementById('drawer-title');

  function el(tag, text, cls) { var e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = String(text); if (cls) e.className = cls; return e; }
  function kv(parent, k, v) { if (v === undefined || v === null || v === '') return; parent.appendChild(el('b', k)); parent.appendChild(el('span', v)); }
  function section(title) { dbody.appendChild(el('h3', title)); }

  function loadDetail() {
    if (!detailPromise) detailPromise = fetch('runs-detail.json', { credentials: 'same-origin' }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (j) { detail = j; return j; });
    return detailPromise;
  }

  function show(id) {
    if (!ID_RE.test(id)) return;
    state.run = id; writeHash();
    drawer.hidden = false;
    dtitle.textContent = 'Run ' + id;
    dbody.textContent = 'Loading...';
    loadDetail().then(function (all) {
      var d = Object.prototype.hasOwnProperty.call(all, id) ? all[id] : null;
      dbody.textContent = '';
      if (!d) { dbody.appendChild(el('p', 'No details were kept for this run.')); return; }
      var grid = el('div', null, 'kv'); dbody.appendChild(grid);
      kv(grid, 'Automation version', d.version); kv(grid, 'wrapper-core', d.wrapper); kv(grid, 'Slot', d.slot);
      kv(grid, 'Exit code', d.exit_code);
      Object.keys(d.result || {}).forEach(function (k) { kv(grid, 'result.' + k, d.result[k]); });
      if (d.error) {
        section('Error');
        var g = el('div', null, 'kv'); dbody.appendChild(g);
        kv(g, 'Category', d.error.category); kv(g, 'Type', d.error.type); kv(g, 'Attempts', d.error.attempts); kv(g, 'Message', d.error.message);
      }
      section('Retry audit (' + (d.retries || []).length + ')');
      if (!(d.retries || []).length) dbody.appendChild(el('p', 'No retries.', 'sub'));
      (d.retries || []).forEach(function (r) {
        dbody.appendChild(el('div', 'attempt ' + r.attempt + ': ' + r.decision + ' (' + r.category + ')' + (r.wait_s ? ', waited ' + r.wait_s + ' s' : '') + (r.outcome ? ' -> ' + r.outcome : ''), 'diag'));
      });
      section('Events (first ' + (d.events || []).length + ')');
      var ul = el('ul'); (d.events || []).forEach(function (e) {
        var parts = [e.ts, e.event]; Object.keys(e.data || {}).forEach(function (k) { parts.push(k + '=' + e.data[k]); });
        ul.appendChild(el('li', parts.join('  '), 'diag'));
      }); dbody.appendChild(ul);
      section('Failure evidence');
      if (!(d.evidence || []).length) dbody.appendChild(el('p', 'None captured for this run.', 'sub'));
      (d.evidence || []).forEach(function (u) {
        if (typeof u !== 'string' || !/^\.\.\/dashboard\/evidence\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(u)) return;
        var a = el('a', u.split('/').pop()); a.setAttribute('href', u); a.setAttribute('rel', 'noopener'); dbody.appendChild(a); dbody.appendChild(document.createElement('br'));
      });
    }).catch(function () { dbody.textContent = 'Could not load run details (runs-detail.json).'; });
  }

  function hide() { drawer.hidden = true; state.run = ''; writeHash(); }
  document.getElementById('drawer-close').addEventListener('click', hide);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !drawer.hidden) hide(); });
  tbody.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('button.openrun');
    if (b) show(b.getAttribute('data-run'));
  });
  window.addEventListener('hashchange', function () { readHash(); render(); if (state.run) show(state.run); else drawer.hidden = true; });

  readHash(); render();
  if (state.run) show(state.run);
})();
