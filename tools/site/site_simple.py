"""The four tabs of the app - Home, Install, Fleet, Docs - composed from the staged page views.

Home and Install are small, hand-composed views. Fleet is ONE scrolling page: a sticky jump menu (anchors, not tabs) over compact sections
(Problems, Automations, Runs, Machines, Schedules, Releases, Admin actions, Value). The heavy tables the page builders already produce
(runs, machines, schedules, releases, admin panel, errors, reliability, queues, programme value) are embedded inside those sections; their own
page headings are removed and their explanatory paragraph becomes a collapsed "About this section". Everything that comes from telemetry or the
manifest is escaped here or was escaped by the page builder: the result is mounted with innerHTML by assets/app.js.
"""
from __future__ import annotations

import re

import site_common as sc
from site_common import SiteConfig, esc

from registry.portal import common as portal_common

_CRUMBS = re.compile(r'<div class="crumbs">.*?</div>\s*', re.S)
_H1 = re.compile(r"<h1\b[^>]*>.*?</h1>\s*", re.S)
_SUB = re.compile(r'<p class="sub"[^>]*>(.*?)</p>\s*', re.S)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

SECTIONS = [("problems", "Problems"), ("automations", "Automations"), ("runs", "Runs"), ("machines", "Machines"),
            ("schedules", "Schedules"), ("releases", "Releases"), ("admin", "Admin actions"), ("value", "Value")]
SECTION_IDS = [k for k, _ in SECTIONS]

ADVISORY = ('<p class="advisory" role="note"><b>Advisory data.</b> Numbers come from installed machines and are not authenticated; '
            "treat them as advisory.</p>")


def strip_chrome(html_text: str) -> tuple[str, str]:
    """(body without breadcrumb / h1 / first intro paragraph, that intro paragraph's inner html)."""
    t = _CRUMBS.sub("", html_text or "", count=1)
    t = _H1.sub("", t, count=1)
    m = _SUB.search(t[:1500])
    hint = ""
    if m:
        hint = m.group(1).strip()
        t = t[:m.start()] + t[m.end():]
    return t.strip(), hint


def _hint(hint: str) -> str:
    return f'<details class="hint"><summary>About this section</summary><p class="sub">{hint}</p></details>' if hint else ""


def _section(sec: str, title: str, body: str, hint: str = "", lead: bool = True, lazy: str = "") -> str:
    lead_html = f'<p class="lead" data-lead="{sec}"></p>' if lead else ""
    inner = (f'<div class="fbody lazy" data-lazy="{lazy}" aria-busy="true"><p class="sub">Loading&hellip;</p></div>' if lazy
             else f'<div class="fbody">{body}</div>')
    return f'<section class="fsec" id="sec-{sec}"><h2>{esc(title)}</h2>{lead_html}{_hint(hint)}{inner}</section>'


def _pill(text: str, kind: str = "") -> str:
    return f'<span class="pill {esc(kind)}">{esc(text)}</span>'


# ------------------------------------------------------------------------------------------------------------------------ Home
def home_view(cfg: SiteConfig, manifest: dict) -> str:
    n = len(manifest.get("automations") or {})
    return (
        '<section class="home">'
        "<h1>Unattended automation, installed in one click</h1>"
        '<p class="lead">Install an automation on your own Windows account, then watch it run: updates, retries and logs are handled for you.</p>'
        '<div id="fleet-now" class="fleet-now"><p class="sub">Loading status&hellip;</p></div>'
        '<p class="quick"><a class="btn primary" href="#/install">Install an automation</a> <a class="btn" href="#/fleet">Open Fleet</a></p>'
        f'<p class="sub">{n} automation{"" if n == 1 else "s"} available &middot; <a href="#/docs">Documentation</a> &middot; '
        f'<a href="{esc(cfg.repo_url)}">Source on GitHub</a></p>'
        f"{ADVISORY}</section>")


# ---------------------------------------------------------------------------------------------------------------------- Install
def _rings(entry: dict) -> str:
    rings = entry.get("rings") or {}
    return " ".join(_pill(f"{r} {v}", "info") for r, v in rings.items())


