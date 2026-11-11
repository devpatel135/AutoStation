"""Static metrics dashboard for GitLab Pages: ``index.html`` + ``data.json`` + ``signals.json`` + ``runs-latest.json``.

Report data is injected as a JSON blob; all rendering happens client-side. Every log-derived
string that reaches Python code (error messages, synopses, host names...) passes through ``esc()``;
the JS side uses its own ``e()`` escaper for client-rendered content.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from pathlib import Path

from .common import write_json
from . import analytics


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def fmt_ms(ms) -> str:
    if ms is None:
        return "-"
    s = ms / 1000
    if s < 1:
        return f"{int(ms)} ms"
    if s < 60:
        return f"{s:.1f} s"
    return f"{int(s // 60)}m {int(s % 60):02d}s"


def pct(x) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


CSS = """\
/* Layout: fixed nav → status strip → scrollable content */
:root{
  --bg:#0b0d16;--sf:#131620;--sf2:#1a1d2b;--bd:#252838;--bd-hi:#353a52;
  --fg:#dde0f0;--fg2:#848aa8;--fg3:#4c5068;
  --ac:#00c8b8;--ac-lo:rgba(0,200,184,.13);--ac-txt:#2dddd0;
  --ok:#22c55e;--ok-lo:rgba(34,197,94,.13);
  --warn:#f59e0b;--warn-lo:rgba(245,158,11,.13);
  --crit:#ef4444;--crit-lo:rgba(239,68,68,.13);
  --mute:#64748b;--mute-lo:rgba(100,116,139,.13);
  --mono:ui-monospace,SFMono-Regular,Consolas,Menlo,monospace;
  --sans:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  --nh:52px;--sh:38px;
  color-scheme:dark;
}
@media(prefers-color-scheme:light){
  :root:not([data-theme="dark"]){
    --bg:#f0f1f8;--sf:#fff;--sf2:#e8eaf3;--bd:#d4d7ea;--bd-hi:#bfc4d8;
    --fg:#181b2c;--fg2:#50567a;--fg3:#8890b0;
    --ac:#0aaba0;--ac-lo:rgba(10,171,160,.1);--ac-txt:#088a82;
    --ok:#16a34a;--ok-lo:rgba(22,163,74,.1);
    --warn:#d97706;--warn-lo:rgba(217,119,6,.1);
    --crit:#dc2626;--crit-lo:rgba(220,38,38,.1);
    --mute:#475569;--mute-lo:rgba(71,85,105,.1);
    color-scheme:light;
  }
}
:root[data-theme="light"]{
  --bg:#f0f1f8;--sf:#fff;--sf2:#e8eaf3;--bd:#d4d7ea;--bd-hi:#bfc4d8;
  --fg:#181b2c;--fg2:#50567a;--fg3:#8890b0;
  --ac:#0aaba0;--ac-lo:rgba(10,171,160,.1);--ac-txt:#088a82;
  --ok:#16a34a;--ok-lo:rgba(22,163,74,.1);
  --warn:#d97706;--warn-lo:rgba(217,119,6,.1);
  --crit:#dc2626;--crit-lo:rgba(220,38,38,.1);
  --mute:#475569;--mute-lo:rgba(71,85,105,.1);
  color-scheme:light;
}
:root[data-theme="dark"]{color-scheme:dark;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;}
body{background:var(--bg);color:var(--fg);font-family:var(--sans);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased;padding:0;}
nav{position:fixed;top:env(safe-area-inset-top,0px);left:0;right:0;height:var(--nh);background:var(--sf);border-bottom:1px solid var(--bd);display:flex;align-items:center;z-index:100;padding-inline:max(20px,env(safe-area-inset-left,0px)) max(20px,env(safe-area-inset-right,0px));}
.nav-brand{display:flex;align-items:center;gap:9px;flex-shrink:0;margin-right:20px;}
.nav-logo{color:var(--ac);flex-shrink:0;}
.nav-wm{font-size:14px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--fg);}
.nav-ver{font-family:var(--mono);font-size:9px;color:var(--fg3);letter-spacing:.05em;margin-top:1px;}
.nav-tabs{display:flex;align-items:stretch;height:100%;overflow-x:auto;scrollbar-width:none;flex:1;}
.nav-tabs::-webkit-scrollbar{display:none;}
.tab-btn{display:flex;align-items:center;padding:0 14px;font-size:12px;font-weight:500;color:var(--fg2);background:none;border:none;border-bottom:2px solid transparent;cursor:pointer;white-space:nowrap;flex-shrink:0;font-family:inherit;transition:color .12s,border-color .12s;}
.tab-btn:hover{color:var(--fg);}
.tab-btn.active{color:var(--ac-txt);border-bottom-color:var(--ac);}
.tab-btn .ct{font-family:var(--mono);font-size:10px;padding:0 5px;background:var(--sf2);color:var(--fg3);border-radius:3px;margin-left:5px;font-variant-numeric:tabular-nums;}
.tab-btn.active .ct{background:var(--ac-lo);color:var(--ac-txt);}
.nav-end{display:flex;align-items:center;gap:8px;margin-left:auto;flex-shrink:0;}
.btn-sm{background:none;border:1px solid var(--bd);border-radius:6px;padding:4px 8px;color:var(--fg2);cursor:pointer;font-size:11px;font-family:inherit;transition:all .12s;display:flex;align-items:center;gap:4px;}
.btn-sm:hover{border-color:var(--bd-hi);color:var(--fg);}
.sstrip{position:fixed;top:calc(env(safe-area-inset-top,0px) + var(--nh));left:0;right:0;height:var(--sh);background:var(--sf2);border-bottom:1px solid var(--bd);display:flex;align-items:center;z-index:90;padding-inline:max(20px,env(safe-area-inset-left,0px)) max(20px,env(safe-area-inset-right,0px));gap:0;overflow-x:auto;scrollbar-width:none;}
.sstrip::-webkit-scrollbar{display:none;}
.ss{display:flex;align-items:center;gap:5px;padding:0 14px;border-right:1px solid var(--bd);font-size:11px;color:var(--fg3);white-space:nowrap;flex-shrink:0;}
.ss:first-child{padding-left:0;}
.ss-n{font-weight:700;font-size:14px;font-variant-numeric:tabular-nums;}
.ss-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0;}
.ss-dot.ok{background:var(--ok);}.ss-dot.warn{background:var(--warn);}.ss-dot.mute{background:var(--mute);}
.fleet-badge{margin-left:auto;padding:2px 9px;border-radius:20px;font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;flex-shrink:0;}
.fleet-badge.ok{background:var(--ok-lo);color:var(--ok);}
.fleet-badge.warn{background:var(--warn-lo);color:var(--warn);}
.fleet-badge.crit{background:var(--crit-lo);color:var(--crit);}
.main{padding-top:calc(env(safe-area-inset-top,0px) + var(--nh) + var(--sh));padding-bottom:48px;padding-left:max(20px,env(safe-area-inset-left,0px));padding-right:max(20px,env(safe-area-inset-right,0px));max-width:1240px;margin:0 auto;}
.panel{display:none;}.panel.active{display:block;}
.sh{font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--fg3);padding:14px 0 6px;display:flex;align-items:center;gap:8px;}
.sh::after{content:'';flex:1;height:1px;background:var(--bd);}
.sh:first-child{padding-top:0;}
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;padding-bottom:4px;}
.kpi{background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:12px 14px;}
.kpi-k{font-size:10px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--fg3);margin-bottom:4px;}
.kpi-v{font-size:22px;font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums;}
.kpi-s{font-size:11px;color:var(--fg3);margin-top:3px;}
.kpi-v.ok{color:var(--ok);}.kpi-v.warn{color:var(--warn);}.kpi-v.crit{color:var(--crit);}.kpi-v.ac{color:var(--ac-txt);}
.tag{font-size:10px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;padding:1px 5px;border-radius:3px;line-height:1.5;white-space:nowrap;vertical-align:middle;}
.tag.ok{background:var(--ok-lo);color:var(--ok);}
.tag.warn{background:var(--warn-lo);color:var(--warn);}
.tag.crit{background:var(--crit-lo);color:var(--crit);}
.tag.mute{background:var(--mute-lo);color:var(--mute);}
.tag.ac{background:var(--ac-lo);color:var(--ac-txt);}
.tag.sys{background:var(--sf2);color:var(--fg3);border:1px solid var(--bd);}
.tbl-wrap{overflow-x:auto;border-radius:8px;border:1px solid var(--bd);}
table{width:100%;border-collapse:collapse;font-size:12px;}
thead th{background:var(--sf2);padding:7px 12px;text-align:left;font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--fg3);border-bottom:1px solid var(--bd);white-space:nowrap;cursor:pointer;user-select:none;}
thead th:hover{color:var(--fg2);}
thead th.s-asc::after{content:' ↑';color:var(--ac-txt);}
thead th.s-desc::after{content:' ↓';color:var(--ac-txt);}
tbody td{padding:8px 12px;border-bottom:1px solid var(--bd);color:var(--fg);vertical-align:top;overflow-wrap:anywhere;min-width:0;}
tbody tr:last-child td{border-bottom:none;}
tbody tr:hover td{background:var(--sf2);}
tr.xrow{cursor:pointer;}
tr.xbody td{background:var(--sf2)!important;border-top:none;padding:.7rem 1rem;border-bottom:none;}
tbody tr.xbody:last-child td{border-bottom:none;}
code{font-family:var(--mono);font-size:11px;background:var(--sf2);padding:1px 5px;border-radius:3px;}
.diag-card{background:var(--sf);border:1px solid var(--bd);border-radius:6px;padding:.65rem .85rem;font-size:12px;}
.diag-head{font-weight:600;margin-bottom:.3rem;color:var(--fg);}
.diag-note{font-size:11px;color:var(--fg3);font-style:italic;margin-top:.3rem;}
.chips{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:12px;}
.chip{font-size:11px;font-weight:500;padding:3px 10px;border-radius:20px;border:1px solid var(--bd);background:none;color:var(--fg2);cursor:pointer;transition:all .12s;font-family:inherit;}
.chip:hover{border-color:var(--bd-hi);color:var(--fg);}
.chip.on{background:var(--ac-lo);border-color:var(--ac);color:var(--ac-txt);}
.agrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;}
.acard{background:var(--sf);border:1px solid var(--bd);border-left:3px solid var(--bd);border-radius:10px;overflow:hidden;transition:border-color .12s,box-shadow .12s;}
.acard:hover{border-color:var(--bd-hi);box-shadow:0 3px 14px rgba(0,0,0,.25);}
.acard.healthy{border-left-color:var(--ok);}
.acard.degraded{border-left-color:var(--warn);}
.acard.failing{border-left-color:var(--crit);}
.acard.paused{border-left-color:var(--mute);}
.acard-top{padding:12px 14px 10px;display:flex;align-items:flex-start;gap:9px;}
.hdot{width:7px;height:7px;border-radius:50%;flex-shrink:0;margin-top:5px;}
.hdot.ok{background:var(--ok);box-shadow:0 0 0 2px var(--ok-lo);}
.hdot.warn{background:var(--warn);box-shadow:0 0 0 2px var(--warn-lo);}
.hdot.crit{background:var(--crit);box-shadow:0 0 0 2px var(--crit-lo);}
.hdot.mute{background:var(--mute);}
.acard-title{flex:1;min-width:0;}
.aname{font-family:var(--mono);font-size:12.5px;font-weight:500;color:var(--fg);word-break:break-all;}
.atags{display:flex;gap:4px;margin-top:5px;flex-wrap:wrap;}
.acard-div{height:1px;background:var(--bd);}
.acard-body{padding:10px 14px;}
.strip{display:flex;gap:2px;align-items:center;margin:.45rem 0;}
.pip{width:11px;height:11px;border-radius:2px;background:var(--bd);flex-shrink:0;}
.pip.success{background:var(--ok);opacity:.85;}
.pip.partial{background:var(--warn);opacity:.85;}
.pip.failed,.pip.hung{background:var(--crit);opacity:.85;}
.pip.paused{background:var(--mute);opacity:.85;}
.strip-lbl{font-size:10px;font-weight:500;color:var(--fg3);margin-left:6px;letter-spacing:.03em;}
.syn{font-size:12px;color:var(--fg2);margin:.45rem 0;line-height:1.5;}
.kvg{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:12px;margin-top:.5rem;}
.kvg dt{color:var(--fg3);font-weight:500;}
.kvg dd{color:var(--fg);}
.advisory{background:var(--sf);border:1px dashed var(--bd);border-radius:8px;padding:.65rem 1rem;font-size:12px;color:var(--fg3);margin-bottom:16px;}
.advisory b{color:var(--fg2);}
.empty{padding:40px 20px;text-align:center;color:var(--fg3);font-size:13px;border:1px dashed var(--bd);border-radius:10px;}
@media(max-width:640px){.kpi-v{font-size:18px;}.agrid{grid-template-columns:1fr;}}
"""

# No web fonts: the site loads nothing from outside its own origin (privacy + the strict CSP below). System font stacks are used.
_FONT_LINK = ""

_NAV_SVG = """\
<nav role="navigation">
  <div class="nav-brand">
    <svg class="nav-logo" viewBox="0 0 52 32" width="40" height="24" aria-hidden="true" style="overflow:visible">
      <line x1="0" y1="29" x2="52" y2="29" stroke="#00c8b8" stroke-width="1.5"/>
      <rect x="3" y="10" width="35" height="13" rx="1.5" fill="none" stroke="#991b1b" stroke-width="1.5"/>
      <rect x="3" y="17" width="35" height="3" fill="#fbbf24" opacity=".6"/>
      <rect x="30" y="5" width="8" height="18" rx="1" fill="none" stroke="#7f1d1d" stroke-width="1.2"/>
      <rect x="32" y="7" width="4" height="5" rx=".5" fill="#bfdbfe" opacity=".5"/>
      <circle cx="3.5" cy="14" r="2.5" fill="#34d399"/>
      <circle cx="9" cy="29" r="3.5" fill="none" stroke="#00c8b8" stroke-width="1.5"/>
      <circle cx="9" cy="29" r="1.3" fill="#00c8b8"/>
      <circle cx="21" cy="29" r="5" fill="none" stroke="#00c8b8" stroke-width="1.5"/>
      <circle cx="21" cy="29" r="1.8" fill="#00c8b8"/>
      <circle cx="33" cy="29" r="5" fill="none" stroke="#00c8b8" stroke-width="1.5"/>
      <circle cx="33" cy="29" r="1.8" fill="#00c8b8"/>
    </svg>
    <div>
      <div class="nav-wm">AUTOSTATION</div>
      <div class="nav-ver">FLEET DASHBOARD</div>
    </div>
  </div>
  <div class="nav-tabs" role="tablist" aria-label="Dashboard sections">
    <button class="tab-btn active" role="tab" data-tab="overview" aria-selected="true">Overview</button>
    <button class="tab-btn" role="tab" data-tab="automations" aria-selected="false">Automations <span class="ct" id="ct-auto"></span></button>
    <button class="tab-btn" role="tab" data-tab="signals" aria-selected="false">Signals <span class="ct" id="ct-sig"></span></button>
    <button class="tab-btn" role="tab" data-tab="telemetry" aria-selected="false">Telemetry</button>
    <button class="tab-btn" role="tab" data-tab="infra" aria-selected="false">Infrastructure</button>
    <button class="tab-btn" role="tab" data-tab="evidence" aria-selected="false">Evidence</button>
  </div>
  <div class="nav-end">
    <span id="gentime" style="font-family:var(--mono);font-size:10px;color:var(--fg3)"></span>
    <button class="btn-sm" id="theme-toggle" type="button">Theme</button>
  </div>
