/* Global search over search-ops.json (automations, machines, error signatures, recent runs, pages).
   Safety: results are built with createElement + textContent only (no HTML parsing, no dynamic code); a link target must be a
   same-site relative path of safe characters, so a forged index entry can never produce a javascript: or off-site URL. */
(function () {
  'use strict';
  var input = document.getElementById('gsearch');
  var list = document.getElementById('gs-results');
  if (!input || !list) return;
  var base = (document.currentScript && document.currentScript.getAttribute('data-base')) || '';
  if (!/^(\.\.\/)*$/.test(base)) base = '';
  var SAFE = /^[A-Za-z0-9_\-\/.#=&%]+$/;
  var index = null, loading = null, active = -1, shown = [];

  function load() {
    if (index) return Promise.resolve(index);
    if (!loading) {
      loading = fetch(base + 'search-ops.json', { credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
        .then(function (j) { index = Array.isArray(j) ? j.slice(0, 1000) : []; return index; })
        .catch(function () { index = []; return index; });
    }
    return loading;
  }

  function safeHref(u) {
    if (typeof u !== 'string' || u.length > 300 || !SAFE.test(u) || u.charAt(0) === '/' || u.indexOf('..') >= 0 || u.indexOf(':') >= 0) return null;
    return base + u;
  }

  function hide() { list.hidden = true; active = -1; }

  function render(items) {
    while (list.firstChild) list.removeChild(list.firstChild);
    shown = [];
    items.forEach(function (it) {
      var href = safeHref(it.u);
      if (!href) return;
      var li = document.createElement('li');
      var a = document.createElement('a');
      a.href = href;
      var t = document.createElement('span');
      t.className = 'gs-t';
      t.textContent = String(it.t || '').slice(0, 20);
      var n = document.createElement('span');
      n.className = 'gs-n';
      n.textContent = String(it.n || '').slice(0, 120);
      var s = document.createElement('span');
      s.className = 'gs-s';
      s.textContent = String(it.s || '').slice(0, 120);
      a.appendChild(t); a.appendChild(n); a.appendChild(s);
      li.appendChild(a);
      list.appendChild(li);
      shown.push(a);
    });
    if (!shown.length) {
      var e = document.createElement('li');
      e.className = 'gs-empty';
      e.textContent = 'No matches';
      list.appendChild(e);
    }
    list.hidden = false;
    active = -1;
  }

  function search() {
    var q = input.value.trim().toLowerCase().slice(0, 80);
    if (q.length < 2) { hide(); return; }
    load().then(function (idx) {
      var words = q.split(/\s+/);
      var hits = idx.filter(function (it) {
        var hay = (String(it.n || '') + ' ' + String(it.s || '') + ' ' + String(it.t || '')).toLowerCase();
        return words.every(function (w) { return hay.indexOf(w) >= 0; });
      }).slice(0, 12);
      render(hits);
    });
  }

  input.addEventListener('input', search);
  input.addEventListener('focus', function () { if (input.value.trim().length >= 2) search(); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { hide(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!shown.length) return;
      e.preventDefault();
      active = (active + (e.key === 'ArrowDown' ? 1 : -1) + shown.length) % shown.length;
      shown[active].focus();
    } else if (e.key === 'Enter' && shown.length) {
      e.preventDefault();
      (shown[active >= 0 ? active : 0]).click();
    }
  });
  list.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { hide(); input.focus(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      active = (active + (e.key === 'ArrowDown' ? 1 : -1) + shown.length) % shown.length;
      shown[active].focus();
    }
  });
  document.addEventListener('click', function (e) { if (!list.contains(e.target) && e.target !== input) hide(); });
  document.getElementById('gs-form').addEventListener('submit', function (e) { e.preventDefault(); });
})();