def install_view(cfg: SiteConfig, manifest: dict, details: dict[str, str]) -> str:
    """details: automation name -> its (already extracted, escaped) install guide."""
    rows = []
    for name, entry in (manifest.get("automations") or {}).items():
        if not _NAME.match(str(name)):
            continue
        entry = entry or {}
        status = str(entry.get("status") or "active")
        desc = str(entry.get("description") or "").strip()
        desc = desc if desc and "auto-registered" not in desc else "Runs on a schedule on your PC; updates itself."
        guide, _ = strip_chrome(details.get(name, ""))
        guide = re.sub(r'<div class="row"[^>]*>.*?</div>\s*(?:<p class="summary"[^>]*>.*?</p>\s*)?', "", guide, count=1, flags=re.S)
        rows.append(
            f'<article class="iitem" id="a-{esc(name)}-card"><div class="irow"><div><div class="iname">{esc(name)}</div>'
            f'<div class="isum">{esc(desc[:200])}</div><div class="irings">{_pill(status, "ok" if status == "active" else "sys")} {_rings(entry)}</div></div>'
            f'<a class="btn primary big" href="{esc(cfg.installer_url(name))}">Download installer</a></div>'
            f'<details class="idetails" id="a-{esc(name)}"><summary>Verify the download, what it changes, silent install, Intune / SCCM</summary>'
            f'<div class="ibody">{guide}</div></details></article>')
    body = "".join(rows) or '<p class="sub">No automations are published yet.</p>'
    return (
        '<section class="install"><h1>Install an automation</h1>'
        "<p class=\"lead\">Download, run, done. It installs for your own Windows account only: no administrator rights, no services. "
        "Updates arrive on their own.</p>"
        f"{body}"
        '<details class="more"><summary>Before you start, other ways to install, and rolling out to a team</summary><div class="ibody">'
        "<p><strong>You need</strong> Windows 10 or 11 (64-bit) and Microsoft Edge for browser automations. The installer is self-contained. "
        "Pick a ring only if told to: <strong>broad</strong> is the stable default, <strong>pilot</strong> gets changes first, <strong>dev</strong> is for developers "
        "(switch later by re-running the installer with <code>/RING=...</code>).</p>"
        '<p><strong>Prefer a script?</strong> <a href="install/bootstrap.ps1" download>bootstrap.ps1</a> downloads the installer, verifies its SHA-256, '
        "requires a valid code signature for the <code>broad</code> ring and installs. Download it, read it, then run "
        "<code>.\\bootstrap.ps1 -Automation &lt;name&gt; -Ring broad</code>. We do not recommend piping a web request into <code>iex</code>.</p>"
        '<p><strong>Rolling out to a team?</strong> Open an automation above: its details include Intune and SCCM settings. '
        'The catalog is also available as <a href="install/catalog.json">catalog.json</a>.</p></div></details></section>')


# ----------------------------------------------------------------------------------------------------------------------- Fleet
def _short(v, n: int = 12) -> str:
    return str(v or "")[:n]


def _automation_detail(name: str, entry: dict) -> str:
    owner = esc(str(entry.get("owner") or "-"))
    dc = esc(str(entry.get("data_classification") or "-"))
    systems = ", ".join(esc(str(s)) for s in (entry.get("systems") or [])) or "-"
    versions = entry.get("versions") or {}
    rings = entry.get("rings") or {}
    vrows = []
    for ver in sorted(versions, key=lambda v: [int(x) if x.isdigit() else 0 for x in re.split(r"[.]", str(v))], reverse=True)[:8]:
        rec = versions.get(ver) or {}
        on = ", ".join(r for r, v in rings.items() if str(v) == str(ver)) or "-"
        req = rec.get("requires") or {}
        req_s = "; ".join(f"{k} {v}" for k, v in req.items()) if isinstance(req, dict) else ""
        vrows.append(f"<tr><td>{esc(str(ver))}</td><td>{esc(on)}</td><td><code>{esc(_short(rec.get('sha256')))}</code></td><td>{esc(req_s)}</td></tr>")
    vtable = ('<div class="tw"><table><thead><tr><th>Version</th><th>In rings</th><th>SHA-256</th><th>Needs</th></tr></thead><tbody>' + "".join(vrows) +
              "</tbody></table></div>") if vrows else ""
    paused = str(entry.get("paused_reason") or "").strip()
    paused_html = f'<p class="warnbox">Paused: {esc(paused)}</p>' if paused else ""
    desc = str(entry.get("description") or "").strip()
    return (f'{paused_html}<p>{esc(desc)}</p>'
            f'<dl class="kv"><dt>Owner</dt><dd>{owner}</dd><dt>Data</dt><dd>{dc}</dd><dt>Systems</dt><dd>{systems}</dd><dt>Rings</dt><dd>{_rings(entry) or "-"}</dd></dl>'
            f'{vtable}<div class="auto-recent" data-recent="{esc(name)}"></div>'
            f'<p class="acts"><a class="btn primary" href="#/install/{esc(name)}">Install</a> <a class="btn" href="#/fleet/runs?auto={esc(name)}">Its runs</a> '
            f'<a class="btn" href="#/fleet/admin?a={esc(name)}">Admin actions</a></p>')


