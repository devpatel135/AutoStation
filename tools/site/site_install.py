"""Install center: ``install/index.html``, one page per catalog entry, ``install/catalog.json`` and ``install/bootstrap.ps1``."""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

from site_common import SiteConfig, esc, layout, write

from registry.portal.common import (classification_pill, describe_schedule, describe_slot, describe_windows, pill, rings_of,
                                    safe_url, status_pill)
from wrapper_core import versions

HERE = Path(__file__).resolve().parent
SHA_RE = re.compile(r"(?i)\b[0-9a-f]{64}\b")
PLACEHOLDER = "0" * 64

# user-facing symptom -> cause -> fix (hand-written: these are what people actually hit on a VDI)
USER_TROUBLE = [
    ("Windows says \"Unknown publisher\" / SmartScreen blocks the installer",
     "The installer is an unsigned dev/pilot build (production releases are signed with the organisation's code-signing certificate).",
     "Verify the SHA-256 shown on this page first. If it matches and your security team approves, use <em>More info &rarr; Run anyway</em>, or right-click the file &rarr; Properties &rarr; Unblock."),
    ("The installer says git was not found",
     "Only installers configured for the SSH log transport need <code>git</code>. The downloadable installer does not; if you see this you are using an organisation build with SSH logging.",
     "Install Git for Windows (user scope), open a new terminal and confirm <code>git --version</code>, then run the installer again - it is safe to re-run."),
    ("Nothing seems to run",
     "The schedule is a Task Scheduler task under <code>\\AUTOMAGIX\\</code>, re-asserted at every sign-in. VDI session resets can clear tasks until the next sign-in.",
     "Sign out and back in (the Startup-folder script re-registers the tasks), or run <code>%LOCALAPPDATA%\\AUTOMAGIX\\python\\python.exe -m wrapper_core maintain</code>. Check <code>schtasks /Query /TN \"AUTOMAGIX\\&lt;name&gt;-&lt;slot&gt;\"</code>."),
    ("A notice says the automation is paused",
     "The platform operators used the kill-switch (manifest <code>status: paused</code> or a fleet-wide pause). The reason is in the notice.",
     "Nothing to do on your machine: it resumes by itself on the next run after operators lift the pause. Read <code>%LOCALAPPDATA%\\AUTOMAGIX\\state\\toast\\&lt;name&gt;.status.txt</code>."),
    ("The automation was updated and I did not do anything",
     "Self-heal checks the registry on every run and installs the version your ring is assigned (checksum-verified).",
     "That is by design. To see what is installed: <code>python -m wrapper_core status</code> (embedded python under <code>%LOCALAPPDATA%\\AUTOMAGIX\\python</code>)."),
    ("Self-heal reports a checksum (sha256) mismatch or the registry is unreachable",
     "A corrupt download, a proxy rewriting traffic, or the registry host being blocked from the VDI network.",
     "Open the registry URL from the install config in a browser on the same machine. If blocked, ask network operations to allow <code>*.github.io</code> (registry) and the logging Git host. The previous version keeps running meanwhile."),
    ("Browser automations fail with an Edge / profile error",
     "These automations drive Microsoft Edge with a dedicated profile under <code>%LOCALAPPDATA%\\AUTOMAGIX\\browser-profile</code>. A second Edge window using the same profile locks it.",
     "Close any Edge window opened by the automation, then let the next scheduled run retry. If it persists, open an issue (the failure evidence screenshot is attached to the run on the dashboard)."),
]


def parse_md_table(md: str, heading_contains: str) -> list[list[str]]:
    """Rows (header first) of the first markdown table after a heading that contains ``heading_contains``."""
    lines = md.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if line.startswith("#") and heading_contains.lower() in line.lower():
            j = i + 1
            while j < len(lines) and "|" not in lines[j]:
                j += 1
            rows = []
            while j < len(lines) and "|" in lines[j]:
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                if not all(re.match(r"^:?-{2,}:?$", c) for c in cells):
                    rows.append(cells)
                j += 1
            return rows
    return []