</nav>
<div class="sstrip" id="sstrip" aria-hidden="true"></div>"""

TELEMETRY_NOTICE = "Telemetry is submitted by installed clients and is not authenticated; treat numbers as advisory"

THEME_JS = """\
document.getElementById('theme-toggle').addEventListener('click',()=>{
  const r=document.documentElement;
  const dark=matchMedia('(prefers-color-scheme:dark)').matches;
  const cur=r.dataset.theme;
  const next=cur==='light'?'dark':cur==='dark'?undefined:dark?'light':'dark';
  if(next){r.dataset.theme=next;try{localStorage.setItem('amx-theme',next);}catch{}}
  else{delete r.dataset.theme;try{localStorage.removeItem('amx-theme');}catch{}}
});"""

def _csp_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'"

_RENDER_JS = """\
const D = JSON.parse(document.getElementById('__dash_data__').textContent);
const e = s => String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const pct = x => x==null?'—':(x*100).toFixed(1)+'%';
const fms = ms => {
  if(ms==null) return '—';
  const s=ms/1000;
  if(s<60) return s.toFixed(1)+'s';
  return Math.floor(s/60)+'m '+String(Math.round(s%60)).padStart(2,'0')+'s';
};
const TAG_CLS={ok:'ok',healthy:'ok',success:'ok',critical:'crit',failed:'crit',failing:'crit',partial:'warn',degraded:'warn',medium:'warn',high:'warn',low:'mute',paused:'mute',unknown:'mute'};
const pill=(t,c)=>`<span class="tag ${TAG_CLS[c]||'sys'}">${e(t)}</span>`;
const dt = s => s?s.replace('T',' ').replace('Z',''):'—';

