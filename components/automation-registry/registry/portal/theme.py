"""Shared CSS/JS for every portal page. Visual language follows the architecture assessment page."""
from __future__ import annotations

CSS = r"""
:root {
  --bg:#0f1117; --surface:#181c27; --surface2:#1e2335; --fg:#e2e4eb; --fg-muted:#9aa0b8; --fg-dim:#6a7090;
  --border:#252a3d; --accent:#4f8ef7; --accent2:#7c5cfc; --accent-dim:#1a2540;
  --green:#34d399; --green-bg:#0a2018; --yellow:#fbbf24; --yellow-bg:#1f1800; --red:#f87171; --red-bg:#200f0f;
  --purple:#c084fc; --purple-bg:#1a0f2a;
  --mono:'IBM Plex Mono',ui-monospace,SFMono-Regular,Consolas,'Liberation Mono',monospace;
  --sans:Inter,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif; color-scheme:dark;
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --bg:#f5f6fa; --surface:#ffffff; --surface2:#eef0f8; --fg:#111320; --fg-muted:#4a5070; --fg-dim:#7a83a3;
    --border:#dde0f0; --accent:#1f4dcc; --accent2:#6038d4; --accent-dim:#e8eeff; --green:#16a34a; --green-bg:#f0fdf4;
    --yellow:#b45309; --yellow-bg:#fffbeb; --red:#dc2626; --red-bg:#fef2f2; --purple:#7c3aed; --purple-bg:#f5f3ff; color-scheme:light;
  }
}
:root[data-theme="light"] {
  --bg:#f5f6fa; --surface:#ffffff; --surface2:#eef0f8; --fg:#111320; --fg-muted:#4a5070; --fg-dim:#7a83a3;
  --border:#dde0f0; --accent:#1f4dcc; --accent2:#6038d4; --accent-dim:#e8eeff; --green:#16a34a; --green-bg:#f0fdf4;
  --yellow:#b45309; --yellow-bg:#fffbeb; --red:#dc2626; --red-bg:#fef2f2; --purple:#7c3aed; --purple-bg:#f5f3ff; color-scheme:light;
}
* { box-sizing:border-box; margin:0; padding:0; }
[hidden] { display:none !important; }
body { font-family:var(--sans); background:var(--bg); color:var(--fg); font-size:14px; line-height:1.6; padding:1.5rem 1rem 3rem; }
.page { max-width:1100px; margin:0 auto; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
header.top { display:flex; align-items:center; gap:1rem; flex-wrap:wrap; margin-bottom:1.5rem; padding-bottom:1rem; border-bottom:1px solid var(--border); }
.brand { display:flex; align-items:center; gap:.7rem; }
.brand svg { flex-shrink:0; }
.eyebrow { font-family:var(--mono); font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--accent); }
h1 { font-size:1.35rem; font-weight:800; text-wrap:balance; }
h2 { font-size:.75rem; font-weight:700; font-family:var(--mono); text-transform:uppercase; letter-spacing:.1em; color:var(--accent); margin:2rem 0 .8rem; padding-bottom:.4rem; border-bottom:1px solid var(--border); }
h3 { font-size:.95rem; font-weight:600; margin:1.1rem 0 .4rem; }
p { margin-bottom:.7rem; font-size:13.5px; } p:last-child { margin-bottom:0; }
nav.main { display:flex; gap:2px; margin-left:auto; flex-wrap:wrap; align-items:center; }
nav.main a { padding:.35rem .8rem; font-size:12px; font-family:var(--mono); letter-spacing:.04em; color:var(--fg-muted); border-bottom:2px solid transparent; }
nav.main a:hover { color:var(--fg); text-decoration:none; }
nav.main a.active { color:var(--accent); border-color:var(--accent); }
button, .btn { font:inherit; font-size:12.5px; cursor:pointer; border:1px solid var(--border); background:var(--surface2); color:var(--fg); padding:.35rem .8rem; border-radius:6px; display:inline-block; text-decoration:none; }
button:hover, .btn:hover { border-color:var(--accent); text-decoration:none; }
.btn.primary, button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
.btn.danger, button.danger { background:var(--red-bg); border-color:var(--red); color:var(--red); }
.btn.disabled, button:disabled { opacity:.45; cursor:not-allowed; pointer-events:none; }
input[type=text], input[type=search], select, textarea { font:inherit; font-size:13px; background:var(--surface); color:var(--fg); border:1px solid var(--border); border-radius:6px; padding:.4rem .6rem; max-width:100%; }
textarea { width:100%; font-family:var(--mono); font-size:12px; }
input:focus, select:focus, textarea:focus, button:focus-visible, a:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
.controls { display:flex; gap:.6rem; flex-wrap:wrap; align-items:center; margin-bottom:1rem; }
.controls input[type=search] { flex:1 1 220px; }
.chips { display:flex; gap:.35rem; flex-wrap:wrap; margin-bottom:1rem; }
.chip { font-family:var(--mono); font-size:11px; padding:2px 9px; border-radius:99px; border:1px solid var(--border); background:var(--surface); color:var(--fg-muted); cursor:pointer; }
.chip.on { border-color:var(--accent); color:var(--accent); background:var(--accent-dim); }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:.85rem; }
.card { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:1rem; display:flex; flex-direction:column; gap:.55rem; }
.card.paused { border-left:3px solid var(--yellow); }
.card .name { font-family:var(--mono); font-weight:700; font-size:13px; word-break:break-all; }
.card .row { display:flex; justify-content:space-between; gap:.5rem; align-items:baseline; flex-wrap:wrap; }
.summary { color:var(--fg-muted); font-size:12.5px; }
.kv { display:grid; grid-template-columns:auto 1fr; gap:.15rem .7rem; font-size:12px; }
.kv dt { color:var(--fg-dim); font-family:var(--mono); font-size:10.5px; text-transform:uppercase; letter-spacing:.06em; padding-top:2px; }
.kv dd { color:var(--fg); word-break:break-word; }
.pill { display:inline-block; font-family:var(--mono); font-size:10px; font-weight:700; padding:2px 7px; border-radius:3px; white-space:nowrap; background:var(--surface2); color:var(--fg-muted); }
.pill.ok { background:var(--green-bg); color:var(--green); } .pill.warn { background:var(--yellow-bg); color:var(--yellow); }
.pill.bad { background:var(--red-bg); color:var(--red); } .pill.info { background:var(--accent-dim); color:var(--accent); }
.pill.purple { background:var(--purple-bg); color:var(--purple); }
.rings { display:flex; gap:.35rem; flex-wrap:wrap; }
.mono { font-family:var(--mono); font-size:11.5px; color:var(--fg-muted); word-break:break-all; }
.dl { margin-top:auto; padding-top:.5rem; border-top:1px solid var(--border); display:flex; flex-direction:column; gap:.3rem; }
.banner { border-radius:6px; padding:.75rem 1rem; font-size:13px; margin-bottom:1rem; border:1px solid var(--border); background:var(--accent-dim); }
.banner.bad { background:var(--red-bg); border-color:var(--red); } .banner.warn { background:var(--yellow-bg); border-color:var(--yellow); }
table { width:100%; border-collapse:collapse; font-size:13px; margin-bottom:1.2rem; }
th { text-align:left; padding:6px 10px; font-family:var(--mono); font-size:10px; letter-spacing:.07em; text-transform:uppercase; background:var(--surface2); border:1px solid var(--border); color:var(--fg-muted); }
td { padding:7px 10px; border:1px solid var(--border); vertical-align:top; background:var(--surface); }
.tbl-wrap { overflow-x:auto; }
code, pre { font-family:var(--mono); font-size:12px; }
code { background:var(--surface2); padding:1px 5px; border-radius:3px; color:var(--accent); }
pre { background:var(--surface2); border:1px solid var(--border); border-radius:6px; padding:.8rem 1rem; overflow-x:auto; margin-bottom:1rem; line-height:1.55; white-space:pre-wrap; word-break:break-word; }
pre .add { color:var(--green); } pre .del { color:var(--red); } pre .hunk { color:var(--fg-dim); }
.tiles { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:.7rem; margin-bottom:1rem; }
.tile { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:.7rem .9rem; }
.tile .n { font-size:1.5rem; font-weight:800; line-height:1.1; } .tile .l { font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:.07em; color:var(--fg-dim); }
.panel { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:1rem; margin-bottom:1rem; }
.panel.accent { border-color:var(--accent); }
.field { display:flex; flex-direction:column; gap:.25rem; margin-bottom:.7rem; }
.field label { font-family:var(--mono); font-size:10.5px; text-transform:uppercase; letter-spacing:.06em; color:var(--fg-dim); }
.actions { display:flex; gap:.35rem; flex-wrap:wrap; }
footer.foot { margin-top:3rem; padding-top:1rem; border-top:1px solid var(--border); font-size:11.5px; color:var(--fg-dim); font-family:var(--mono); }
.md h1,.md h2,.md h3,.md h4 { text-transform:none; font-family:var(--sans); letter-spacing:0; color:var(--fg); border:0; margin:1.4rem 0 .5rem; }
.md h1 { font-size:1.3rem; } .md h2 { font-size:1.1rem; } .md h3 { font-size:1rem; } .md ul,.md ol { margin:0 0 .8rem 1.4rem; }
.md blockquote { border-left:3px solid var(--accent); padding:.2rem .9rem; color:var(--fg-muted); margin-bottom:.8rem; }
.empty { color:var(--fg-dim); font-style:italic; padding:1rem 0; }
.sr { position:absolute; left:-9999px; }
@media (max-width:640px) { body { padding:1rem .75rem 2.5rem; } nav.main { margin-left:0; } h1 { font-size:1.15rem; } .grid { grid-template-columns:1fr; } }
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
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" width="40" height="40" aria-hidden="true">'
    '<rect x="8" y="150" width="184" height="7" rx="3.5" fill="#3b82f6" opacity="0.9"/>'
    '<rect x="8" y="164" width="184" height="7" rx="3.5" fill="#3b82f6" opacity="0.9"/>'
    '<rect x="14" y="100" width="136" height="52" rx="8" fill="#991b1b"/><rect x="14" y="100" width="136" height="9" rx="5" fill="#fbbf24"/>'
    '<path d="M 148 100 L 182 100 Q 190 100 190 114 L 190 148 L 148 148 Z" fill="#7f1d1d"/>'
    '<circle cx="42" cy="150" r="18" fill="#0d0a0a" stroke="#fbbf24" stroke-width="3"/>'
    '<circle cx="92" cy="150" r="18" fill="#0d0a0a" stroke="#fbbf24" stroke-width="3"/>'
    '<circle cx="154" cy="150" r="18" fill="#0d0a0a" stroke="#fbbf24" stroke-width="3"/>'
    '<rect x="102" y="12" width="84" height="60" rx="8" fill="#2a1010"/><rect x="108" y="18" width="72" height="48" rx="4" fill="#0d0a0a"/>'
    '<rect x="114" y="24" width="52" height="5" rx="2" fill="#3b82f6"/><rect x="114" y="34" width="38" height="4" rx="2" fill="#3b82f6" opacity="0.5"/>'
    '</svg>')