def automations_body(manifest: dict) -> str:
    rows = []
    for name, entry in (manifest.get("automations") or {}).items():
        if not _NAME.match(str(name)):
            continue
        entry = entry or {}
        status = str(entry.get("status") or "active")
        rings = entry.get("rings") or {}
        ver = rings.get("broad") or next(iter(rings.values()), "-")
        try:
            sched = portal_common.describe_schedule(entry)
        except Exception:  # noqa: BLE001 - an odd schedule must never break the page
            sched = "-"
        rows.append(
            f'<tr data-auto="{esc(name)}"><td><button type="button" class="rowbtn" aria-expanded="false" aria-controls="auto-{esc(name)}">{esc(name)}</button></td>'
            f'<td>{_pill(status, "ok" if status == "active" else "sys")}</td><td>{esc(str(ver))}</td><td>{esc(sched)}</td>'
            f'<td data-cell="ok">-</td><td data-cell="last">-</td></tr>'
            f'<tr class="detail" id="auto-{esc(name)}" hidden><td colspan="6">{_automation_detail(name, entry)}</td></tr>')
    if not rows:
        return '<p class="sub">Nothing to show yet: no automation is published. Add one under components/automations and release it.</p>'
    return ('<div class="tw"><table id="autos"><thead><tr><th>Automation</th><th>Status</th><th>Version</th><th>Runs</th><th>Success</th><th>Last run</th></tr></thead>'
            "<tbody>" + "".join(rows) + "</tbody></table></div>")


def fleet_view(cfg: SiteConfig, manifest: dict, parts: dict[str, str]) -> str:
    """parts: staged page key -> its extracted view html (jobs is lazy-loaded and not needed here)."""
    def get(key: str) -> tuple[str, str]:
        return strip_chrome(parts.get(key, ""))

    jump = "".join(f'<a href="#/fleet/{k}" data-jump="{k}">{esc(label)}</a>' for k, label in SECTIONS)
    more = []
    for key, title in (("errors", "Errors and fixes"), ("reliability", "Reliability"), ("queues", "Queues and approvals")):
        body, _ = get(key)
        if body:
            more.append(f'<h3 class="grp">{esc(title)}</h3>{body}')
    problems = ('<div id="problems-list" class="problems"><p class="sub">Loading&hellip;</p></div>'
                + (f'<details class="more" id="problems-more"><summary>Reliability, errors, queues and approvals</summary><div class="ibody">{"".join(more)}</div></details>'
                   if more else ""))
    m_body, m_hint = get("machines")
    s_body, s_hint = get("schedules")
    r_body, r_hint = get("audit")
    a_body, _ = get("portal-admin")
    v_body, _ = get("program")
    sections = [
        _section("problems", "Problems", problems),
        _section("automations", "Automations", automations_body(manifest)),
        _section("runs", "Runs", "", lazy="runs"),
        _section("machines", "Machines", m_body or '<p class="sub">Nothing to show yet: machines appear after they report once.</p>', m_hint),
        _section("schedules", "Schedules", s_body or '<p class="sub">Nothing to show yet.</p>', s_hint),
        _section("releases", "Releases", r_body or '<p class="sub">Nothing to show yet.</p>', r_hint),
        _section("admin", "Admin actions", a_body or '<p class="sub">Admin actions are not available in this build.</p>',
                 "Pause, resume, promote, roll back or run now. Each action produces the exact command; nothing runs from this page. Changes are reviewed as pull requests."),
    ]
    value = (f'<details class="fsec coll" id="sec-value"><summary><h2>Value</h2><span class="lead" data-lead="value"></span></summary>'
             f'<div class="fbody">{v_body or "<p class=sub>Nothing to show yet.</p>"}</div></details>')
    return (f'<div class="fleet"><nav class="jump" aria-label="Sections of this page">{jump}</nav>'
            f'<div id="fleet-banner"></div>{ADVISORY}{"".join(sections)}{value}</div>')