const SEV={critical:'critical',high:'high',medium:'medium',low:'low'};
const STA={success:'ok',partial:'partial',failed:'failed',hung:'failed',paused:'paused',unknown:'unknown'};
const HDOT={healthy:'ok',degraded:'warn',failing:'crit',paused:'mute'};

function spark(vals,w=110,h=26,color='var(--ok)'){
  const v=vals.map(x=>Math.max(0,+x||0));
  const mx=Math.max(...v,0.001);
  const pd=2,step=v.length>1?(w-pd*2)/(v.length-1):0;
  const pts=v.map((x,i)=>`${pd+i*step},${h-pd-(x/mx)*(h-pd*2)}`).join(' ');
  const lx=pd+(v.length-1)*step,ly=h-pd-(v[v.length-1]/mx)*(h-pd*2);
  if(mx===0.001) return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><line x1="0" y1="${h-2}" x2="${w}" y2="${h-2}" stroke="var(--bd)" stroke-width="1"/><\\/svg>`;
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true" style="overflow:visible"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round"/><circle cx="${lx}" cy="${ly}" r="2.5" fill="${color}"/><\\/svg>`;
}

function renderStrip(){
  const f=D.fleet,imp=D.impact;
  const healthy=Object.values(D.automations).filter(a=>a.health==='healthy').length;
  const bad=Object.values(D.automations).filter(a=>a.health!=='healthy'&&a.health!=='paused').length;
  const flCls=f.status==='healthy'?'ok':f.status==='degraded'?'warn':'crit';
  document.getElementById('sstrip').innerHTML=
    `<div class="ss"><div class="ss-dot ok"><\\/div><span class="ss-n">${f.automations}<\\/span>&nbsp;automations<\\/div>`+
    `<div class="ss"><div class="ss-dot ok"><\\/div><span class="ss-n">${healthy}<\\/span>&nbsp;healthy<\\/div>`+
    `<div class="ss"><div class="ss-dot warn"><\\/div><span class="ss-n">${bad}<\\/span>&nbsp;degraded<\\/div>`+
    `<div class="ss"><div class="ss-dot mute"><\\/div><span class="ss-n">${f.hosts}<\\/span>&nbsp;host${f.hosts!==1?'s':''}<\\/div>`+
    `<div class="ss"><span class="ss-n">${imp.items_30d.toLocaleString()}<\\/span>&nbsp;items (30d)<\\/div>`+
    `<span class="fleet-badge ${flCls}">${f.status}<\\/span>`;
  document.getElementById('gentime').textContent='Generated '+dt(D.generated_at)+' UTC · '+D.window_days+'d window';
}

