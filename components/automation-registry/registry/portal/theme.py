"""Shared CSS/JS for every portal page. Teal dark-first design; existing class names preserved."""
from __future__ import annotations

CSS = r"""
:root {
  --bg:#0b0d16; --sf:#131620; --sf2:#1a1d2b; --bd:#252838; --bd-hi:#353a52;
  --fg:#dde0f0; --fm:#848aa8; --fd:#4c5068;
  --ac:#00c8b8; --ac-lo:rgba(0,200,184,.13); --ac-t:#2dddd0;
  --ok:#22c55e; --ok-lo:rgba(34,197,94,.13);
  --warn:#f59e0b; --warn-lo:rgba(245,158,11,.13);
  --crit:#ef4444; --crit-lo:rgba(239,68,68,.13);
  --mute:#64748b; --mute-lo:rgba(100,116,139,.13);
  --info:#818cf8; --info-lo:rgba(129,140,248,.13);
  --r-dev:#818cf8; --r-pilot:#f59e0b; --r-broad:#22c55e;
  /* legacy aliases kept for backward compat */
  --surface:var(--sf); --surface2:var(--sf2); --border:var(--bd);
  --accent:var(--ac); --accent2:#7c5cfc; --accent-dim:var(--ac-lo);
  --fg-muted:var(--fm); --fg-dim:var(--fd);
  --green:var(--ok); --green-bg:var(--ok-lo);
  --yellow:var(--warn); --yellow-bg:var(--warn-lo);
  --red:var(--crit); --red-bg:var(--crit-lo);
  --purple:var(--info); --purple-bg:var(--info-lo);
  --mono:'JetBrains Mono','IBM Plex Mono',ui-monospace,SFMono-Regular,Consolas,'Liberation Mono',monospace;
  --sans:Inter,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  --nav-h:52px; --strip-h:38px;
  color-scheme:dark;
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --bg:#f0f1f8; --sf:#fff; --sf2:#e8eaf3; --bd:#d4d7ea; --bd-hi:#bfc4d8;
    --fg:#181b2c; --fm:#50567a; --fd:#8890b0;
    --ac:#0aaba0; --ac-lo:rgba(10,171,160,.1); --ac-t:#088a82;
    --ok:#16a34a; --ok-lo:rgba(22,163,74,.1);
    --warn:#d97706; --warn-lo:rgba(217,119,6,.1);
    --crit:#dc2626; --crit-lo:rgba(220,38,38,.1);
    --mute:#475569; --mute-lo:rgba(71,85,105,.1);
    --info:#6366f1; --info-lo:rgba(99,102,241,.1);
    --r-dev:#6366f1; --r-pilot:#d97706; --r-broad:#16a34a;
    color-scheme:light;
  }
}
:root[data-theme="light"] {
  --bg:#f0f1f8; --sf:#fff; --sf2:#e8eaf3; --bd:#d4d7ea; --bd-hi:#bfc4d8;
  --fg:#181b2c; --fm:#50567a; --fd:#8890b0;
  --ac:#0aaba0; --ac-lo:rgba(10,171,160,.1); --ac-t:#088a82;
  --ok:#16a34a; --ok-lo:rgba(22,163,74,.1);
  --warn:#d97706; --warn-lo:rgba(217,119,6,.1);
  --crit:#dc2626; --crit-lo:rgba(220,38,38,.1);
  --mute:#475569; --mute-lo:rgba(71,85,105,.1);
  --info:#6366f1; --info-lo:rgba(99,102,241,.1);
  --r-dev:#6366f1; --r-pilot:#d97706; --r-broad:#16a34a;
  color-scheme:light;
}
*,*::before,*::after { box-sizing:border-box; margin:0; padding:0; }
[hidden] { display:none !important; }
body { font-family:var(--sans); background:var(--bg); color:var(--fg); font-size:14px; line-height:1.5; -webkit-font-smoothing:antialiased; }
a { color:var(--ac-t); text-decoration:none; } a:hover { text-decoration:underline; }
h1 { font-size:1.2rem; font-weight:700; }
h2 { font-size:10px; font-weight:700; font-family:var(--mono); text-transform:uppercase; letter-spacing:.08em; color:var(--fd); margin:1.6rem 0 .6rem; padding-bottom:.4rem; display:flex; align-items:center; gap:8px; }
h2::after { content:''; flex:1; height:1px; background:var(--bd); }
h3 { font-size:.9rem; font-weight:600; margin:.9rem 0 .35rem; }
p { margin-bottom:.6rem; font-size:13px; } p:last-child { margin-bottom:0; }

/* ── SITE NAV ── */
.site-nav { position:fixed; top:0; left:0; right:0; height:var(--nav-h); background:var(--sf); border-bottom:1px solid var(--bd); display:flex; align-items:center; z-index:100; padding:0 max(16px,env(safe-area-inset-left,0px)) 0 max(16px,env(safe-area-inset-right,0px)); gap:0; }
.nav-brand { display:flex; align-items:center; gap:9px; text-decoration:none; margin-right:20px; flex-shrink:0; }
.nav-brand:hover { text-decoration:none; }
.nav-wm { font-size:13px; font-weight:700; letter-spacing:.08em; color:var(--fg); text-transform:uppercase; }
.nav-ver { font-family:var(--mono); font-size:9px; color:var(--fd); letter-spacing:.04em; margin-top:1px; }
.nav-tabs { display:flex; align-items:stretch; height:100%; flex:1; overflow-x:auto; scrollbar-width:none; }
.nav-tabs::-webkit-scrollbar { display:none; }
.nav-tab { display:flex; align-items:center; padding:0 13px; font-size:12px; font-weight:500; color:var(--fm); text-decoration:none; border-bottom:2px solid transparent; white-space:nowrap; flex-shrink:0; transition:color .12s,border-color .12s; }
.nav-tab:hover { color:var(--fg); text-decoration:none; }
.nav-tab.active { color:var(--ac-t); border-bottom-color:var(--ac); }
.nav-end { display:flex; align-items:center; gap:8px; margin-left:auto; flex-shrink:0; }
.nav-digest { font-family:var(--mono); font-size:9px; color:var(--fd); white-space:nowrap; }
@media (max-width:600px) { .nav-digest { display:none; } }

/* ── STATUS STRIP ── */
.status-strip { position:fixed; top:var(--nav-h); left:0; right:0; height:var(--strip-h); background:var(--sf2); border-bottom:1px solid var(--bd); display:flex; align-items:center; z-index:90; padding:0 max(16px,env(safe-area-inset-left,0px)) 0 max(16px,env(safe-area-inset-right,0px)); gap:0; overflow-x:auto; scrollbar-width:none; }
.status-strip::-webkit-scrollbar { display:none; }
.ss { display:flex; align-items:center; gap:5px; padding:0 14px; border-right:1px solid var(--bd); font-size:11px; color:var(--fd); white-space:nowrap; flex-shrink:0; }
.ss:first-child { padding-left:0; }
.ss-n { font-weight:700; font-size:14px; font-variant-numeric:tabular-nums; }
.ss-dot { width:6px; height:6px; border-radius:50%; flex-shrink:0; }
.ss-dot.ok { background:var(--ok); } .ss-dot.warn { background:var(--warn); } .ss-dot.crit { background:var(--crit); } .ss-dot.mute { background:var(--mute); }
.fleet-badge { margin-left:auto; padding:2px 9px; border-radius:20px; font-size:10px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; flex-shrink:0; }
.fleet-badge.ok { background:var(--ok-lo); color:var(--ok); } .fleet-badge.warn { background:var(--warn-lo); color:var(--warn); }

/* ── PAGE LAYOUT ── */
.page { max-width:1100px; margin:0 auto; padding:calc(var(--nav-h) + var(--strip-h) + 20px) max(16px,env(safe-area-inset-right,0px)) 40px max(16px,env(safe-area-inset-left,0px)); }
.view-head { display:flex; align-items:flex-start; justify-content:space-between; flex-wrap:wrap; gap:10px; padding:12px 0 10px; }
.view-title { font-size:15px; font-weight:600; }
.view-sub { font-size:11px; color:var(--fd); margin-top:2px; }

/* ── PRIMITIVES ── */
button, .btn { font:inherit; font-size:12px; cursor:pointer; border:1px solid var(--bd); background:none; color:var(--fm); padding:.3rem .75rem; border-radius:5px; display:inline-flex; align-items:center; gap:4px; text-decoration:none; transition:all .12s; }
button:hover, .btn:hover { border-color:var(--bd-hi); color:var(--fg); text-decoration:none; }
.btn.primary, button.primary { background:var(--ac-lo); border-color:var(--ac); color:var(--ac-t); font-weight:600; }
.btn.primary:hover, button.primary:hover { background:rgba(0,200,184,.22); }
.btn.danger, button.danger { background:var(--warn-lo); border-color:var(--warn); color:var(--warn); }
.btn.resume, button.resume { background:var(--ok-lo); border-color:var(--ok); color:var(--ok); }
.btn.crit, button.crit { background:var(--crit-lo); border-color:var(--crit); color:var(--crit); }
.btn.disabled, button:disabled { opacity:.4; cursor:not-allowed; pointer-events:none; }
input[type=text], input[type=search], select, textarea { font:inherit; font-size:12px; background:var(--sf); color:var(--fg); border:1px solid var(--bd); border-radius:6px; padding:.35rem .6rem; max-width:100%; }
textarea { width:100%; font-family:var(--mono); font-size:12px; }
input:focus, select:focus, textarea:focus, button:focus-visible, a:focus-visible { outline:2px solid var(--ac); outline-offset:1px; }

/* ── TAGS / PILLS (both naming conventions kept) ── */
.tag, .pill { display:inline-block; font-family:var(--mono); font-size:10px; font-weight:600; padding:1px 5px; border-radius:3px; white-space:nowrap; background:var(--sf2); color:var(--fm); letter-spacing:.04em; }
.tag.ok, .pill.ok { background:var(--ok-lo); color:var(--ok); }
.tag.warn, .pill.warn { background:var(--warn-lo); color:var(--warn); }
.tag.bad, .tag.crit, .pill.bad { background:var(--crit-lo); color:var(--crit); }
.tag.ai, .pill.info { background:var(--ac-lo); color:var(--ac-t); }
.tag.sys, .pill.sys { background:var(--sf2); color:var(--fm); border:1px solid var(--bd); }
.tag.mute, .pill.purple { background:var(--mute-lo); color:var(--mute); }

/* ── RING PROGRESS PIPS ── */
.ring-prog { display:flex; flex-direction:column; align-items:flex-end; gap:3px; flex-shrink:0; }
.ring-pips { display:flex; gap:3px; }
.rpip { width:18px; height:5px; border-radius:2px; background:var(--bd); }
.rpip.dev { background:var(--r-dev); } .rpip.pilot { background:var(--r-pilot); } .rpip.broad { background:var(--r-broad); }
.ring-labels { display:flex; gap:3px; }
.rl { font-size:8px; color:var(--fd); letter-spacing:.03em; width:18px; text-align:center; font-weight:500; }

/* ── CATALOG CARDS (new style) ── */
.card-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:10px; }
.acard { background:var(--sf); border:1px solid var(--bd); border-radius:10px; overflow:hidden; transition:border-color .12s,box-shadow .12s; cursor:pointer; }
.acard:hover { border-color:var(--bd-hi); box-shadow:0 3px 14px rgba(0,0,0,.2); }
.acard.paused { border-left:3px solid var(--warn); }
.acard-top { padding:12px 14px 9px; display:flex; align-items:flex-start; gap:8px; }
.hdot { width:7px; height:7px; border-radius:50%; flex-shrink:0; margin-top:5px; }
.hdot.ok { background:var(--ok); box-shadow:0 0 0 2px var(--ok-lo); } .hdot.warn { background:var(--warn); box-shadow:0 0 0 2px var(--warn-lo); }
.hdot.crit { background:var(--crit); box-shadow:0 0 0 2px var(--crit-lo); } .hdot.mute { background:var(--mute); }
.acard-title { flex:1; min-width:0; }
.acard-name { font-family:var(--mono); font-size:12.5px; font-weight:500; color:var(--fg); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.acard-tags { display:flex; gap:4px; margin-top:5px; flex-wrap:wrap; }
.acard-div { height:1px; background:var(--bd); }
.acard-meta { padding:8px 14px; display:grid; grid-template-columns:1fr 1fr; gap:4px 10px; }
.ml { font-size:9px; color:var(--fd); letter-spacing:.05em; text-transform:uppercase; font-weight:500; }
.mv { font-size:11.5px; color:var(--fg); margin-top:1px; }
.mv.ok { color:var(--ok); } .mv.warn { color:var(--warn); } .mv.mute { color:var(--mute); }
.acard-sha { padding:5px 14px; background:var(--sf2); display:flex; align-items:center; gap:6px; font-size:10px; color:var(--fd); }
.sha-val { font-family:var(--mono); color:var(--fm); }
.acard-foot { padding:8px 14px; display:flex; gap:6px; flex-wrap:wrap; }

/* ── LEGACY CARD (backward compat for detail pages) ── */
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:.85rem; }
.card { background:var(--sf); border:1px solid var(--bd); border-radius:8px; padding:1rem; display:flex; flex-direction:column; gap:.5rem; }
.card .name { font-family:var(--mono); font-weight:700; font-size:13px; word-break:break-all; }
.card .row { display:flex; justify-content:space-between; gap:.5rem; align-items:baseline; flex-wrap:wrap; }

/* ── HEALTH CARDS ── */
.hcard { background:var(--sf); border:1px solid var(--bd); border-radius:10px; padding:14px; border-left:3px solid var(--bd); margin-bottom:10px; }
.hcard.healthy { border-left-color:var(--ok); } .hcard.degraded { border-left-color:var(--warn); }
.hcard.failing { border-left-color:var(--crit); } .hcard.paused { border-left-color:var(--mute); }
.hcard-head { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; margin-bottom:8px; }
.hcard-name { font-family:var(--mono); font-size:12.5px; font-weight:500; }
.run-strip { display:flex; gap:2px; margin:5px 0; }
.rs-pip { width:11px; height:11px; border-radius:2px; background:var(--bd); flex-shrink:0; }
.rs-pip.success { background:var(--ok); } .rs-pip.partial { background:var(--warn); }
.rs-pip.failed,.rs-pip.hung { background:var(--crit); }

/* ── MACHINE CARDS ── */
.machine-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:10px; }
.mcard { background:var(--sf); border:1px solid var(--bd); border-radius:10px; padding:14px; transition:border-color .12s; }
.mcard:hover { border-color:var(--bd-hi); }
.mcard-head { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
.mname { font-family:var(--mono); font-size:12px; font-weight:500; color:var(--fg); flex:1; }
.mring { font-size:9px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; padding:2px 5px; border-radius:3px; }
.mring.broad { background:var(--ok-lo); color:var(--ok); } .mring.pilot { background:var(--warn-lo); color:var(--warn); } .mring.dev { background:var(--ac-lo); color:var(--ac-t); }
.mhb { font-size:11px; color:var(--fd); margin-bottom:7px; }
.mhb span { color:var(--fm); }
.mautos { display:flex; flex-wrap:wrap; gap:3px; }
.achip { font-size:9px; font-family:var(--mono); padding:2px 5px; border-radius:3px; background:var(--sf2); border:1px solid var(--bd); color:var(--fm); }
.msig { margin-top:8px; padding:5px 8px; border-radius:5px; font-size:11px; font-weight:500; display:flex; align-items:center; gap:5px; }
.msig.crit { background:var(--crit-lo); color:var(--crit); border:1px solid rgba(239,68,68,.3); }
.msig.warn { background:var(--warn-lo); color:var(--warn); border:1px solid rgba(245,158,11,.3); }
.msig.ok { background:var(--ok-lo); color:var(--ok); border:1px solid rgba(34,197,94,.3); }
.sig-dot { width:5px; height:5px; border-radius:50%; background:currentColor; flex-shrink:0; }

/* ── SHARED LAYOUT PIECES ── */
.summary { color:var(--fm); font-size:12.5px; }
.kv { display:grid; grid-template-columns:auto 1fr; gap:.12rem .65rem; font-size:12px; }
.kv dt { color:var(--fd); font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:.06em; padding-top:2px; }
.kv dd { color:var(--fg); word-break:break-word; }
.rings { display:flex; gap:.35rem; flex-wrap:wrap; }
.mono { font-family:var(--mono); font-size:11.5px; color:var(--fm); word-break:break-all; }
.dl { margin-top:auto; padding-top:.5rem; border-top:1px solid var(--bd); display:flex; flex-direction:column; gap:.3rem; }

/* ── KPI TILES ── */
.tiles { display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr)); gap:.7rem; margin-bottom:1rem; }
.tile { background:var(--sf); border:1px solid var(--bd); border-radius:8px; padding:.7rem .9rem; }
.tile .n, .tile-v { font-size:1.4rem; font-weight:700; line-height:1.1; font-variant-numeric:tabular-nums; }
.tile .l, .tile-k { font-family:var(--mono); font-size:9.5px; text-transform:uppercase; letter-spacing:.07em; color:var(--fd); }
.tile .s, .tile-s { font-size:11px; color:var(--fd); margin-top:2px; }
.tile .n.ok, .tile-v.ok { color:var(--ok); } .tile .n.warn, .tile-v.warn { color:var(--warn); }
.tile .n.crit, .tile-v.crit { color:var(--crit); } .tile .n.ac, .tile-v.ac { color:var(--ac-t); }

/* ── ALERTS / BANNERS ── */
.banner { border-radius:6px; padding:.7rem 1rem; font-size:13px; margin-bottom:1rem; border:1px solid var(--bd); background:var(--ac-lo); }
.banner.bad { background:var(--crit-lo); border-color:var(--crit); } .banner.warn { background:var(--warn-lo); border-color:var(--warn); }

/* ── TABLES ── */
table { width:100%; border-collapse:collapse; font-size:12px; }
th { text-align:left; padding:6px 10px; font-family:var(--mono); font-size:9.5px; letter-spacing:.07em; text-transform:uppercase; background:var(--sf2); border-bottom:1px solid var(--bd); color:var(--fd); white-space:nowrap; }
td { padding:7px 10px; border-bottom:1px solid var(--bd); vertical-align:top; }
tbody tr:last-child td { border-bottom:none; }
tbody tr:hover td { background:var(--sf2); }
.tbl-wrap { overflow-x:auto; border-radius:8px; border:1px solid var(--bd); margin-bottom:1rem; }

/* ── CODE ── */
code, pre { font-family:var(--mono); font-size:12px; }
code { background:var(--sf2); padding:1px 5px; border-radius:3px; color:var(--ac-t); }
pre { background:var(--sf2); border:1px solid var(--bd); border-radius:6px; padding:.8rem 1rem; overflow-x:auto; margin-bottom:1rem; line-height:1.55; white-space:pre-wrap; word-break:break-word; }
pre .add { color:var(--ok); } pre .del { color:var(--crit); } pre .hunk { color:var(--fd); }

/* ── ADMIN / PANEL / FORM ── */
.panel { background:var(--sf); border:1px solid var(--bd); border-radius:8px; padding:1rem; margin-bottom:1rem; }
.panel.accent { border-color:var(--ac); }
.field { display:flex; flex-direction:column; gap:.25rem; margin-bottom:.7rem; }
.field label { font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:var(--fd); }
.controls { display:flex; gap:.6rem; flex-wrap:wrap; align-items:center; margin-bottom:1rem; }
.controls input[type=search] { flex:1 1 220px; }
.chips { display:flex; gap:.35rem; flex-wrap:wrap; margin-bottom:1rem; }
.chip { font-family:var(--mono); font-size:11px; padding:2px 9px; border-radius:99px; border:1px solid var(--bd); background:var(--sf); color:var(--fm); cursor:pointer; }
.chip.on { border-color:var(--ac); color:var(--ac-t); background:var(--ac-lo); }
.actions { display:flex; gap:.35rem; flex-wrap:wrap; }
.eyebrow { font-family:var(--mono); font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--ac-t); }

/* ── SEARCH WITH ICON ── */
.search-wrap { position:relative; flex:1 1 220px; min-width:0; }
.search-ic { position:absolute; left:9px; top:50%; transform:translateY(-50%); color:var(--fd); pointer-events:none; }
.search-wrap input[type=search] { width:100%; padding-left:30px; }

/* ── PILL FILTER ROW (catalog) ── */
.filter-pills { display:flex; gap:.35rem; flex-wrap:wrap; margin-bottom:.75rem; }
.fpill { font-family:var(--mono); font-size:11px; padding:3px 10px; border-radius:99px; border:1px solid var(--bd); background:none; color:var(--fm); cursor:pointer; transition:all .12s; }
.fpill:hover { border-color:var(--bd-hi); color:var(--fg); }
.fpill.on { background:var(--ac-lo); border-color:var(--ac); color:var(--ac-t); }

/* ── SPARKLINES (health cards) ── */
.spark-row { display:flex; gap:14px; margin-top:8px; flex-wrap:wrap; }
.spark-item { display:flex; flex-direction:column; gap:2px; }
.spark-label { font-size:9px; color:var(--fd); text-transform:uppercase; letter-spacing:.05em; font-weight:600; }
.spark-svg { stroke:var(--ok); fill:none; stroke-width:1.5; overflow:visible; }
.spark-svg.bad { stroke:var(--crit); }
.spark-svg line { stroke:var(--bd); stroke-width:1; }

/* ── STATUS STRIP NUMBER COLORS ── */
.ss-n.ok { color:var(--ok); } .ss-n.warn { color:var(--warn); } .ss-n.crit { color:var(--crit); }

/* ── MARKDOWN CONTENT ── */
.md h1,.md h2,.md h3,.md h4 { text-transform:none; font-family:var(--sans); letter-spacing:0; color:var(--fg); border:0; margin:1.4rem 0 .5rem; }
.md h1 { font-size:1.3rem; } .md h2 { font-size:1.1rem; } .md h3 { font-size:1rem; } .md ul,.md ol { margin:0 0 .8rem 1.4rem; }
.md blockquote { border-left:3px solid var(--ac); padding:.2rem .9rem; color:var(--fm); margin-bottom:.8rem; }

/* ── FOOTER / MISC ── */
.empty { color:var(--fd); font-style:italic; padding:1rem 0; }
.sr { position:absolute; left:-9999px; }
footer.foot { margin-top:2.5rem; padding-top:1rem; border-top:1px solid var(--bd); font-size:11px; color:var(--fd); font-family:var(--mono); }
@media (max-width:640px) { .card-grid,.machine-grid { grid-template-columns:1fr; } .acard-meta { grid-template-columns:1fr; } .ring-prog { display:none; } }
"""

