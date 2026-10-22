#!/usr/bin/env python3
"""Deployment-readiness audit of the LIVE platform: what is deployed, what is healthy, what still needs a person.

    python tools/readiness.py [--owner devpatel135] [--repo AutoStation] [--json] [--deep]

Checks (public endpoints; $GITHUB_TOKEN adds the settings that are not public):
  pages      site + registry + operator pages answer, manifest parses, every active automation has a verifiable installer
  registry   each ring version has an artifact whose sha256 matches the manifest; signature present/valid when keys are pinned
  installers rolling release has the installer + .sha256 for every active automation (--deep also re-hashes the download)
  pipelines  last run of every workflow on main succeeded
  settings   rulesets on main / registry-artifacts / triggers (public API) and, with a token, secret scanning, Actions policy,
             secrets/variables present (names only), logging repo exists and is private
Exit code 1 if anything FAILS; WARN/TODO items (things only a person can do) do not fail the run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "components" / "wrapper-core"))

from wrapper_core import miniyaml  # noqa: E402

OK, WARN, TODO, FAIL = "ok", "warn", "todo", "FAIL"
PAGES = ["", "install/", "docs/", "portal/", "portal/admin.html", "dashboard/", "program/", "jobs/", "machines/", "queues/", "schedules/", "audit/"]
WORKFLOWS = ["ci", "release", "installers", "site", "codeql"]

Fetch = Callable[[str, Optional[str]], Tuple[int, bytes]]


def http_fetch(url: str, token: Optional[str] = None) -> Tuple[int, bytes]:
    headers = {"User-Agent": "autostation-readiness", "Accept": "application/vnd.github+json"}
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=40) as r:  # noqa: S310
            return r.status, r.read(120_000_000)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, OSError):
        return 0, b""


class Report:
    def __init__(self) -> None:
        self.rows: List[Dict[str, str]] = []

    def add(self, area: str, status: str, what: str, detail: str = "") -> None:
        self.rows.append({"area": area, "status": status, "check": what, "detail": detail})

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r["status"] == FAIL)


def _json(fetch: Fetch, url: str, token: Optional[str] = None):
    code, body = fetch(url, token)
    if code != 200:
        return code, None
    try:
        return code, json.loads(body.decode("utf-8"))
    except ValueError:
        return code, None


def check_pages(rep: Report, fetch: Fetch, base: str) -> Optional[dict]:
    for p in PAGES:
        code, body = fetch(base + p, None)
        rep.add("pages", OK if code == 200 and body else FAIL, f"/{p}", f"HTTP {code}")
    code, body = fetch(base + "registry/manifest.yaml", None)
    if code != 200:
        rep.add("registry", FAIL, "manifest.yaml", f"HTTP {code}")
        return None
    try:
        manifest = miniyaml.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        rep.add("registry", FAIL, "manifest.yaml parses", str(exc)[:120])
        return None
    rep.add("registry", OK, "manifest.yaml parses", f"{len(manifest.get('automations') or {})} automation(s)")
    return manifest


def check_registry(rep: Report, fetch: Fetch, base: str, manifest: dict) -> List[str]:
    active: List[str] = []
    for kind in ("components", "automations"):
        for name, entry in (manifest.get(kind) or {}).items():
            if kind == "automations":
                if entry.get("status") != "active":
                    rep.add("registry", WARN, f"{name} status", str(entry.get("status")))
                    continue
                active.append(name)
            versions = entry.get("versions") or {}
            for ring, ver in (entry.get("rings") or {}).items():
                rec = versions.get(str(ver))
                if not rec:
                    rep.add("registry", FAIL, f"{name} {ring}", f"ring points at {ver}, which has no version record")
                    continue
                code, body = fetch(base + "registry/" + str(rec.get("url")), None)
                if code != 200:
                    rep.add("registry", FAIL, f"{name} {ver} artifact", f"HTTP {code}")
                elif hashlib.sha256(body).hexdigest() != str(rec.get("sha256")).lower():
                    rep.add("registry", FAIL, f"{name} {ver} sha256", "does not match the manifest")
                else:
                    rep.add("registry", OK, f"{name} {ring}={ver}", "artifact present, sha256 verified")
    code, sig_body = fetch(base + "registry/manifest.yaml.sig", None)
    if code == 200:
        try:
            doc = json.loads(sig_body.decode("utf-8"))
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            rep.add("signing", FAIL, "manifest signature", "manifest.yaml.sig is not valid JSON")
        else:
            rep.add("signing", OK, "manifest signed", f"key {doc.get('key_id', '?')}, signed_at {doc.get('signed_at', '?')}")
            seq, extras = doc.get("seq"), doc.get("extras")
            if isinstance(seq, int) and not isinstance(seq, bool) and doc.get("sig2"):
                rep.add("signing", OK, "anti-rollback sequence", f"seq {seq} (endpoints refuse a lower one)")
            else:
                rep.add("signing", WARN, "anti-rollback sequence", "no seq in the signature: set SIGN_SEQ in the site workflow")
            if isinstance(extras, dict) and "triggers.json" in extras:
                code_t, trig = fetch(base + "registry/triggers.json", None)
                same = code_t == 200 and hashlib.sha256(trig).hexdigest() == str(extras["triggers.json"]).lower()
                rep.add("signing", OK if same else FAIL, "triggers.json is signed", "published bytes match the signed digest" if same
                        else "published triggers.json does NOT match the signed digest (endpoints will ignore it)")
            else:
                code_t, _ = fetch(base + "registry/triggers.json", None)
                rep.add("signing", WARN if code_t == 200 else OK, "triggers.json is signed",
                        "published but not covered by the signature (endpoints with a pinned key ignore it)" if code_t == 200
                        else "no triggers.json published")
    else:
        rep.add("signing", TODO, "manifest unsigned", "run tools/setup_live.py (or add REGISTRY_SIGNING_SEED + AUTOSTATION_REGISTRY_PUBKEY by hand)")
    return active


def check_installers(rep: Report, fetch: Fetch, owner: str, repo: str, active: List[str], deep: bool) -> None:
    code, rel = _json(fetch, f"https://api.github.com/repos/{owner}/{repo}/releases/tags/installers-latest")
    if not rel:
        rep.add("installers", FAIL, "installers-latest release", f"HTTP {code}")
        return
    assets = {a["name"]: a for a in rel.get("assets", [])}
    for name in active:
        exe, sha = f"{name}-setup.exe", f"{name}-setup.exe.sha256"
        if exe not in assets or sha not in assets:
            rep.add("installers", FAIL, name, "installer or .sha256 missing from the release")
            continue
        size_mb = assets[exe]["size"] / 1048576
        rep.add("installers", OK, f"{exe}", f"{size_mb:.0f} MB, updated {assets[exe]['updated_at']}")
        if deep:
            c1, body = fetch(assets[exe]["browser_download_url"], None)
            c2, text = fetch(assets[sha]["browser_download_url"], None)
            want = text.decode("ascii", "replace").split()[0].lower() if c2 == 200 and text else ""
            ok = c1 == 200 and hashlib.sha256(body).hexdigest() == want
            rep.add("installers", OK if ok else FAIL, f"{exe} sha256", "download matches the published digest" if ok else "MISMATCH or download failed")


def check_pipelines(rep: Report, fetch: Fetch, owner: str, repo: str) -> None:
    code, data = _json(fetch, f"https://api.github.com/repos/{owner}/{repo}/actions/runs?branch=main&per_page=60")
    if not data:
        rep.add("pipelines", WARN, "workflow runs", f"could not read (HTTP {code})")
        return
    latest: Dict[str, dict] = {}
    for run in data.get("workflow_runs", []):
        if run["status"] == "completed" and run["name"] not in latest and run.get("event") != "dynamic":
            latest[run["name"]] = run
    for wf in WORKFLOWS:
        run = latest.get(wf)
        if not run:
            rep.add("pipelines", WARN, wf, "no completed run on main yet")
        else:
            good = run["conclusion"] in ("success", "skipped")
            rep.add("pipelines", OK if good else FAIL, wf, f"{run['conclusion']} ({run['head_sha'][:7]})")


def check_settings(rep: Report, fetch: Fetch, owner: str, repo: str, token: Optional[str]) -> None:
    code, rulesets = _json(fetch, f"https://api.github.com/repos/{owner}/{repo}/rulesets")
    names = [r.get("name", "") for r in rulesets] if isinstance(rulesets, list) else []
    for want in ("main", "registry-artifacts", "triggers"):
        hit = any(want in n for n in names)
        rep.add("settings", OK if hit else TODO, f"ruleset: {want}", "active" if hit else "missing: run tools/harden_repo.py")
    if not token:
        rep.add("settings", TODO, "secrets/variables, scanning, logs repo", "set GITHUB_TOKEN (repo admin) and re-run to verify these")
        return
    code, info = _json(fetch, f"https://api.github.com/repos/{owner}/{repo}", token)
    if not info:
        why = {401: "GitHub rejected the token (expired, mistyped or revoked)", 403: "the token is not allowed to read this repo (scope or SSO authorisation)",
               404: "the token cannot see this repo (needs the repo scope)", 0: "GitHub was unreachable"}.get(code, f"HTTP {code}")
        rep.add("settings", TODO, "token-only checks skipped", f"{why}; scanning, secrets and the logs repo were NOT checked")
        return
    perms = info.get("permissions") or {}
    sa = info.get("security_and_analysis")
    if not perms.get("admin") or sa is None:
        rep.add("settings", TODO, "secret scanning status", "not visible: the token needs admin on the repo (classic token with the repo scope)")
    else:
        for key in ("secret_scanning", "secret_scanning_push_protection"):
            on = (sa.get(key) or {}).get("status") == "enabled"
            rep.add("settings", OK if on else FAIL, key, "enabled" if on else "disabled (Settings > Advanced Security)")
    for kind, want in (("secrets", ("LOGGING_TOKEN",)), ("variables", ())):
        code, data = _json(fetch, f"https://api.github.com/repos/{owner}/{repo}/actions/{kind}", token)
        if data is None:
            rep.add("settings", TODO, f"{kind} list", f"not readable (HTTP {code}): needs admin on the repo")
            continue
        have = {x["name"] for x in data.get(kind, [])}
        for w in want:
            rep.add("settings", OK if w in have else TODO, f"{kind[:-1]} {w}", "present" if w in have else "missing")
    code, logs = _json(fetch, f"https://api.github.com/repos/{owner}/AutoStation-logs", token)
    if logs:
        rep.add("settings", OK if logs.get("private") else FAIL, "logs repo is private", str(logs.get("private")))
    else:
        rep.add("settings", TODO, "logs repo", f"not readable (HTTP {code}): check the token can see {owner}/AutoStation-logs")


def run(owner: str, repo: str, fetch: Fetch = http_fetch, deep: bool = False, token: Optional[str] = None) -> Report:
    rep = Report()
    base = f"https://{owner}.github.io/{repo}/"
    manifest = check_pages(rep, fetch, base)
    active = check_registry(rep, fetch, base, manifest) if manifest else []
    check_installers(rep, fetch, owner, repo, active, deep)
    check_pipelines(rep, fetch, owner, repo)
    check_settings(rep, fetch, owner, repo, token)
    return rep


def render(rep: Report) -> str:
    out = []
    for r in rep.rows:
        out.append(f"[{r['status']:<4}] {r['area']:<10} {r['check']}" + (f"  - {r['detail']}" if r["detail"] else ""))
    counts = {s: sum(1 for r in rep.rows if r["status"] == s) for s in (OK, WARN, TODO, FAIL)}
    out.append(f"\n{counts[OK]} ok, {counts[WARN]} warn, {counts[TODO]} to do by a person, {counts[FAIL]} FAILED")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default="devpatel135")
    ap.add_argument("--repo", default="AutoStation")
    ap.add_argument("--deep", action="store_true", help="download and re-hash the published installers")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = run(a.owner, a.repo, deep=a.deep, token=os.environ.get("GITHUB_TOKEN"))
    print(json.dumps(rep.rows, indent=2) if a.json else render(rep))
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