function renderOverview(){
  const f=D.fleet,imp=D.impact;
  document.getElementById('kpi-row').innerHTML=[
    {k:'Automations',   v:f.automations,     s:f.hosts+' host(s)'},
    {k:'Total runs',    v:f.total,            s:f.success+' ok · '+f.partial+' partial · '+(f.failed+f.hung)+' failed'},
    {k:'Success rate',  v:pct(f.success_rate),s:'excludes paused / maintenance skips'},
    {k:'Active signals',v:f.signals_total,    s:(f.signals.critical||0)+' critical · '+(f.signals.high||0)+' high · '+(f.signals.medium||0)+' medium'},
    {k:'Items (30d)',   v:imp.items_30d.toLocaleString(),s:imp.items_7d+' in last 7 days'},
    {k:'Est. hrs saved',v:imp.hours_saved_30d,s:'last 30 days (catalog estimate)'}
  ].map(t=>`<div class="kpi"><div class="kpi-k">${e(t.k)}<\\/div><div class="kpi-v">${e(String(t.v??''))}<\\/div><div class="kpi-s">${e(t.s)}<\\/div><\\/div>`).join('');

  const hiSigs=D.signals.filter(s=>s.severity==='critical'||s.severity==='high');
  document.getElementById('tbody-sig-ov').innerHTML=hiSigs.length
    ? hiSigs.map(s=>`<tr><td>${pill(s.severity,SEV[s.severity])}<\\/td><td><code>${e(s.type)}<\\/code><\\/td><td>${e(s.automation)}<\\/td><td>${e(s.detail)}<\\/td><td style="white-space:nowrap">${e(s.first_seen)}<\\/td><\\/tr>`).join('')
    : '<tr><td colspan="5" style="color:var(--fg3);font-style:italic">No critical or high signals.<\\/td><\\/tr>';

  document.getElementById('tbody-recent').innerHTML=(D.recent_runs||[]).map(r=>
    `<tr><td style="font-family:var(--mono);font-size:11px;color:var(--fg3)">${e(dt(r.started_at))}<\\/td>`+
    `<td>${e(r.automation)}<\\/td><td>${e(r.host)}<\\/td>`+
    `<td>${pill(r.status,STA[r.status]??'unknown')}<\\/td>`+
    `<td style="font-variant-numeric:tabular-nums">${fms(r.duration_ms)}<\\/td>`+
    `<td style="font-size:12px;color:var(--fg2)">${e(r.synopsis)}<\\/td><\\/tr>`
  ).join('');
}

