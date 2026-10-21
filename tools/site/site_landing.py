"""Landing page (role-based entry) and placeholder pages for dashboards that have no data yet."""
from __future__ import annotations

from site_common import SiteConfig, esc, layout, write


def build_landing(out, m: dict, cfg: SiteConfig, doc_stems: set[str], have: dict) -> str:
    autos = m.get("automations") or {}
    active = sum(1 for e in autos.values() if (e or {}).get("status") != "paused")
    comps = m.get("components") or {}
    broad = {n: ((c or {}).get("rings") or {}).get("broad") for n, c in comps.items()}

    def role(href: str, title: str, text: str, go: str, key: str = "") -> str:
        if key and key not in doc_stems and key not in have:
            return ""
        return f'<a class="card role" href="{esc(href)}"><h3>{esc(title)}</h3><p>{esc(text)}</p><span class="go">{esc(go)} &rarr;</span></a>'

    roles = "".join([
        role("install/index.html", "I need to install an automation", "Download, verify and install for your own Windows account. No admin rights. Updates itself.", "Install center"),
        role("docs/GETTING_STARTED.html", "I build automations", "From \"I have a process\" to a running pilot: scaffold, offline lifecycle, tests, release.", "Getting started", "GETTING_STARTED"),
        role("portal/admin.html", "I operate the fleet", "Kill-switch, rollback, promotion and on-demand runs as reviewed pull requests - no YAML editing.", "Admin panel"),
        role("dashboard/index.html", "I watch the fleet", "Run health, compliance signals (missed, slow, hung), item-level metrics, LLM diagnosis, failure evidence.", "Fleet dashboard"),
        role("docs/GOVERNANCE.html", "I approve changes", "Rings, promotion checklist, data classification, and what an approval means.", "Governance", "GOVERNANCE"),
        role("jobs/index.html", "I run the automations day to day", "Job history, machines, queues and approvals, schedules and the audit trail - the operator console.", "Jobs"),
        role("program/index.html", "I need to show the value", "Hours saved, retired licences, success rate, drift and top failure causes.", "Program dashboard"),
        role("docs/index.html", "I need an answer", "API reference, standard, runbooks, security model - searchable.", "Documentation"),
    ])
    stats = (f'<div class="tiles"><div class="tile"><div class="n">{len(autos)}</div><div class="l">Automations</div></div>'
             f'<div class="tile"><div class="n" style="color:var(--green)">{active}</div><div class="l">Active</div></div>'
             f'<div class="tile"><div class="n" style="color:var(--yellow)">{len(autos) - active}</div><div class="l">Paused</div></div>'
             f'<div class="tile"><div class="n">{esc(broad.get("wrapper-core") or "-")}</div><div class="l">wrapper-core (broad)</div></div>'
             f'<div class="tile"><div class="n">{esc(broad.get("execution-framework") or "-")}</div><div class="l">framework (broad)</div></div></div>')
    registry_link = ' &middot; <a href="registry/manifest.yaml">registry manifest</a>' if have.get("registry") else ""
    body = f"""
<section class="hero"><div class="eyebrow">Unattended automation, owned by the team</div>
<h1>Unattended automation that anyone can install and everyone can operate</h1>
<p>Scripted automations (Python, Node, PowerShell) run under Windows Task Scheduler as the signed-in user, heal and update themselves from a git-governed registry,
log every item they process, and fall back to an LLM only where fixed logic cannot decide. No Orchestrator server, no per-robot licence.</p></section>
{stats}
<h2>Start here</h2>
<div class="roles">{roles}</div>
<h2>How it fits together</h2>
<div class="two">
<div class="panel"><h3>For users</h3><ol class="steps"><li><div>Open the <a href="install/index.html">install center</a>, pick the automation.</div></li><li><div>Download, verify the SHA-256, run. One minute, per-user, no admin.</div></li><li><div>It registers its schedule and keeps itself current. You never think about it again.</div></li></ol></div>
<div class="panel"><h3>For developers</h3><ol class="steps"><li><div><code>python tools/new_automation.py</code> scaffolds a repo that already runs, logs and tests.</div></li><li><div><code>python -m wrapper_core dev .</code> runs the real lifecycle offline.</div></li><li><div>Merge &rarr; CI packages it &rarr; a dev-ring pull request &rarr; approve &rarr; promote pilot &rarr; broad.</div></li></ol></div>
</div>
<h2>Links</h2>
<p><a href="{esc(cfg.repo_url)}">Repository</a> &middot; <a href="{esc(cfg.repo_url)}/actions">Pipelines</a> &middot; <a href="{esc(cfg.repo_url)}/releases/tag/installers-latest">Installer releases</a> &middot;
<a href="{esc(cfg.repo_url)}/issues/new/choose">Report a problem or request an automation</a> &middot; <a href="status.json">status.json</a>{registry_link}</p>
"""
    html_text = layout("AutoStation", body, cfg=cfg, depth=0, active="home",
                       description="AutoStation: install, build, operate and govern unattended automations - scripted, governed, self-healing.")
    write(out, "index.html", html_text)
    return "index.html"


def build_placeholder(out, cfg: SiteConfig, rel: str, title: str, active: str, message_html: str) -> str:
    body = f'<div class="crumbs"><a href="../index.html">Home</a> / {esc(title)}</div><h1>{esc(title)}</h1><div class="panel">{message_html}</div>'
    write(out, rel, layout(title, body, cfg=cfg, depth=rel.count("/"), active=active))
    return rel
