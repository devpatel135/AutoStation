/* AutoStation single-page app: one shell, FOUR tabs (Home, Install, Fleet, Docs), one router.
   Routes are hash routes:  #/<tab>[/<sub>][?key=value&...]   e.g. #/fleet/runs?status=failed  #/fleet/a/<automation>  #/install/<automation>  #/docs/RUNBOOKS?a=<heading-id>
   Fleet and Install are ONE scrolling view each: the second part of their route only scrolls to a section / opens a row inside it. Docs has a view per document.
   Old routes (#/jobs, #/catalog/a/x, #/admin ...) are aliased to their place in the four tabs.
   Every tab's content is a build-time HTML string in views/<tab>.json ({ "": main view, "<sub>": sub view }); it is produced by this
   generator with every telemetry-derived value already escaped, so it is mounted with innerHTML. Nothing else is ever parsed as HTML:
   anything built in the browser uses createElement/textContent. Scripts that belong to a view register with AS.hook() and run after mount.
   Same-origin only: no CDN, no inline script (the CSP allows only this file's origin plus the theme bootstrap by SHA-256). */
(function () {
  'use strict';
  var TAB_RE = /^[a-z][a-z0-9-]{0,30}$/;
  var SUB_RE = /^[A-Za-z0-9_.\-\/]{0,160}$/;
  var NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
  var SINGLE = { fleet: 1, install: 1 };
  var OLD = { jobs: 'fleet/runs', machines: 'fleet/machines', catalog: 'fleet/automations', dashboard: 'fleet/problems', program: 'fleet/value', reliability: 'fleet/problems',
              errors: 'fleet/problems', queues: 'fleet/problems', schedules: 'fleet/schedules', audit: 'fleet/releases', admin: 'fleet/admin' };
  var OLD_CATALOG = { overview: 'fleet', health: 'fleet/problems', fleet: 'fleet/machines', audit: 'fleet/releases', compatibility: 'fleet/automations', governance: 'docs/GOVERNANCE' };
  var view = document.getElementById('view');
  var tabs = Array.prototype.slice.call(document.querySelectorAll('#tabs a[data-tab]'));
  var tabNames = tabs.map(function (a) { return a.getAttribute('data-tab'); });
  var LABEL = {};
  tabs.forEach(function (a) { LABEL[a.getAttribute('data-tab')] = a.textContent; });
  var cache = {}, hooks = {}, paramHandlers = [], current = { tab: null, sub: null }, silent = 0, seq = 0;
  /* every build gets an id (<meta name="build">): data files are fetched with ?v=<id> so a new deployment is never mixed with cached old views */
  var BUILD = ((document.querySelector('meta[name="build"]') || {}).content || '');
  if (!/^[a-f0-9]{6,20}$/.test(BUILD)) BUILD = '';

  var AS = window.AS = {
    /* register a function to run (once per mount) after the view `key` ("tab" or "tab/sub") has been inserted */
    hook: function (key, fn) { (hooks[key] = hooks[key] || []).push(fn); },
    /* current query parameters of the route, as URLSearchParams */
    params: function () { return new URLSearchParams(parse().query); },
    /* replace the query of the current route without re-rendering (used for filters / selected rows) */
    setParams: function (obj) {
      var p = new URLSearchParams();
      Object.keys(obj || {}).forEach(function (k) { if (obj[k] !== '' && obj[k] != null) p.set(k, String(obj[k])); });
      var q = p.toString(), r = parse(), h = '#/' + r.path + (q ? '?' + q : '');
      silent++;
      try { history.replaceState(null, '', location.pathname + location.search + h); } catch (e) { location.hash = h; }
      setTimeout(function () { silent = Math.max(0, silent - 1); }, 0);
    },
    /* called when the user navigates (back/forward, a link) inside the same view and only the query changed */
    onParams: function (fn) { paramHandlers.push(fn); },
    anchor: function () { return new URLSearchParams(parse().query).get('a') || ''; },
    route: function () { return parse(); },
    href: function (path, query) { return '#/' + path + (query ? '?' + query : ''); },
    /* a data URL of this build (cache-busted); only same-origin relative paths */
    url: function (u) { return BUILD && /^[A-Za-z0-9_\-\/.]+$/.test(u) ? u + '?v=' + BUILD : u; }
  };

  function parse() {
    var raw = (location.hash || '').replace(/^#/, '');
    var q = raw.indexOf('?'), path = q >= 0 ? raw.slice(0, q) : raw, query = q >= 0 ? raw.slice(q + 1) : '';
    path = path.replace(/^\/+/, '').replace(/\/+$/, '');
    var parts = path.split('/');
    var tab = parts[0] || 'home', sub = parts.slice(1).join('/'), aliased = false;
    if (TAB_RE.test(tab) && tabNames.indexOf(tab) < 0 && Object.prototype.hasOwnProperty.call(OLD, tab)) {      /* an old route: same place, new address */
      var target = OLD[tab];
      if (tab === 'catalog') {
        if (sub.indexOf('a/') === 0 && NAME_RE.test(sub.slice(2))) target = 'fleet/' + sub;
        else if (Object.prototype.hasOwnProperty.call(OLD_CATALOG, sub)) target = OLD_CATALOG[sub];
      }
      var tp = target.split('/'); tab = tp[0]; sub = tp.slice(1).join('/'); aliased = true;
    }
    if (!TAB_RE.test(tab) || tabNames.indexOf(tab) < 0) { tab = 'home'; sub = ''; path = 'home'; }
    if (!SUB_RE.test(sub) || sub.indexOf('..') >= 0) sub = '';
    return { tab: tab, sub: sub, path: tab + (sub ? '/' + sub : ''), query: query, aliased: aliased };
  }

  function load(tab) {
    if (cache[tab]) return Promise.resolve(cache[tab]);
    return fetch(AS.url('views/' + tab + '.json'), { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
      .then(function (j) { cache[tab] = (j && typeof j === 'object') ? j : {}; return cache[tab]; });
  }

  function message(title, text) {
    while (view.firstChild) view.removeChild(view.firstChild);
    var h = document.createElement('h1'); h.textContent = title;
    var p = document.createElement('p'); p.textContent = text;
    view.appendChild(h); view.appendChild(p);
  }

  function setActive(tab) {
    tabs.forEach(function (a) {
      var on = a.getAttribute('data-tab') === tab;
      a.setAttribute('aria-selected', on ? 'true' : 'false');
      a.tabIndex = on ? 0 : -1;
      if (on) { a.setAttribute('aria-current', 'page'); view.setAttribute('aria-labelledby', a.id); } else { a.removeAttribute('aria-current'); }
    });
    var act = document.getElementById('tab-' + tab);
    if (act && act.scrollIntoView) { try { act.scrollIntoView({ block: 'nearest', inline: 'center' }); } catch (e) { /* old browser */ } }
  }

  function scrollToAnchor() {
    var a = AS.anchor();
    var el = a && /^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,120}$/.test(a) ? document.getElementById(a) : null;
    if (el) { reveal(el); el.scrollIntoView(); return true; }
    return false;
  }

  /* open a <details> (and any collapsed ancestors) or reveal a hidden table row so a link can land on it */
  function reveal(el) {
    var n = el;
    while (n && n !== view) {
      if (n.tagName === 'DETAILS') n.open = true;
      n = n.parentNode;
    }
    if (el.hasAttribute && el.hasAttribute('hidden')) {
      el.removeAttribute('hidden');
      var b = el.id ? view.querySelector('[aria-controls="' + el.id + '"]') : null;
      if (b) b.setAttribute('aria-expanded', 'true');
    }
  }

  /* sections that load on demand (the Runs table): fetched once, when they come near the viewport or a link targets them */
  function loadLazy(host) {
    if (host.__p) return host.__p;
    var name = host.getAttribute('data-lazy') || '';
    if (!TAB_RE.test(name)) return Promise.resolve();
    host.__p = fetch(AS.url('views/' + name + '.json'), { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
      .then(function (map) {
        var html = map && map[''];
        if (typeof html !== 'string') throw new Error('no view');
        host.innerHTML = html;                                      /* build-time HTML, escaped by the generator */
        host.removeAttribute('aria-busy');
        common(host);
        (hooks['lazy:' + name] || []).forEach(function (fn) { try { fn(host); } catch (e) { if (window.console) console.error('view script failed', name, e); } });
      })
      .catch(function () { host.textContent = 'Could not load this section. Reload the page; if it persists the site build is incomplete.'; host.removeAttribute('aria-busy'); });
    return host.__p;
  }
  function lazyInit(root) {
    var hosts = Array.prototype.slice.call(root.querySelectorAll('[data-lazy]'));
    if (!hosts.length) return;
    if (!('IntersectionObserver' in window)) { hosts.forEach(loadLazy); return; }
    var io = new IntersectionObserver(function (es) { es.forEach(function (e) { if (e.isIntersecting) { io.unobserve(e.target); loadLazy(e.target); } }); }, { rootMargin: '700px 0px' });
    hosts.forEach(function (h) { io.observe(h); });
  }
  function ensureLazy(el) {
    var hosts = Array.prototype.slice.call(el.querySelectorAll ? el.querySelectorAll('[data-lazy]') : []);
    if (el.hasAttribute && el.hasAttribute('data-lazy')) hosts.push(el);
    return Promise.all(hosts.map(loadLazy));
  }

  /* Fleet / Install are one page: the second part of the route scrolls to a section or opens a row (#/fleet/runs, #/fleet/a/<name>, #/install/<name>, #/install?open=<name>) */
  function focusSub(r) {
    if (scrollToAnchor()) return;
    var sub = r.sub, id = '';
    if (r.tab === 'fleet') {
      if (sub.indexOf('a/') === 0 && NAME_RE.test(sub.slice(2))) id = 'auto-' + sub.slice(2);
      else if (/^[a-z]{3,12}$/.test(sub)) id = 'sec-' + sub;
    } else if (r.tab === 'install') {
      var open = sub || new URLSearchParams(r.query).get('open') || '';
      if (NAME_RE.test(open)) id = 'a-' + open;
    }
    var el = id ? document.getElementById(id) : null;
    if (!el) { if (!sub) window.scrollTo(0, 0); return; }
    reveal(el);
    var target = el;                                              /* an automation's details row opens, but the page scrolls to the row that owns it */
    if (id.indexOf('auto-') === 0) { var owner = view.querySelector('tr[data-auto="' + id.slice(5) + '"]'); if (owner) target = owner; }
    ensureLazy(el).then(function () { if (current.tab === r.tab) { target.scrollIntoView(); try { target.focus({ preventScroll: true }); } catch (e) { /* not focusable */ } } });
  }

  function render() {
    var r = parse(), mine = ++seq;
    if ((!location.hash || location.hash === '#') || r.aliased) {      /* an empty hash becomes #/home, an old route its new address, without adding a history entry */
      try { history.replaceState(null, '', location.pathname + location.search + '#/' + r.path + (r.query ? '?' + r.query : '')); } catch (e) { /* file:// */ }
    }
    if (current.tab === r.tab && (SINGLE[r.tab] || current.sub === r.sub)) {   /* same view (a single-page tab never re-renders for its second part) */
      if (!silent) { paramHandlers.forEach(function (fn) { try { fn(AS.params()); } catch (e) { /* a view handler must not break routing */ } }); if (SINGLE[r.tab]) focusSub(r); else scrollToAnchor(); }
      return;
    }
    setActive(r.tab);
    document.title = (LABEL[r.tab] || 'AutoStation') + (r.sub && !SINGLE[r.tab] ? ' - ' + r.sub.split('/').pop() : '') + ' - AutoStation';
    view.setAttribute('aria-busy', 'true');
    load(r.tab).then(function (map) {
      if (mine !== seq) return;                                     /* a newer navigation superseded this one */
      var vkey = SINGLE[r.tab] ? '' : r.sub;
      var html = Object.prototype.hasOwnProperty.call(map, vkey) ? map[vkey] : null;
      paramHandlers = [];
      current = { tab: r.tab, sub: r.sub };
      if (typeof html !== 'string') {
        message('Not found', 'There is no "' + r.path + '" view. Pick a tab above.');
      } else {
        view.innerHTML = html;                                      /* build-time HTML: values were escaped by the generator */
        common(view);
        (hooks[r.tab] || []).concat(vkey ? (hooks[r.tab + '/' + vkey] || []) : (hooks[r.tab + '/'] || [])).forEach(function (fn) {
          try { fn(view); } catch (e) { if (window.console) console.error('view script failed', r.path, e); }
        });
        lazyInit(view);
      }
      view.removeAttribute('aria-busy');
      if (SINGLE[r.tab] && typeof html === 'string') { window.scrollTo(0, 0); focusSub(r); }
      else if (!scrollToAnchor()) { window.scrollTo(0, 0); }
      try { view.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
    }).catch(function () {
      if (mine !== seq) return;
      current = { tab: null, sub: null };
      message('Could not load this tab', location.protocol === 'file:' ? 'Open the site over http(s) (for example the GitHub Pages URL) - browsers block loading tab data from file://.' : 'The tab data could not be fetched. Reload the page; if it persists the site build is incomplete.');
      view.removeAttribute('aria-busy');
    });
  }

  /* behaviours every view can use: copy buttons, the ring selector on install pages */
  function common(root) {
    Array.prototype.forEach.call(root.querySelectorAll('[data-copy]'), function (btn) {
      btn.addEventListener('click', function () {
        var el = document.getElementById(btn.getAttribute('data-copy')); if (!el) return;
        var done = function () { var o = btn.textContent; btn.textContent = 'Copied'; setTimeout(function () { btn.textContent = o; }, 1200); };
        try { navigator.clipboard.writeText(el.textContent).then(done, function () {}); } catch (e) { /* clipboard unavailable */ }
      });
    });
    /* one ring selector per automation guide (several guides share the Install page): each one drives only the guide it sits in */
    Array.prototype.forEach.call(root.querySelectorAll('#ring-select'), function (sel) {
      var scope = sel.closest('details') || root;
      var apply = function () {
        Array.prototype.forEach.call(scope.querySelectorAll('[data-ring-text]'), function (el) {
          el.textContent = el.getAttribute('data-ring-text').split('{ring}').join(sel.value);
        });
        Array.prototype.forEach.call(scope.querySelectorAll('[data-ring-ver]'), function (el) {
          var v = el.getAttribute('data-ring-ver-' + sel.value); el.textContent = v || 'not released to this ring';
        });
      };
      sel.addEventListener('change', apply); apply();
    });
  }

  /* ---- tab bar: arrow keys / Home / End move between tabs (automatic activation), as the ARIA tabs pattern asks ---- */
  var bar = document.getElementById('tabs');
  bar.addEventListener('keydown', function (e) {
    var i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    var n = null;
    if (e.key === 'ArrowRight') n = (i + 1) % tabs.length; else if (e.key === 'ArrowLeft') n = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') n = 0; else if (e.key === 'End') n = tabs.length - 1;
    if (n === null) return;
    e.preventDefault(); tabs[n].focus(); location.hash = tabs[n].getAttribute('href');
  });

  /* ---- skip link, theme toggle ---- */
  var skip = document.querySelector('a[data-skip]');
  if (skip) skip.addEventListener('click', function (e) { e.preventDefault(); view.focus(); });
  var tt = document.getElementById('theme-toggle');
  if (tt) tt.addEventListener('click', function () {
    var cur = document.documentElement.getAttribute('data-theme');
    if (!cur) cur = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    var next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('autostation-theme', next); } catch (e) { /* private mode */ }
  });

  /* ---- global search over search-ops.json (automations, machines, runs, errors, docs sections, pages). Results are built with
          createElement/textContent only, and a target must be a safe in-app route. ---- */
  (function () {
    var input = document.getElementById('gsearch'), list = document.getElementById('gs-results');
    if (!input || !list) return;
    var SAFE = /^\/[A-Za-z0-9_\-\/.=&%?~]+$/;
    var index = null, loading = null, active = -1, shown = [];
    function loadIdx() {
      if (index) return Promise.resolve(index);
      if (!loading) loading = fetch(AS.url('search-ops.json'), { credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
        .then(function (j) { index = Array.isArray(j) ? j.slice(0, 2000) : []; return index; })
        .catch(function () { index = []; return index; });
      return loading;
    }
    function hide() { list.hidden = true; active = -1; }
    function renderHits(items) {
      while (list.firstChild) list.removeChild(list.firstChild);
      shown = [];
      items.forEach(function (it) {
        var u = it.u;
        if (typeof u !== 'string' || u.length > 300 || !SAFE.test(u) || u.indexOf('..') >= 0) return;
        var li = document.createElement('li'), a = document.createElement('a');
        a.href = '#' + u;
        [['gs-t', it.t, 20], ['gs-n', it.n, 120], ['gs-s', it.s, 120]].forEach(function (f) {
          var s = document.createElement('span'); s.className = f[0]; s.textContent = String(f[1] || '').slice(0, f[2]); a.appendChild(s);
        });
        a.addEventListener('click', function () { hide(); input.value = ''; });
        li.appendChild(a); list.appendChild(li); shown.push(a);
      });
      if (!shown.length) { var e = document.createElement('li'); e.className = 'gs-empty'; e.textContent = 'No matches'; list.appendChild(e); }
      list.hidden = false; active = -1;
    }
    function run() {
      var q = input.value.trim().toLowerCase().slice(0, 80);
      if (q.length < 2) { hide(); return; }
      loadIdx().then(function (idx) {
        var words = q.split(/\s+/);
        renderHits(idx.filter(function (it) {
          var hay = (String(it.n || '') + ' ' + String(it.s || '') + ' ' + String(it.t || '')).toLowerCase();
          return words.every(function (w) { return hay.indexOf(w) >= 0; });
        }).slice(0, 14));
      });
    }
    input.addEventListener('input', run);
    input.addEventListener('focus', function () { if (input.value.trim().length >= 2) run(); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { hide(); return; }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (!shown.length) return; e.preventDefault();
        active = (active + (e.key === 'ArrowDown' ? 1 : -1) + shown.length) % shown.length; shown[active].focus();
      } else if (e.key === 'Enter' && shown.length) { e.preventDefault(); shown[active >= 0 ? active : 0].click(); location.hash = shown[active >= 0 ? active : 0].getAttribute('href'); }
    });
    list.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { hide(); input.focus(); return; }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); active = (active + (e.key === 'ArrowDown' ? 1 : -1) + shown.length) % shown.length; shown[active].focus(); }
    });
    document.addEventListener('click', function (e) { if (!list.contains(e.target) && e.target !== input) hide(); });
    document.getElementById('gs-form').addEventListener('submit', function (e) { e.preventDefault(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || '')) { e.preventDefault(); input.focus(); }
    });
  })();

  window.addEventListener('hashchange', render);
  /* view scripts (jobs.js, views.js) load after this file and register their hooks first; DOMContentLoaded fires after all of them ran */
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', render); else render();
})();