let activeFilter='all';
function renderAutomations(){
  const names=Object.keys(D.automations);
  document.getElementById('ct-auto').textContent=names.length;
  const healths=[...new Set(names.map(n=>D.automations[n].health))];
  document.getElementById('health-chips').innerHTML=['all',...healths].map(f=>
    `<button class="chip${f===activeFilter?' on':''}" data-f="${e(f)}">${f==='all'?'All automations':f}<\\/button>`
  ).join('');
  document.querySelectorAll('.chip').forEach(b=>b.addEventListener('click',()=>{activeFilter=b.dataset.f;renderAutomations();}));

  const filtered=names.filter(n=>activeFilter==='all'||D.automations[n].health===activeFilter);
  document.getElementById('auto-grid').innerHTML=filtered.map(name=>{
    const a=D.automations[name],h=a.health,last=a.last_run??{};
    const strip=a.recent_runs.slice(0,10).map(r=>`<div class="pip ${r.status}" title="${e(r.started_at)} — ${e(r.status)}"><\\/div>`).join('');
    const rSpark=spark(a.daily.map(d=>d.runs),100,24,'var(--ac)');
    const fSpark=spark(a.daily.map(d=>d.failed),100,24,'var(--crit)');
    const ver=Object.entries(a.ring_versions??{}).map(([k,v])=>`${k}: ${v}`).join(', ');
    return `<div class="acard ${h}">
      <div class="acard-top">
        <div class="hdot ${HDOT[h]??'mute'}"><\\/div>
        <div class="acard-title">
          <div class="aname">${e(name)}<\\/div>
          <div class="atags">${pill(h,h)}<\\/div>
          <div style="font-size:11px;color:var(--fg3);margin-top:4px;line-height:1.4">${e(a.description)}<\\/div>
        <\\/div>
      <\\/div>
      <div class="acard-div"><\\/div>
      <div class="acard-body">
        <div class="strip">${strip}<span class="strip-lbl">last ${a.recent_runs.slice(0,10).length} runs<\\/span><\\/div>
        <p class="syn">${e(last.synopsis??'No runs in window.')}<\\/p>
        <dl class="kvg">
          <dt>Last run<\\/dt><dd>${e(dt(last.started_at))} ${pill(last.status??'—',STA[last.status]??'unknown')}<\\/dd>
          <dt>Duration<\\/dt><dd>${fms(last.duration_ms)}<\\/dd>
          <dt>Runs<\\/dt><dd>${a.runs.total} · ${pct(a.runs.success_rate)} success<\\/dd>
          <dt>P50 / P95<\\/dt><dd>${fms(a.duration_ms.p50)} / ${fms(a.duration_ms.p95)}<\\/dd>
          <dt>Items<\\/dt><dd>${a.items.total.toLocaleString()} · ${a.items.failed} failed · ${pct(a.items.skip_rate)} skip<\\/dd>
          <dt>Runs\\/day<\\/dt><dd>${rSpark}<\\/dd>
          <dt>Failures\\/day<\\/dt><dd>${fSpark}<\\/dd>
          <dt>Version<\\/dt><dd>${e(ver)}<\\/dd>
        <\\/dl>
      <\\/div><\\/div>`;
  }).join('');
}

let sigSort={col:'severity',dir:'desc'};
const SEV_ORD={critical:0,high:1,medium:2,low:3};
function renderSignals(){
  document.getElementById('ct-sig').textContent=D.signals.length;
  const sigs=[...D.signals].sort((a,b)=>{
    let va=a[sigSort.col]??'',vb=b[sigSort.col]??'';
    if(sigSort.col==='severity'){va=SEV_ORD[va]??9;vb=SEV_ORD[vb]??9;}
    return sigSort.dir==='asc'?va<vb?-1:va>vb?1:0:va<vb?1:va>vb?-1:0;
  });
  document.getElementById('tbody-sigs').innerHTML=sigs.map((s,i)=>{
    const d=s.diagnosis;
    const expRow=d?`<tr class="xbody" id="sx${i}" hidden><td colspan="7"><div class="diag-card">
      <div class="diag-head">${e(d.root_cause)}<\\/div>
      <div style="margin:.25rem 0">Suggested: ${e(d.suggested_action)}<\\/div>
      ${pill(d.confidence+' confidence',d.confidence==='high'?'ok':'medium')}
      <div class="diag-note">Unverified: model output over unauthenticated telemetry<\\/div>
    <\\/div><\\/td><\\/tr>`:'';
    return `<tr class="xrow" data-i="${i}" title="${d?'Click for LLM diagnosis':''}">
      <td>${pill(s.severity,SEV[s.severity])}<\\/td>
      <td><code>${e(s.type)}<\\/code><\\/td>
      <td>${e(s.automation??'—')}<\\/td>
      <td>${e(s.host??'—')}<\\/td>
      <td>${e(s.detail)}<\\/td>
      <td style="white-space:nowrap">${e(s.first_seen)}<\\/td>
      <td style="font-size:11px">${e(s.owner??'—')}<\\/td>
    <\\/tr>${expRow}`;
  }).join('');
  document.querySelectorAll('.xrow').forEach(r=>r.addEventListener('click',()=>{
    const ex=document.getElementById('sx'+r.dataset.i);if(ex)ex.hidden=!ex.hidden;
  }));
  document.querySelectorAll('#tbl-sigs th[data-sort]').forEach(th=>{
    const col=th.dataset.sort;
    th.classList.toggle('s-asc',sigSort.col===col&&sigSort.dir==='asc');
    th.classList.toggle('s-desc',sigSort.col===col&&sigSort.dir==='desc');
    th.addEventListener('click',()=>{sigSort={col,dir:sigSort.col===col&&sigSort.dir==='asc'?'desc':'asc'};renderSignals();});
  });
}

