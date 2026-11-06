"""HTML helpers. RULE: every manifest/signal/run-derived string goes through ``esc`` (or ``json_for_script``)."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re

from wrapper_core.paths import RINGS

from .theme import CSS, LOGO_SVG, THEME_HEAD_JS, THEME_TOGGLE_JS

DEFAULT_CONFIG = {
    "title": "AUTOSTATION",
    "gitlab_url": "https://gitlab.example.internal",
    "project_path": "automation/automation-registry",
    "default_branch": "main",
    "ops_ref": "main",
    "installers_path": "installers",
    # --- GitHub mode (platform = "github"): links point at GitHub Actions / PRs / Releases instead of GitLab ---
    "platform": "gitlab",
    "github_repo": "",                      # owner/name, e.g. devpatel135/AutoStation
    "manifest_path": "manifest.yaml",       # path of manifest.yaml inside the repo
    "ops_workflow": "ops-action.yml",
    "installer_url_template": "",           # e.g. https://github.com/{repo}/releases/download/{name}-v{version}/{name}-{version}-setup.exe
}


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def json_for_script(obj) -> str:
    """JSON safe to embed inside <script type="application/json">: no '<', '>', '&' or line separators."""
    text = json.dumps(obj, ensure_ascii=True, sort_keys=True)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def safe_url(url: str) -> str | None:
    """Only http(s)/mailto links become anchors; anything else (javascript:, data:) is rendered as plain text."""
    u = str(url).strip()
    return u if re.match(r"^(https?://|mailto:)", u, re.I) else None


def installer_rel(config: dict, name: str, version: str) -> str:
    """Where the installer for ``name`` ``version`` lives: a relative path, or an absolute URL when
    ``installer_url_template`` is configured (GitHub Releases)."""
    tpl = str(config.get("installer_url_template") or "")
    if tpl:
        return tpl.format(repo=config.get("github_repo", ""), name=name, version=version)
    return f"{config.get('installers_path', 'installers').strip('/')}/{name}-{version}-setup.exe"


def rel_href(config: dict, rel: str, depth_prefix: str = "") -> str:
    """Prefix a relative link with ``depth_prefix`` (e.g. ``../``) but leave absolute URLs alone."""
    return rel if rel.startswith(("http://", "https://")) else f"{depth_prefix}{rel}"


def rings_of(m: dict) -> list[str]:
    return [r for r in RINGS if r in (m.get("rings") or RINGS)]


_DAY3 = {"monday": "Mon", "tuesday": "Tue", "wednesday": "Wed", "thursday": "Thu", "friday": "Fri", "saturday": "Sat", "sunday": "Sun"}


def _days(days) -> str:
    return ", ".join(_DAY3.get(str(d).lower(), str(d)) for d in days)


def describe_slot(slot: dict, tz: str | None) -> str:
    t, z = slot.get("type"), f" {tz}" if tz else ""
    if t == "daily":
        return f"Daily at {slot.get('time')}{z}"
    if t == "weekly":
        return f"{_days(slot.get('days') or ['Monday'])} at {slot.get('time')}{z}"
    if t == "hourly":
        n = int(slot.get("every", 1))
        return "Every hour" if n == 1 else f"Every {n} hours"
    if t == "poll":
        return f"Polls every {slot.get('every_minutes')} min for triggers"
    if t == "weekdays":
        return f"Every weekday at {slot.get('time')}{z}"
    if t == "monthly":
        day = slot.get("day", 1)
        return f"Monthly on the {'last day' if str(day) == 'last' else 'day ' + str(day)} at {slot.get('time')}{z}"
    if t == "interval":
        return f"Every {slot.get('every_minutes')} min" + (f" between {slot['window']}" if slot.get("window") else "") + z
    if t == "cron":
        return f"Cron {slot.get('expr')}{z}"
    return str(t)


def describe_schedule(entry: dict) -> str:
    sch = entry.get("schedule") or {}
    slots = sch.get("slots") or []
    if not slots:
        return "No schedule"
    return "; ".join(describe_slot(s, sch.get("timezone")) for s in slots)


def describe_windows(entry: dict) -> str:
    wins = (entry.get("schedule") or {}).get("maintenance_windows") or []
    parts = []
    for w in wins:
        if w.get("days") and w.get("daily"):
            parts.append(f"{_days(w['days'])} {w['daily']}")
        elif w.get("days"):
            parts.append(_days(w["days"]))
        elif w.get("daily"):
            parts.append(str(w["daily"]))
    return ", ".join(parts)


def pill(text, kind: str = "") -> str:
    return f'<span class="pill {esc(kind)}">{esc(text)}</span>'


def classification_pill(c) -> str:
    kind = {"public": "ok", "internal": "info", "confidential": "warn", "restricted": "bad"}.get(str(c), "")
    return pill(c or "unclassified", kind)


def status_pill(entry: dict) -> str:
    if entry.get("status") == "paused":
        return pill("paused", "warn")
    return pill("active", "ok")


def health_pill(h: dict | None) -> str:
    if not h:
        return pill("no telemetry", "")
    kind = {"healthy": "ok", "degraded": "warn", "failing": "bad"}.get(h.get("status"), "")
    return pill(h.get("status", "unknown"), kind)


NAV = [("overview.html", "Overview", "overview"), ("index.html", "Catalog", "catalog"),
       ("health.html", "Health", "health"), ("fleet.html", "Fleet", "fleet"),
       ("admin.html", "Admin", "admin"), ("audit.html", "Audit", "audit")]

# The portal page never loads a font or fetches anything: nothing but inline style, data: images and the generator-authored inline
# scripts (allowed by SHA-256, never 'unsafe-inline') may run.
_INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)


def csp_meta(*inline_scripts: str) -> str:
    hs = "".join(" 'sha256-" + base64.b64encode(hashlib.sha256(code.encode("utf-8")).digest()).decode("ascii") + "'" for code in inline_scripts)
    return ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'; '
            f'script-src{hs}; base-uri \'none\'; form-action \'none\'">')


CSP_META = csp_meta(THEME_HEAD_JS, THEME_TOGGLE_JS)
TELEMETRY_NOTICE = "Telemetry is submitted by installed clients and is not authenticated; treat numbers as advisory"
TELEMETRY_BANNER = f'<div class="banner" role="note"><strong>Advisory data.</strong> {TELEMETRY_NOTICE}.</div>'


def _status_strip(status_data: dict | None, base: str) -> str:
    if not status_data:
        return '<div class="status-strip"></div>'
    healthy = status_data.get("healthy", 0)
    attention = status_data.get("attention", 0)
    paused = status_data.get("paused", 0)
    machines = status_data.get("machines", 0)
    silent = status_data.get("silent", 0)
    sig_count = status_data.get("signals", 0)
    fleet_paused = status_data.get("fleet_paused", False)
    badge_cls = "crit" if fleet_paused else ("warn" if sig_count else "ok")
    badge_txt = "FLEET PAUSED" if fleet_paused else (f"{sig_count} signal{'s' if sig_count != 1 else ''}" if sig_count else "All clear")
    silent_txt = f"&nbsp;·&nbsp;<span style='color:var(--crit);font-weight:600'>{esc(str(silent))} silent</span>" if silent else ""
    return (f'<div class="status-strip">'
            f'<div class="ss"><span class="ss-dot ok"></span><span class="ss-n ok">{healthy}</span><span>healthy</span></div>'
            f'<div class="ss"><span class="ss-dot warn"></span><span class="ss-n warn">{attention}</span><span>attention</span></div>'
            f'<div class="ss"><span class="ss-dot mute"></span><span class="ss-n" style="color:var(--mute)">{paused}</span><span>paused</span></div>'
            f'<div class="ss" style="border-right:none">{machines} machines{silent_txt}</div>'
            f'<div class="fleet-badge {esc(badge_cls)}" style="margin-left:auto">{esc(badge_txt)}</div>'
            f'</div>')


def layout(title: str, body: str, *, config: dict, active: str = "", depth: int = 0, manifest_text: str = "",
           extra_head: str = "", extra_js: str = "", status_data: dict | None = None) -> str:
    base = "../" * depth
    tabs = "".join(f'<a href="{base}{href}" class="nav-tab{" active" if key == active else ""}">{label}</a>'
                   for href, label, key in NAV)
    if config.get("home_url"):
        tabs = f'<a href="{esc(base + str(config["home_url"]))}" class="nav-tab">Site home</a>' + tabs
    digest = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()[:12] if manifest_text else "n/a"
    brand = esc(config.get("title", "AUTOSTATION"))
    version_hint = esc(str(config.get("portal_version", "")))
    ver_html = f'<div class="nav-ver">{version_hint}</div>' if version_hint else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
{csp_meta(THEME_HEAD_JS, THEME_TOGGLE_JS, *_INLINE_SCRIPT.findall(extra_js))}
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — {brand}</title>
<script>{THEME_HEAD_JS}</script>
<style>{CSS}</style>{extra_head}
</head><body>
<nav class="site-nav" aria-label="Main">
  <a class="nav-brand" href="{base}overview.html">{LOGO_SVG}<div><div class="nav-wm">{brand}</div>{ver_html}</div></a>
  <div class="nav-tabs">{tabs}</div>
  <div class="nav-end">
    <span class="nav-digest">manifest sha256 {esc(digest)}…</span>
    <button id="theme-toggle" type="button" title="Toggle light/dark theme" aria-label="Toggle theme">Theme</button>
  </div>
</nav>
{_status_strip(status_data, base)}
<div class="page">
{TELEMETRY_BANNER}
{body}
<footer class="foot">Generated from manifest.yaml (sha256 {esc(digest)}). Static site: no server, no scripts that call home.</footer>
</div><script>{THEME_TOGGLE_JS}</script>{extra_js}</body></html>
"""