def fetch_installer_sha(url: str, timeout: float = 10.0) -> str | None:
    """SHA-256 from the ``.sha256`` asset next to a release installer; None when unreachable (never raises)."""
    try:
        req = urllib.request.Request(url + ".sha256", headers={"User-Agent": "autostation-site-build"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https release URL
            m = SHA_RE.search(resp.read(4096).decode("utf-8", "replace"))
            return m.group(0).lower() if m else None
    except Exception:  # noqa: BLE001 - offline builds are normal
        return None


def _default_ring(entry: dict, order: list[str]) -> str:
    for r in reversed(order):
        if (entry.get("rings") or {}).get(r):
            return r
    return order[-1] if order else "broad"


def _cmd_block(bid: str, text: str, ring_template: bool = False) -> str:
    attr = f' data-ring-text="{esc(text)}"' if ring_template else ""
    return f'<div class="cmd"><pre id="{esc(bid)}"{attr}>{esc(text.replace("{ring}", "broad") if ring_template else text)}</pre><button type="button" data-copy="{esc(bid)}">Copy</button></div>'


def build_catalog_json(m: dict, cfg: SiteConfig, shas: dict[str, str | None]) -> dict:
    autos = {}
    for name, e in (m.get("automations") or {}).items():
        e = e or {}
        autos[name] = {"status": e.get("status", "active"), "paused_reason": e.get("paused_reason") or "", "rings": e.get("rings") or {},
                       "installer": {"url": cfg.installer_url(name), "sha256": shas.get(name)},
                       "package_sha256": {v: (r or {}).get("sha256") for v, r in (e.get("versions") or {}).items()}}
    return {"schema_version": 1, "generated_at": cfg.built_at, "registry_url": cfg.registry_url, "automations": autos}


def build_install_center(out: Path, m: dict, cfg: SiteConfig, *, runbooks_md: str = "", runtimes: dict | None = None,
                         shas: dict[str, str | None] | None = None) -> dict:
    """Write install/ and return {"pages": [...], "shas": {...}}."""
    runtimes = runtimes or {}
    shas = shas if shas is not None else {}
    order = rings_of(m)
    autos = m.get("automations") or {}
    pages = []

    catalog = build_catalog_json(m, cfg, shas)
    write(out, "install/catalog.json", json.dumps(catalog, indent=2, sort_keys=True) + "\n")
    boot = (HERE / "bootstrap.ps1").read_text(encoding="utf-8").replace("@@PAGES_URL@@", cfg.pages_url)
    write(out, "install/bootstrap.ps1", boot)

    # ---- index ----------------------------------------------------------------------------------------
    cards = []
    for name, e in autos.items():
        e = e or {}
        cat = e.get("catalog") or {}
        summary = cat.get("summary") or e.get("description") or ""
        dr = _default_ring(e, order)
        v = (e.get("rings") or {}).get(dr)
        sha = shas.get(name)
        rv = e.get("rings") or {}
        dl = (f'<a class="btn primary" href="{esc(cfg.installer_url(name))}">Download installer</a>' if v and e.get("status") != "paused"
              else '<span class="btn disabled">Not available</span>')
        ring_pills = " ".join(pill(f"{r} {rv.get(r) or '-'}", "info" if rv.get(r) else "") for r in order)
        sha_txt = ("SHA-256 " + esc(sha[:16]) + "...") if sha else "SHA-256: see the release page"
        cards.append(
            f'<article class="card" id="{esc(name)}"><div class="row"><a class="name" href="{esc(name)}.html">{esc(name)}</a><span>{status_pill(e)}</span></div>'
            f'<p class="summary">{esc(summary)}</p><dl class="kv"><dt>Owner</dt><dd>{esc(e.get("owner"))}</dd>'
            f'<dt>Runs</dt><dd>{esc(describe_schedule(e))}</dd><dt>Data</dt><dd>{classification_pill(e.get("data_classification"))}</dd>'
            f'<dt>Rings</dt><dd>{ring_pills}</dd></dl>'
            f'<div class="dl">{dl} <a class="btn" href="{esc(name)}.html">Install guide &amp; details</a>'
            f'<span class="sha">{sha_txt}</span></div></article>')
    intro = f"""
<div class="crumbs"><a href="../index.html">Home</a> / Install</div>
<h1>Install an automation</h1>
<p>Everything installs <strong>for your own Windows account only</strong> - no administrator rights, no services, no drivers. The installer fetches
what is missing, registers the schedule, and keeps itself up to date from the registry.</p>
<h2>Before you start</h2>
<div class="two">
<div class="panel"><h3>You need</h3><ul style="margin-left:1.2rem"><li>Windows 10 or 11 (x64) - a normal VDI session is fine</li><li>Nothing else: the installer is self-contained (own Python, bundled automation)</li>
<li>Microsoft Edge for browser automations (already part of Windows)</li><li>Optional: network access to <code>{esc(cfg.owner)}.github.io</code> for updates</li></ul></div>
<div class="panel"><h3>Which ring?</h3><p><strong>broad</strong> is the stable ring for everyone. <strong>pilot</strong> gets changes first, so join it only if you volunteered.
<strong>dev</strong> is for automation developers. You can switch later by re-running the installer with <code>/RING=...</code>.</p></div></div>
<h2>Automations</h2>
<div class="grid">{''.join(cards) or '<p class="empty">No automations are registered yet.</p>'}</div>
<h2>Prefer a script?</h2>
<p><a href="bootstrap.ps1" download>bootstrap.ps1</a> downloads the installer, <strong>verifies its SHA-256</strong> against the hash published here and on the release, then installs
for you. Download it, read it, then run: <code>.\\bootstrap.ps1 -Automation &lt;name&gt; -Ring broad</code>. (We deliberately do not recommend piping a web request into <code>iex</code>.)</p>
<h2>Rolling out to a team?</h2>
<p>Each install guide has Intune and SCCM settings (install/uninstall command lines and detection rules). The catalog is also available as
<a href="catalog.json">catalog.json</a> for scripting.</p>
"""
    write(out, "install/index.html", layout("Install center", intro, cfg=cfg, depth=1, active="install",
                                            description="Install AUTOSTATION automations for your own Windows account - no admin rights."))
    pages.append("install/index.html")

    ops_rows = parse_md_table(runbooks_md, "Failure triage")
    ops_table = ""
    if ops_rows and len(ops_rows) > 1:
        ops_table = ('<h3>If you operate this automation: what each alert means</h3><div class="tbl-wrap"><table><tr>'
                     + "".join(f"<th>{esc(c)}</th>" for c in ops_rows[0]) + "</tr>"
                     + "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in ops_rows[1:]) + "</table></div>"
                     f'<p class="sub"><a href="../docs/RUNBOOKS.html">Full runbooks</a></p>')

    # ---- one page per automation ----------------------------------------------------------------------
    for name, e in autos.items():
        e = e or {}
        cat = e.get("catalog") or {}
        rings = e.get("rings") or {}
        dr = _default_ring(e, order)
        sha = shas.get(name)
        paused = e.get("status") == "paused"
        runtime = runtimes.get(name)
        needs_edge = runtime == "node" or "browser" in " ".join(map(str, cat.get("tags") or [])).lower()
        installer = cfg.installer_url(name)
        fname = f"{name}-setup.exe"
        ring_opts = "".join(f'<option value="{esc(r)}"{" selected" if r == dr else ""}>{esc(r)}</option>' for r in order)
        ring_vers = "".join(f'<tr><td>{esc(r)}</td><td><code>{esc(rings.get(r) or "-")}</code></td></tr>' for r in order)
        sha_line = (f'<div class="sha">installer SHA-256: <span id="sha-{esc(name)}">{esc(sha)}</span></div>' if sha else
                    f'<div class="sha">installer SHA-256: not available at build time - open the <a href="{esc(cfg.repo_url)}/releases/tag/installers-latest">release page</a> and compare the <code>{esc(fname)}.sha256</code> asset.</div>')
        verify_cmd = (f'$expected = "{sha}"\n' if sha else '$expected = "<paste the SHA-256 from the release page>"\n') + \
                     f'$actual = (Get-FileHash .\\{fname} -Algorithm SHA256).Hash.ToLower()\nif ($actual -eq $expected) {{ "OK - hash matches" }} else {{ "MISMATCH - do not run it" }}\nGet-AuthenticodeSignature .\\{fname} | Format-List Status, SignerCertificate'
        silent = f".\\{fname} /S /RING={{ring}}"
        uninst = f'"%LOCALAPPDATA%\\AUTOMAGIX\\uninstall-{name}.exe" /S'
        key = f"HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\AUTOMAGIX-{name}"
        sched = (e.get("schedule") or {})
        slot_rows = "".join(
            f"<tr><td><code>{esc(s.get('id'))}</code></td><td>{esc(describe_slot(s, sched.get('timezone')))}</td>"
            f"<td class=\"mono\">{esc(json.dumps(s.get('params') or {}, sort_keys=True))}</td></tr>" for s in (sched.get("slots") or []))
        baseline = (sched.get("expected_duration_ms") or {}).get("baseline")
        eh = e.get("error_handling") or {}
        links = "".join(f'<li><a href="{esc(safe_url(l.get("url")))}" rel="noopener">{esc(l.get("title") or l.get("url"))}</a></li>'
                        for l in (cat.get("links") or []) if isinstance(l, dict) and safe_url(l.get("url") or ""))
        reqs = ["Windows 10 or 11 (x64); standard user is enough - <strong>no administrator rights</strong>",
                "Nothing else to install: the installer bundles its own Python and this automation. It works offline on first install",
                f"Optional: network access to <code>{esc(cfg.owner)}.github.io</code> (HTTPS) so it can pick up updates and the operators' pause switch"]
        if needs_edge:
            reqs.append("Microsoft Edge (preinstalled on Windows) - the automation drives it with its own profile; you must be signed in to the target site with your normal account")
        changes = [
            "Files under <code>%LOCALAPPDATA%\\AUTOMAGIX\\</code> only: <code>python\\</code> (embedded runtime), <code>wrapper-core\\</code>, <code>execution-framework\\</code>, "
            f"<code>automations\\{esc(name)}\\</code>, <code>config\\</code>, <code>state\\</code>, <code>logs\\</code>, <code>runs\\</code> (14-day scratch)",
            "Connected builds keep a logging token for the project's private logs repository, encrypted with Windows DPAPI under <code>secrets\\</code> (bound to your Windows account). It can only add log files. No passwords for any other system are ever stored",
            "Every run's log, heartbeat and (on failure) a screenshot and redacted page snapshot are sent over HTTPS to that logs repository; a copy of recent runs stays in <code>%LOCALAPPDATA%\\AUTOMAGIX\\runs\\</code> for 14 days. Standalone builds keep logs only in <code>local-logs\\</code>",
            f"Scheduled tasks (your account, least privilege) under <code>\\AUTOMAGIX\\{esc(name)}-&lt;slot&gt;</code>, plus a sign-in script <code>AUTOMAGIX-maintain.cmd</code> in your Startup folder that re-registers them",
            f"Current-user registry keys: <code>HKCU\\Software\\AUTOMAGIX</code> and the uninstall entry <code>{esc(key)}</code>",
            "Outbound connections (HTTPS only): the registry on GitHub Pages (read-only, update check on every run and sign-in) and, in connected builds, api.github.com (log upload). Nothing listens on the network; no services, drivers or admin elevation",
            "On the automation's first run, it executes once immediately to prove it works",
        ]
        paused_banner = (f'<div class="warnbox"><strong>Paused:</strong> {esc(e.get("paused_reason") or "no reason recorded")}. You can still install it; it will not run until operators resume it.</div>'
                         if paused else "")
        issue = cfg.issue_url("bug-automation-failure.yml", automation=name, title=f"[{name}] ")
        silent_block = _cmd_block(f"silent-{name}", silent, True)
        verify_block = _cmd_block(f"verify-{name}", verify_cmd)
        ring_ver_attrs = " ".join(f'data-ring-ver-{r}="{esc(rings.get(r) or "")}"' for r in order)
        body = f"""
<div class="crumbs"><a href="../index.html">Home</a> / <a href="index.html">Install</a> / {esc(name)}</div>
<div class="row" style="display:flex;gap:.8rem;align-items:center;flex-wrap:wrap"><h1 style="font-family:var(--mono)">{esc(name)}</h1>{status_pill(e)} {classification_pill(e.get("data_classification"))}</div>
<p class="summary" style="margin-top:.4rem">{esc(cat.get("summary") or e.get("description"))}</p>
{paused_banner}
<h2>Get it</h2>
<div class="panel accent">
  <div class="controls"><label for="ring-select" class="mono">Your ring</label><select id="ring-select">{ring_opts}</select>
  <span class="mono">version for that ring: <strong data-ring-ver="1" {ring_ver_attrs}>{esc(rings.get(dr) or "-")}</strong></span></div>
  <p><a class="btn primary" href="{esc(installer)}" id="dl-{esc(name)}">Download {esc(fname)}</a></p>
  {sha_line}
  <ol class="steps" style="margin-top:1rem">
    <li><div><strong>Download</strong> the installer with the button above (a GitHub Release asset).</div></li>
    <li><div><strong>Verify</strong> it (30 seconds; do this - it is how you know the file is the one we published):{verify_block}</div></li>
    <li><div><strong>Run</strong> it by double-clicking, or silently for your ring:{silent_block}</div></li>
    <li><div><strong>That is all.</strong> It registers its schedule and runs once. Updates arrive on their own; you never download it again.</div></li>
  </ol>
  <p class="sub">Or use the verifying script: <code>.\\bootstrap.ps1 -Automation {esc(name)} -Ring {esc(dr)}</code> (<a href="bootstrap.ps1" download>download bootstrap.ps1</a>).</p>
</div>
<h2>What this automation does</h2>
<dl class="kv">
<dt>Owner</dt><dd><a href="mailto:{esc(e.get("owner"))}">{esc(e.get("owner"))}</a>{(" / " + esc(e.get("owner_team"))) if e.get("owner_team") else ""}</dd>
<dt>Process owner</dt><dd>{esc(cat.get("process_owner") or "-")}</dd>
<dt>Systems it touches</dt><dd>{esc(", ".join(map(str, cat.get("systems") or e.get("systems") or [])) or "none")}</dd>
<dt>Data classification</dt><dd>{classification_pill(e.get("data_classification"))} <span class="sub">LLM use is approved up to: {esc(e.get("llm_allowed_classification") or "internal")}</span></dd>
<dt>Tags</dt><dd>{" ".join(pill(t) for t in (cat.get("tags") or [])) or "-"}</dd>
</dl>
{('<h3>Links</h3><ul style="margin-left:1.2rem">' + links + "</ul>") if links else ""}
<h2>When it runs</h2>
<div class="tbl-wrap"><table><tr><th>Slot</th><th>Schedule</th><th>Parameters</th></tr>{slot_rows or '<tr><td colspan="3" class="empty">No schedule</td></tr>'}</table></div>
<p class="summary">{("Maintenance windows (it will not run): " + esc(describe_windows(e)) + ". ") if describe_windows(e) else ""}{("Typical duration " + esc(round(baseline / 60000, 1)) + " min. ") if baseline else ""}On failure it retries up to {esc(eh.get("max_retries", 3))} times for network/availability problems and skips bad data items instead of failing the whole run.{" It shows a notification when it completes." if e.get("notify_on_complete") else ""}</p>
<h2>Requirements</h2>
<ul style="margin-left:1.2rem">{"".join(f"<li>{r}</li>" for r in reqs)}</ul>
<h2>What the installer changes on your machine</h2>
<ul style="margin-left:1.2rem">{"".join(f"<li>{c}</li>" for c in changes)}</ul>
<h2>Versions</h2>
<div class="two"><div><table><tr><th>Ring</th><th>Version</th></tr>{ring_vers}</table></div>
<div><table><tr><th>Version</th><th>Package SHA-256</th></tr>{"".join(f'<tr><td><code>{esc(v)}</code></td><td class="sha">{("PLACEHOLDER - not a real release" if (r or {}).get("sha256") == PLACEHOLDER else esc((r or {}).get("sha256")))}</td></tr>' for v, r in sorted((e.get("versions") or {}).items(), key=lambda kv: versions.parse(kv[0]), reverse=True))}</table></div></div>
<h2>Enterprise deployment (Intune / SCCM)</h2>
<div class="two">
<div class="panel"><h3>Microsoft Intune (Win32 app)</h3>
<ul style="margin-left:1.2rem"><li>Install behaviour: <strong>User</strong></li><li>Install command: <code>{esc(fname)} /S /RING=broad</code></li>
<li>Uninstall command: <code>{esc(uninst)}</code></li><li>Detection rule: <em>Registry</em> &rarr; key <code>{esc(key)}</code> &rarr; value exists <code>DisplayName</code></li>
<li>Requirement: Windows 10 1809+ x64 (nothing else; the installer is self-contained)</li></ul></div>
<div class="panel"><h3>SCCM / Configuration Manager</h3>
<ul style="margin-left:1.2rem"><li>Deployment type: script installer, <em>Install for user</em>, run as logged-on user, no admin</li><li>Install: <code>{esc(fname)} /S /RING=broad</code></li>
<li>Detection: file exists <code>%LOCALAPPDATA%\\AUTOMAGIX\\automations\\{esc(name)}\\automation.yaml</code> (or the HKCU key above)</li>
<li>Exit codes: 0 success; non-zero = failure (see <code>%LOCALAPPDATA%\\AUTOMAGIX\\logs\\wrapper.log</code>)</li></ul></div></div>
<h2>Pause, update, uninstall</h2>
<ul style="margin-left:1.2rem"><li><strong>Pause:</strong> only platform operators can pause (kill-switch via the <a href="../portal/admin.html">admin panel</a>); you will see a notice, nothing to do locally.</li>
<li><strong>Update:</strong> automatic on each run, checksum-verified, no action needed.</li>
<li><strong>Uninstall:</strong> Settings &rarr; Apps &rarr; "AUTOMAGIX {esc(name)}", or silently: <code>{esc(uninst)}</code>. It removes the scheduled tasks, the payload, the encrypted key and credential, and shared components when nothing else uses them. It reports a final "decommissioned" event so operators know.</li></ul>
<h2>Troubleshooting</h2>
<div class="tbl-wrap"><table><tr><th>Symptom</th><th>Likely cause</th><th>What to do</th></tr>
{"".join(f"<tr><td>{s}</td><td>{c}</td><td>{f}</td></tr>" for s, c, f in USER_TROUBLE)}</table></div>
{ops_table}
<h2>Get help</h2>
<p><a class="btn primary" href="{esc(issue)}" rel="noopener">Report a problem with {esc(name)}</a>
<a class="btn" href="mailto:{esc(e.get("owner"))}?subject={esc(name)}">Email the owner</a> <a class="btn" href="../docs/RUNBOOKS.html">Runbooks</a>
<a class="btn" href="../portal/automation/{esc(name)}.html">Catalog entry</a></p>
"""
        write(out, f"install/{name}.html", layout(f"Install {name}", body, cfg=cfg, depth=1, active="install",
                                                   description=(cat.get("summary") or e.get("description") or "")[:200]))
        pages.append(f"install/{name}.html")
    return {"pages": pages, "catalog": catalog}