function renderTelemetry(){
  document.getElementById('tbody-tele').innerHTML=Object.entries(D.automations).map(([name,a])=>{
    const it=a.items;
    return `<tr><td>${e(name)}<\\/td>`+
      `<td style="font-variant-numeric:tabular-nums">${it.total.toLocaleString()}<\\/td>`+
      `<td>${it.throughput_per_min}<\\/td>`+
      `<td>${fms(it.duration_ms.p50)}<\\/td><td>${fms(it.duration_ms.p95)}<\\/td><td>${fms(it.duration_ms.p99)}<\\/td>`+
      `<td>${pct(it.skip_rate)}<\\/td><td>${pct(it.item_failure_rate)}<\\/td><td>${pct(it.run_failure_rate)}<\\/td><\\/tr>`;
  }).join('');
  const rt=D.retries;
  document.getElementById('retry-wrap').innerHTML=
    `<p style="font-size:12px;color:var(--fg3);margin-bottom:.75rem">${rt.total_events} retry event(s) in ${rt.runs_with_retries} run(s) (${pct(rt.retry_run_rate)} of runs).<\\/p>`+
    `<div class="tbl-wrap"><table><thead><tr><th>Category<\\/th><th>Events<\\/th><th>Recovered<\\/th><th>Escalated<\\/th><th>Skipped<\\/th><th>Recovery rate<\\/th><\\/tr><\\/thead><tbody>`+
    Object.entries(rt.by_category).map(([cat,c])=>`<tr><td><code>${e(cat)}<\\/code><\\/td><td>${c.events}<\\/td><td>${c.recovered}<\\/td><td>${c.escalated}<\\/td><td>${c.skipped}<\\/td><td>${pct(c.recovery_rate)}<\\/td><\\/tr>`).join('')+
    `<\\/tbody><\\/table><\\/div>`;
}

function renderInfra(){
  const cv=c=>`${e(c.version)} ${c.ok?pill('ok','ok'):pill('→ '+c.target,'degraded')}`;
  document.getElementById('tbody-ver').innerHTML=D.versions.hosts.map(h=>`<tr>`+
    `<td style="font-family:var(--mono);font-size:12px">${e(h.host)}<\\/td>`+
    `<td>${e(h.ring)}<\\/td>`+
    `<td style="font-size:12px;color:var(--fg3)">${e(dt(h.last_seen)+' UTC')}<\\/td>`+
    `<td>${cv(h.components['wrapper-core'])}<\\/td>`+
    `<td>${cv(h.components['execution-framework'])}<\\/td>`+
    `<td style="font-size:12px">${Object.entries(h.automations).map(([n,v])=>`${e(n)} ${cv(v)}`).join('<br>')}<\\/td>`+
    `<\\/tr>`).join('');
  const ll=D.llm;
  document.getElementById('llm-kpis').innerHTML=[
    {k:'LLM calls',              v:ll.calls,                                     s:'error rate '+pct(ll.error_rate)},
    {k:'Avg latency',            v:fms(ll.duration_ms.avg),                      s:'p95 '+fms(ll.duration_ms.p95)},
    {k:'Run synopses',           v:ll.synopsis_from_llm,                         s:ll.synopsis_deterministic+' deterministic fallback'},
    {k:'Decisions / schema fail',v:ll.decisions+' / '+ll.schema_failures,        s:'LLM decision audit trail'}
  ].map(t=>`<div class="kpi"><div class="kpi-k">${e(t.k)}<\\/div><div class="kpi-v">${e(String(t.v??''))}<\\/div><div class="kpi-s">${e(t.s)}<\\/div><\\/div>`).join('');
}

const PANELS={overview:'panel-overview',automations:'panel-automations',signals:'panel-signals',telemetry:'panel-telemetry',infra:'panel-infra',evidence:'panel-evidence'};
const RENDERS={overview:renderOverview,automations:renderAutomations,signals:renderSignals,telemetry:renderTelemetry,infra:renderInfra};
const done=new Set();

function switchTab(id){
  document.querySelectorAll('.tab-btn').forEach(b=>{
    const a=b.dataset.tab===id;
    b.classList.toggle('active',a);b.setAttribute('aria-selected',String(a));
  });
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  const p=document.getElementById(PANELS[id]);if(p)p.classList.add('active');
  if(!done.has(id)&&RENDERS[id]){RENDERS[id]();done.add(id);}
  try{localStorage.setItem('amx-tab',id);}catch{}
}

document.querySelectorAll('.tab-btn').forEach(b=>b.addEventListener('click',()=>switchTab(b.dataset.tab)));

document.getElementById('theme-toggle').addEventListener('click',()=>{
  const r=document.documentElement;
  const dark=matchMedia('(prefers-color-scheme:dark)').matches;
  const cur=r.dataset.theme;
  const next=cur==='light'?'dark':cur==='dark'?undefined:dark?'light':'dark';
  if(next){r.dataset.theme=next;try{localStorage.setItem('amx-theme',next);}catch{}}
  else{delete r.dataset.theme;try{localStorage.removeItem('amx-theme');}catch{}}
});