# Applies the saved theme before first paint (wrapped: storage may be blocked) and wires the toggle button.
THEME_HEAD_JS = r"""
try { var t = localStorage.getItem('autostation-theme'); if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t); } catch (e) {}
"""

THEME_TOGGLE_JS = r"""
(function () {
  var b = document.getElementById('theme-toggle'); if (!b) return;
  b.addEventListener('click', function () {
    var cur = document.documentElement.getAttribute('data-theme');
    if (!cur) cur = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    var next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('autostation-theme', next); } catch (e) {}
  });
})();
"""

LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 18" width="28" height="18" aria-hidden="true" style="color:var(--ac);flex-shrink:0">'
    '<rect x="1" y="6" width="22" height="7" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/>'
    '<rect x="16" y="2" width="7" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.5"/>'
    '<rect x="5" y="3" width="2.5" height="3" fill="none" stroke="currentColor" stroke-width="1.2"/>'
    '<circle cx="5.5" cy="14.5" r="2" fill="none" stroke="currentColor" stroke-width="1.4"/>'
    '<circle cx="12" cy="14.5" r="2" fill="none" stroke="currentColor" stroke-width="1.4"/>'
    '<circle cx="18.5" cy="14.5" r="2" fill="none" stroke="currentColor" stroke-width="1.4"/>'
    '<path d="M1 9.5L0 13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>'
    '</svg>')