(()=>{
  try{const t=localStorage.getItem('amx-theme');if(t)document.documentElement.dataset.theme=t;}catch{}
  renderStrip();
  let tab='overview';try{tab=localStorage.getItem('amx-tab')||'overview';}catch{}
  if(!PANELS[tab])tab='overview';
  switchTab(tab);
})();
"""

# dashboard/index.html wraps _RENDER_JS as <script>\n{body}\n</script>; program/index.html appends THEME_SCRIPT as <script>{body}</script>
_RENDER_SCRIPT_HASH = _csp_hash("\n" + _RENDER_JS + "\n")
_THEME_SCRIPT_HASH = _csp_hash(THEME_JS)

CSP_META = (
    "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data:; "
    "style-src 'unsafe-inline'; "
    f"script-src {_RENDER_SCRIPT_HASH} {_THEME_SCRIPT_HASH}; base-uri 'none'; form-action 'none'\">"
)

_PAGE_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
__CSP__
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AUTOSTATION Fleet Dashboard</title>
__FONT__
<style>
__CSS__
</style>
</head>
<body>
__NAV__
<script type="application/json" id="__dash_data__">__DATA_JSON__</script>
<main class="main">
  <section class="panel active" id="panel-overview" role="tabpanel">
    <div class="advisory"><b>Advisory data.</b> __TELEMETRY_NOTICE__.</div>
    __QUALITY_NOTE__
    <div class="sh">Fleet summary</div>
    <div class="kpi-row" id="kpi-row"></div>
    <div class="sh">Active signals</div>
    <div class="tbl-wrap"><table><thead><tr>
      <th>Severity</th><th>Signal</th><th>Automation</th><th>Detail</th><th>First seen</th>
    </tr></thead><tbody id="tbody-sig-ov"></tbody></table></div>
    <div class="sh">Recent runs</div>
    <div class="tbl-wrap"><table><thead><tr>
      <th>Started</th><th>Automation</th><th>Host</th><th>Status</th><th>Duration</th><th>Synopsis</th>
    </tr></thead><tbody id="tbody-recent"></tbody></table></div>
  </section>

  <section class="panel" id="panel-automations" role="tabpanel">
    <div class="chips" id="health-chips"></div>
    <div class="agrid" id="auto-grid"></div>
  </section>

  <section class="panel" id="panel-signals" role="tabpanel">
    <div class="sh">All signals <span style="font-weight:400;color:var(--fg3)">— click a row for LLM diagnosis</span></div>
    <div class="tbl-wrap"><table id="tbl-sigs">
      <thead><tr>
        <th data-sort="severity">Severity</th>
        <th data-sort="type">Signal</th>
        <th data-sort="automation">Automation</th>
        <th>Host</th><th>Detail</th>
        <th data-sort="first_seen">First seen</th>
        <th>Owner</th>
      </tr></thead>
      <tbody id="tbody-sigs"></tbody>
    </table></div>
  </section>

  <section class="panel" id="panel-telemetry" role="tabpanel">
    <div class="sh">Item throughput</div>
    <div class="tbl-wrap"><table><thead><tr>
      <th>Automation</th><th>Items (30d)</th><th>Throughput /min</th>
      <th>P50</th><th>P95</th><th>P99</th>
      <th>Skip rate</th><th>Item fail rate</th><th>Run fail rate</th>
    </tr></thead><tbody id="tbody-tele"></tbody></table></div>
    <div class="sh">Intelligent retry</div>
    <div id="retry-wrap"></div>
  </section>

  <section class="panel" id="panel-infra" role="tabpanel">
    <div class="sh">Fleet versions</div>
    <div class="tbl-wrap"><table><thead><tr>
      <th>Host</th><th>Ring</th><th>Last heartbeat</th>
      <th>wrapper-core</th><th>execution-framework</th><th>Automations</th>
    </tr></thead><tbody id="tbody-ver"></tbody></table></div>
    <div class="sh">LLM usage</div>
    <div class="kpi-row" id="llm-kpis"></div>
  </section>

  <section class="panel" id="panel-evidence" role="tabpanel">
    __EVIDENCE_HTML__
  </section>
</main>
<script>
__RENDER_JS__
</script>
</body>
</html>
"""


# ── Compatibility aliases used by program.py ──────────────────────────────────

def spark(values: list, w: int = 120, h: int = 28, cls: str = "ok") -> str:
    vals = [float(v or 0) for v in values]
    if not vals or max(vals) == 0:
        return f'<svg class="spark {esc(cls)}" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true"><line x1="0" y1="{h - 2}" x2="{w}" y2="{h - 2}"/></svg>'
    mx = max(vals)
    step = w / max(1, len(vals) - 1)
    pts = " ".join(f"{i * step:.1f},{h - 2 - (v / mx) * (h - 4):.1f}" for i, v in enumerate(vals))
    return f'<svg class="spark {esc(cls)}" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true"><polyline points="{pts}"/></svg>'


def pill(text, kind: str) -> str:
    return f'<span class="tag {esc(kind)}">{esc(text)}</span>'


THEME_SCRIPT = f"<script>{THEME_JS}</script>"
TELEMETRY_BANNER = f'<div class="advisory" role="note"><b>Advisory data.</b> {TELEMETRY_NOTICE}.</div>'


def _sub(template: str, **kwargs: str) -> str:
    for k, v in kwargs.items():
        template = template.replace(f"__{k.upper()}__", v)
    return template


def quality_note(report: dict) -> str:
    n = int(((report.get("data_quality") or {}).get("skipped_malformed")) or 0)
    return f'<div class="advisory" role="note">{n} malformed log record(s) were skipped and not counted.</div>' if n else ""


def render(report: dict, runs_latest: list[dict], evidence_html: str = "") -> str:
    data = dict(report)
    data["recent_runs"] = runs_latest
    data_json = (json.dumps(data, default=str)
                 .replace("&", "\\u0026")
                 .replace("<", "\\u003c")
                 .replace(">", "\\u003e"))
    ev = (evidence_html or
          '<div class="empty">No failure evidence captured in this window.<br>'
          'Evidence is collected automatically when a run fails with a browser error '
          '— screenshots and DOM snapshots will appear here.</div>')
    return _sub(
        _PAGE_TEMPLATE,
        csp=CSP_META,
        font=_FONT_LINK,
        css=CSS,
        nav=_NAV_SVG,
        data_json=data_json,
        telemetry_notice=TELEMETRY_NOTICE,
        quality_note=quality_note(report),
        evidence_html=ev,
        render_js=_RENDER_JS,
    )


INLINE_IMAGE_MAX = 500 * 1024
_TEXT_EXT = {"text/html": ".html.txt", "text/plain": ".txt", "application/json": ".json", "application/zip": ".zip",
             "image/png": ".png", "image/jpeg": ".jpg"}
_SAFE_STEM = re.compile(r"[^A-Za-z0-9_-]+")


def safe_evidence_file(name: str, ctype: str) -> str:
    """File name used on the published site: sanitised stem + an extension derived from the (validated) content type,
    so stored HTML is always served as plain text (``.html.txt``) and can never be rendered or executed by a browser."""
    stem = _SAFE_STEM.sub("_", name.rsplit(".", 1)[0])[:60].strip("_") or "file"
    return stem + _TEXT_EXT.get(ctype, ".bin")


def evidence_section(data: analytics.Data, out: Path, limit: int = 12) -> str:
    """Failure evidence (screenshots, DOM snapshots, traces). Images <=500 KB are inlined as data URIs; everything else is a
    download link; text/HTML is shown only as escaped text excerpts. Payloads are written under ``evidence/<run_id>/``."""
    records = sorted(getattr(data, "evidence", None) or [], key=lambda r: r["ts"], reverse=True)[:limit]
    if not records or not getattr(data, "archive", None):
        return ""
    parts = ["<h2>Failure evidence</h2><p class=\"sub\">Captured automatically when a run failed (redacted DOM, screenshots, traces). "
             "Stored content is never rendered as a page: text is shown escaped, HTML is a plain-text download.</p>"]
    for rec in records:
        payload = None
        if not isinstance(rec.get("_path"), str) or ".." in rec["_path"]:
            continue
        try:
            payload = json.loads((Path(data.archive) / rec["_path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            pass
        if payload is not None:
            try:
                from .ingest import validate_evidence
                if not isinstance(payload, dict) or validate_evidence(payload):
                    payload = None  # the archive is re-validated on read: never render a payload that would not pass ingest
            except Exception:  # noqa: BLE001
                payload = None
        run = data.runs_by_id.get(rec.get("run_id")) or {}
        err = run.get("error") or {}
        head = (f"<div class=\"kv\"><b>Run</b><span><code>{esc(rec.get('run_id'))}</code></span><b>Automation</b><span>{esc(rec.get('automation'))}</span>"
                f"<b>Host</b><span>{esc(rec.get('host') or run.get('host'))}</span><b>When</b><span>{esc(rec.get('ts'))}</span>"
                f"<b>Status</b><span>{esc(run.get('status') or '-')}</span><b>Error</b><span>{esc((err.get('category') or '') + ' ' + (err.get('message') or '')[:200])}</span></div>")
        body = []
        for f in (payload or {}).get("files", []):
            ctype = f.get("content_type")
            try:
                blob = base64.b64decode(f.get("data_b64") or "", validate=False)
            except ValueError:
                continue
            fname = safe_evidence_file(str(f.get("name", "file")), str(ctype))
            dest = out / "evidence" / re.sub(r"[^A-Za-z0-9._-]", "_", str(rec.get("run_id"))) / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)
            rel = dest.relative_to(out).as_posix()
            label = f"{esc(f.get('name'))} ({len(blob) // 1024 or 1} KB, {esc(ctype)})"
            if ctype in ("image/png", "image/jpeg") and len(blob) <= INLINE_IMAGE_MAX:
                body.append(f"<div><img alt=\"{esc(f.get('name'))}\" style=\"max-width:100%;border:1px solid var(--bd);border-radius:6px\" "
                            f"src=\"data:{esc(ctype)};base64,{base64.b64encode(blob).decode('ascii')}\"><div class=\"sub\"><a href=\"{esc(rel)}\">{label}</a></div></div>")
            elif ctype in ("text/html", "text/plain", "application/json"):
                excerpt = blob[:1500].decode("utf-8", "replace")
                body.append(f"<details><summary>{label} - <a href=\"{esc(rel)}\">download</a></summary><pre>{esc(excerpt)}</pre></details>")
            else:
                body.append(f"<div><a href=\"{esc(rel)}\">{label}</a></div>")
        inner = "".join(body) or "<div class=sub>payload unavailable</div>"
        parts.append(f"<div class=\"acard\" style=\"margin-bottom:.8rem\">{head}{inner}</div>")
    return "".join(parts)


def build_site(report: dict, data: analytics.Data, out_dir: str | Path, signals: list[dict] | None = None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    runs_latest = []
    for r in sorted(data.runs, key=lambda r: r["started_at"], reverse=True)[:50]:
        try:
            runs_latest.append(analytics.run_summary(r))
        except Exception:  # noqa: BLE001 - one malformed record is skipped, never fatal
            continue
    (out / "index.html").write_text(render(report, runs_latest, evidence_section(data, out)), encoding="utf-8")
    write_json(out / "data.json", report)
    write_json(out / "signals.json", {"schema_version": 1, "generated_at": report["generated_at"],
                                      "signals": signals if signals is not None else report["signals"]})
    write_json(out / "runs-latest.json", {"schema_version": 1, "generated_at": report["generated_at"], "runs": runs_latest})
    return {"files": ["index.html", "data.json", "signals.json", "runs-latest.json"], "out": str(out)}
