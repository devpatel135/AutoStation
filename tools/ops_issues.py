#!/usr/bin/env python3
"""Turn fleet signals (SILENT, MISSED, HUNG, VERSION_DRIFT, ...) and canary failures into GitHub issues - and close them again.

    python tools/ops_issues.py sync    --signals report.json [--repo owner/name] [--max-new 5] [--dry-run] [--live-data-ok]
    python tools/ops_issues.py raise   --id canary-install --type CANARY --severity high --title T --detail D [--repo ...]
    python tools/ops_issues.py resolve --id canary-install [--note N] [--repo ...]

One issue per signal fingerprint (an HTML comment in the body carries it), label `ops-signal` plus `severity:<x>` and
`signal:<type>`. A recurrence comments at most once a day; a signal that disappears closes its issue; a signal that comes back
within 14 days reopens it. NOTHING is closed unless the caller says the signal computation really ran (`--live-data-ok`), so a
missing logs repo or a failed analysis can never "resolve" real problems.

Everything that came from telemetry is untrusted (endpoints push it with a credential that ships inside the installer): it is
validated, control characters are stripped, markup / @mentions / issue references are neutralised, lengths are capped.
The pure core (normalize / render / plan) has no I/O and is unit-tested; the GitHub layer is a thin urllib client that reads
GITHUB_TOKEN (issues: write) from the environment and is never given any other secret.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

LABEL = "ops-signal"
SEVERITIES = ("critical", "high", "medium", "low")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
RUN_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+$")
FP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{5,63}$")
TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MARKER_RE = re.compile(r"<!-- autostation-signal:([A-Za-z0-9][A-Za-z0-9_.-]{5,63}) -->")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
ZW = chr(0x200B)                       # zero-width space: breaks auto-linking without changing what a human reads
CLEARING_LABEL = "signal-clearing"     # set when a signal is first seen clear; the issue closes only if it is STILL clear next run
WONTFIX_LABEL = "wontfix"              # a maintainer's 'do not reopen / do not re-raise'
DIGEST_FP = "ops-digest-overflow"
MAX_OPEN_DEFAULT = 50
# only issues for signals the analytics can produce are ever closed automatically (the canary and manual issues are not)
ANALYTICS_TYPES = frozenset({"SILENT", "MISSED", "HUNG", "FAILED", "VERSION_DRIFT", "SLOW", "ITEM_FAILURES", "UNEXPECTED",
                             "ERROR_BUDGET_BURN", "FLAKY", "DURATION_ANOMALY", "REGRESSION_AFTER_RELEASE", "REMEDIATION_FAILING",
                             "PERSISTENT_MANUAL_STEP", "MACHINE_UNHEALTHY"})
MIN_RECORDS = 3                         # a report built from fewer records cannot prove "all clear"
MAX_NEWEST_AGE_H = 48.0                 # ... nor can one whose newest record is older than this
RECURRENCE_GAP = timedelta(hours=24)
REOPEN_WINDOW = timedelta(days=14)
DEFAULT_RUNBOOK = "docs/RUNBOOKS.md#3-failure-triage"
RUNBOOKS = {
    "SILENT": "docs/RUNBOOKS.md#3-failure-triage",
    "MISSED": "docs/RUNBOOKS.md#3-failure-triage",
    "HUNG": "docs/RUNBOOKS.md#3-failure-triage",
    "FAILED": "docs/RUNBOOKS.md#3-failure-triage",
    "VERSION_DRIFT": "docs/RUNBOOKS.md#2-rollback",
    "CANARY": "docs/RUNBOOKS.md#canary-failed",
}
LABEL_COLORS = {"ops-signal": "b60205", "severity:critical": "b60205", "severity:high": "d93f0b", "severity:medium": "fbca04",
                "severity:low": "0e8a16"}
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")


# --------------------------------------------------------------------------------------------- pure core
def sanitize(value: Any, limit: int = 300, markup: bool = True) -> str:
    """Make untrusted text safe to put in an issue: no control/bidi chars, no HTML, no @mentions, no #refs, one line, capped.
    ``markup=False`` is for plain-text fields (titles): the markdown/HTML escaping is skipped, everything else still applies."""
    text = "" if value is None else str(value)
    text = _CTRL.sub(" ", text.replace("\r", " ").replace("\n", " ").replace("\t", " "))
    if markup:
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = re.sub(r"([`*_~\[\]|\\])", r"\\\1", text)
    text = re.sub(r"@(?=[A-Za-z0-9_-])", "@​", text)           # no pings
    text = re.sub(r"(?<![A-Za-z0-9])#(?=\d)", "#​", text)       # no cross-references
    text = re.sub(r"#(?=\d)", "#" + ZW, text)                                  # any #123, whatever precedes it (owner/repo#5)
    text = re.sub(r"(?i)\b(GH)-(?=\d)", lambda m: m.group(1) + ZW + "-", text)  # GH-123 autolinks
    text = re.sub(r"(?i)\b(www)\.", lambda m: m.group(1) + ZW + ".", text)       # www.evil.example becomes a link otherwise
    text = re.sub(r"(?i)\b(https?|ftp|file|javascript|data):(//)?", lambda m: m.group(1) + "​:" + (m.group(2) or ""), text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def normalize_signal(raw: Any) -> Optional[Dict[str, Any]]:
    """Validate one signal dict from the analytics report. Anything malformed is dropped (None), never repaired."""
    if not isinstance(raw, dict):
        return None
    fp, typ = raw.get("id"), raw.get("type")
    if not isinstance(fp, str) or not FP_RE.match(fp) or not isinstance(typ, str) or not TYPE_RE.match(typ):
        return None
    sev = str(raw.get("severity") or "").lower()
    if sev not in SEV_RANK:
        sev = "medium"
    auto = raw.get("automation")
    auto = auto if isinstance(auto, str) and NAME_RE.match(auto) else None
    ev = raw.get("evidence")
    evidence = [sanitize(e, 160) for e in (ev if isinstance(ev, list) else [])[:10]]
    diag = raw.get("diagnosis")
    return {"id": fp, "type": typ, "severity": sev, "automation": auto,
            "host": (raw["host"] if isinstance(raw.get("host"), str) and HOST_RE.match(raw["host"]) else None),
            "registered": raw.get("registered") is not False, "ring": sanitize(raw.get("ring"), 20) or None,
            "detail": sanitize(raw.get("detail"), 400), "first_seen": sanitize(raw.get("first_seen"), 40),
            "last_seen": sanitize(raw.get("last_seen"), 40), "evidence": [e for e in evidence if e],
            "diagnosis": sanitize(diag.get("summary") if isinstance(diag, dict) else diag, 600) if diag else "",
            "owner": sanitize(raw.get("owner") if not isinstance(raw.get("owner"), list) else ", ".join(map(str, raw["owner"][:5])), 120)}


def normalize_signals(raw: Any) -> List[Dict[str, Any]]:
    out, seen = [], set()
    for item in (raw if isinstance(raw, list) else []):
        sig = normalize_signal(item)
        if sig and sig["id"] not in seen:
            seen.add(sig["id"])
            out.append(sig)
    return out


def fingerprint_of(body: Any) -> Optional[str]:
    m = MARKER_RE.search(body if isinstance(body, str) else "")
    return m.group(1) if m else None


def render_title(sig: Dict[str, Any]) -> str:
    who = sig.get("automation") or sig.get("host") or "fleet"
    return f"[{sig['type']}] {sanitize(who, 60, markup=False)}" + (f" on {sanitize(sig['host'], 40, markup=False)}" if sig.get("automation") and sig.get("host") else "")


def render_body(sig: Dict[str, Any], repo: str = "", site_url: str = "") -> str:
    runbook = RUNBOOKS.get(sig["type"], DEFAULT_RUNBOOK)
    base = f"https://github.com/{repo}/blob/main/" if repo else ""
    lines = [f"<!-- autostation-signal:{sig['id']} -->",
             f"**{sig['type']}** ({sig['severity']}) - opened automatically by the health workflow. Closes itself when the signal clears.",
             "", "| | |", "|---|---|",
             f"| Automation | {sig.get('automation') or '-'} |", f"| Host | {sig.get('host') or '-'} |",
             f"| Ring | {sig.get('ring') or '-'} |", f"| First seen | {sig.get('first_seen') or '-'} |",
             f"| Last seen | {sig.get('last_seen') or '-'} |", f"| Owner | {sig.get('owner') or '-'} |", "",
             "### What the platform saw", sig.get("detail") or "(no detail)"]
    if sig.get("diagnosis"):
        lines += ["", "### Diagnosis (model output over unauthenticated telemetry - verify before acting)", sig["diagnosis"]]
    if sig.get("evidence"):
        lines += ["", "### Evidence"] + [f"- `{e}`" for e in sig["evidence"]]
    if sig.get("run_url") and RUN_URL_RE.match(sig["run_url"]):
        lines += ["", f"Failing run: {sig['run_url']}"]
    lines += ["", "### What to do", f"Follow the runbook: [{runbook.split('#')[0]}]({base}{runbook})"]
    if site_url:
        lines.append(f"Dashboards: {site_url.rstrip('/')}/jobs/ and {site_url.rstrip('/')}/machines/")
    lines += ["", "<sub>Telemetry is advisory: it is pushed by installed machines with a credential that ships inside the installer, so treat every "
              "value above as untrusted until you confirm it on the machine.</sub>"]
    return "\n".join(lines)


def labels_for(sig: Dict[str, Any]) -> List[str]:
    return [LABEL, f"severity:{sig['severity']}", f"signal:{sig['type'].lower()}"]


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _label_names(iss: Dict[str, Any]) -> List[str]:
    return [(l.get("name") if isinstance(l, dict) else str(l)) for l in (iss.get("labels") or [])]


def signal_type_of(iss: Dict[str, Any]) -> Optional[str]:
    """The signal type an issue was raised for (from its `signal:<type>` label), upper-cased; None for unlabelled issues."""
    for n in _label_names(iss):
        if isinstance(n, str) and n.startswith("signal:"):
            return n[len("signal:"):].upper()
    return None


def report_trust(doc: Any) -> Tuple[bool, str]:
    """Is this analytics report good enough to let issues CLOSE on? An empty / stale / damaged data set must never read as 'all clear'."""
    if not isinstance(doc, dict) or not isinstance(doc.get("signals"), list):
        return False, "the report has no signals list"
    dq = doc.get("data_quality")
    if not isinstance(dq, dict):
        return False, "the report carries no data_quality section"
    if dq.get("signal_errors"):
        errs = dq["signal_errors"]
        return False, "a signal builder failed (" + sanitize(errs[0] if isinstance(errs, list) and errs else errs, 80) + ")"
    if dq.get("extra_signals_error"):
        return False, "the observability signals failed to compute"
    loaded = dq.get("records_loaded")
    if not isinstance(loaded, int) or isinstance(loaded, bool) or loaded < MIN_RECORDS:
        return False, f"only {loaded} record(s) were loaded (need at least {MIN_RECORDS})"
    skipped = dq.get("skipped_malformed") or 0
    if not isinstance(skipped, int) or skipped > max(10, loaded // 5):
        return False, f"{skipped} records were rejected as malformed"
    age = dq.get("newest_record_age_h")
    if not isinstance(age, (int, float)) or isinstance(age, bool) or age > MAX_NEWEST_AGE_H:
        return False, f"the newest record is {age} h old (limit {MAX_NEWEST_AGE_H:.0f} h)"
    return True, "ok"


def plan(signals: List[Dict[str, Any]], issues: Iterable[Dict[str, Any]], now: datetime, max_new: int = 5, may_close: bool = False,
         min_severity: str = "low", max_open: int = MAX_OPEN_DEFAULT, closable_types: Optional[frozenset] = ANALYTICS_TYPES) -> List[Dict[str, Any]]:
    """Desired-state diff. ``issues`` are GitHub issue dicts (number, state, body, labels, updated_at, closed_at). Returns an ordered list of
    {"op": "create"|"comment"|"reopen"|"close"|"mark_clearing"|"unmark_clearing"|"digest"|"note", ...}.

    * Duplicates for one fingerprint: the lowest number wins, the rest are closed.
    * Closing is two-step: the first run that finds a signal gone only labels the issue ``signal-clearing``; the next run that still
      finds it gone closes it (one glitch in the data must not close real problems). A signal that returns removes the label.
    * Only issues whose ``signal:<type>`` is one the analytics produces are closed (``closable_types``; None = any, used by `resolve`).
    * Signals for automations the manifest does not know (``registered`` False) never open issues; a global ``max_open`` cap rolls the
      overflow into ONE digest issue; a ``wontfix`` label blocks reopening or re-raising.
    """
    by_fp: Dict[str, List[Dict[str, Any]]] = {}
    for iss in list(issues):
        fp = fingerprint_of(iss.get("body"))
        if fp:
            by_fp.setdefault(fp, []).append(iss)
    actions: List[Dict[str, Any]] = []
    wanted = {s["id"]: s for s in signals}
    new_candidates: List[Dict[str, Any]] = []
    open_count = sum(1 for fp, grp in by_fp.items() if fp != DIGEST_FP for i in grp if i.get("state") == "open")
    for fp, sig in wanted.items():
        group = sorted(by_fp.get(fp, []), key=lambda i: i.get("number", 0))
        opened = [i for i in group if i.get("state") == "open"]
        if opened:
            keep = opened[0]
            for dup in opened[1:]:
                actions.append({"op": "close", "number": dup["number"], "fp": fp, "note": f"Duplicate of #{keep['number']}.", "reason": "not_planned"})
            if CLEARING_LABEL in _label_names(keep):
                actions.append({"op": "unmark_clearing", "number": keep["number"], "fp": fp})
            last = _parse_ts(keep.get("updated_at"))
            seen = _parse_ts(sig.get("last_seen"))
            if last is not None and seen is not None and seen > last and now - last >= RECURRENCE_GAP:
                actions.append({"op": "comment", "number": keep["number"], "fp": fp, "sig": sig})
            continue
        if any(WONTFIX_LABEL in _label_names(i) for i in group):
            continue                                        # a maintainer said wontfix: never reopen, never re-raise
        recent = [i for i in group if (_parse_ts(i.get("closed_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= now - REOPEN_WINDOW]
        if recent:
            actions.append({"op": "reopen", "number": recent[-1]["number"], "fp": fp, "sig": sig})
        elif sig.get("registered", True):
            new_candidates.append(sig)
    floor = SEV_RANK.get(min_severity, SEV_RANK["low"])
    new_candidates = [c for c in new_candidates if SEV_RANK.get(c["severity"], 9) <= floor]   # below the floor: dashboards only
    new_candidates.sort(key=lambda s: (SEV_RANK.get(s["severity"], 9), s["id"]))
    room = max(0, int(max_open) - open_count)
    batch = min(max(0, int(max_new)), room)
    for sig in new_candidates[:batch]:
        actions.append({"op": "create", "fp": sig["id"], "sig": sig})
    skipped = new_candidates[batch:]
    if skipped and room < int(max_new):                     # the open-issue cap (not just the per-run rate limit) is what holds them back
        digest_issue = next((i for i in sorted(by_fp.get(DIGEST_FP, []), key=lambda i: i.get("number", 0)) if i.get("state") == "open"), None)
        actions.append({"op": "digest", "number": digest_issue["number"] if digest_issue else None, "fp": DIGEST_FP,
                        "sigs": skipped[:30], "total": len(skipped)})
    elif skipped:
        actions.append({"op": "note", "text": f"{len(skipped)} new signal(s) deferred to the next run (max {max_new} new issues per run)"})
    if not (skipped and room < int(max_new)):
        for i in by_fp.get(DIGEST_FP, []):
            if i.get("state") == "open":
                actions.append({"op": "close", "number": i["number"], "fp": DIGEST_FP, "note": "No more signals are waiting for an issue.",
                                "reason": "completed"})
    if may_close:
        for fp, group in by_fp.items():
            if fp in wanted or fp == DIGEST_FP:
                continue
            for iss in group:
                if iss.get("state") != "open":
                    continue
                if closable_types is not None and signal_type_of(iss) not in closable_types:
                    continue                                # canary / manual issues are resolved by their own workflow, never here
                if CLEARING_LABEL in _label_names(iss):
                    actions.append({"op": "close", "number": iss["number"], "fp": fp, "reason": "completed",
                                    "note": "The signal has cleared (two consecutive clean checks) - closing automatically."})
                else:
                    actions.append({"op": "mark_clearing", "number": iss["number"], "fp": fp})
    return actions


def actions_of(actions: List[Dict[str, Any]], op: str) -> List[Dict[str, Any]]:
    return [a for a in actions if a.get("op") == op]


def manual_signal(fp: str, typ: str, severity: str, title: str, detail: str, automation: Optional[str] = None, run_url: str = "") -> Dict[str, Any]:
    """A signal that did not come from analytics (e.g. the canary)."""
    sig = normalize_signal({"id": fp, "type": typ, "severity": severity, "automation": automation, "detail": detail,
                            "last_seen": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    if sig is None:
        raise ValueError("invalid signal id/type")
    sig["title"] = sanitize(title, 120, markup=False)
    if run_url:
        if not RUN_URL_RE.match(run_url):
            raise ValueError("run url must be https://github.com/<owner>/<repo>/actions/runs/<id>")
        sig["run_url"] = run_url          # the one link we render verbatim: validated against a strict pattern
    return sig


# --------------------------------------------------------------------------------------------- GitHub layer
class GitHub:
    def __init__(self, repo: str, token: str, opener: Callable = urllib.request.urlopen, api: str = "https://api.github.com"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
            raise ValueError("repo must be owner/name")
        self.repo, self._token, self._open, self.api = repo, token, opener, api

    def request(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.api + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "autostation-ops-issues", "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=30) as r:  # noqa: S310
                raw = r.read().decode("utf-8") or "null"
                return r.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8") or "null")
            except ValueError:
                detail = None
            return exc.code, detail

    def ensure_labels(self, names: Iterable[str]) -> None:
        for name in sorted(set(names)):
            color = LABEL_COLORS.get(name, "ededed" if not name.startswith("signal:") else "5319e7")
            self.request("POST", f"/repos/{self.repo}/labels", {"name": name, "color": color})   # 422 = exists: fine

    def list_ops_issues(self, max_pages: int = 100) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            code, data = self.request("GET", f"/repos/{self.repo}/issues?labels={urllib.parse.quote(LABEL)}&state=all&per_page=100&page={page}")
            if code != 200 or not isinstance(data, list):
                raise RuntimeError(f"listing issues failed (HTTP {code})")
            out += [i for i in data if "pull_request" not in i]
            if len(data) < 100:
                return out
        raise RuntimeError(f"more than {max_pages * 100} ops-signal issues: refusing to act on a partial listing")

    def create(self, sig: Dict[str, Any], site_url: str = "") -> int:
        code, data = self.request("POST", f"/repos/{self.repo}/issues", {
            "title": sig.get("title") or render_title(sig), "body": render_body(sig, self.repo, site_url), "labels": labels_for(sig)})
        if code != 201:
            raise RuntimeError(f"create failed (HTTP {code})")
        return int(data["number"])

    def comment(self, number: int, text: str) -> None:
        code, _ = self.request("POST", f"/repos/{self.repo}/issues/{int(number)}/comments", {"body": text})
        if code != 201:
            raise RuntimeError(f"comment failed (HTTP {code})")

    def add_label(self, number: int, name: str) -> None:
        self.ensure_labels([name])
        code, _ = self.request("POST", f"/repos/{self.repo}/issues/{int(number)}/labels", {"labels": [name]})
        if code != 200:
            raise RuntimeError(f"label failed (HTTP {code})")

    def remove_label(self, number: int, name: str) -> None:
        code, _ = self.request("DELETE", f"/repos/{self.repo}/issues/{int(number)}/labels/{urllib.parse.quote(name)}")
        if code not in (200, 404):
            raise RuntimeError(f"label removal failed (HTTP {code})")

    def update_body(self, number: int, body: str) -> None:
        code, _ = self.request("PATCH", f"/repos/{self.repo}/issues/{int(number)}", {"body": body})
        if code != 200:
            raise RuntimeError(f"update failed (HTTP {code})")

    def set_state(self, number: int, state: str, reason: str = "completed") -> None:
        body = {"state": state}
        if state == "closed":
            body["state_reason"] = reason
        code, _ = self.request("PATCH", f"/repos/{self.repo}/issues/{int(number)}", body)
        if code != 200:
            raise RuntimeError(f"state change failed (HTTP {code})")


def digest_signal(sigs: List[Dict[str, Any]], total: int) -> Dict[str, Any]:
    """ONE issue that stands in for every signal that could not get its own issue because the open-issue cap was reached."""
    lines = [f"- [{x['severity']}] {x['type']} {sanitize(x.get('automation') or x.get('host') or 'fleet', 60)}" for x in sigs]
    more = f" (showing {len(sigs)} of {total})" if total > len(sigs) else ""
    return {"id": DIGEST_FP, "type": "DIGEST", "severity": "high", "automation": None, "host": None, "ring": None, "registered": True,
            "detail": f"{total} further signal(s) are open but the cap on open ops-signal issues was reached{more}: " + " ".join(lines)[:380],
            "first_seen": "", "last_seen": "", "evidence": [], "diagnosis": "", "owner": "",
            "title": f"[DIGEST] {total} more ops signals than the open-issue cap"}


def apply(gh: GitHub, actions: List[Dict[str, Any]], site_url: str = "", dry_run: bool = False) -> Dict[str, int]:
    done = {"create": 0, "comment": 0, "reopen": 0, "close": 0, "mark_clearing": 0, "unmark_clearing": 0, "digest": 0, "errors": 0}
    for a in actions:
        op = a["op"]
        if op == "note":
            print(a["text"])
            continue
        print(f"{'(dry-run) ' if dry_run else ''}{op} {a.get('fp', '')} {('#' + str(a['number'])) if 'number' in a else ''}")
        if dry_run:
            done[op] += 1
            continue
        try:
            if op == "create":
                gh.ensure_labels(labels_for(a["sig"]))
                gh.create(a["sig"], site_url)
            elif op == "comment":
                gh.comment(a["number"], f"Still happening (last seen {a['sig'].get('last_seen') or 'now'}).\n\n{a['sig'].get('detail') or ''}")
            elif op == "reopen":
                gh.set_state(a["number"], "open")
                gh.comment(a["number"], "The signal is back - reopening.")
            elif op == "close":
                gh.set_state(a["number"], "closed", a.get("reason", "completed"))   # state first: a failing comment must not repeat every run
                gh.comment(a["number"], a.get("note") or "Closing.")
            elif op == "mark_clearing":
                gh.add_label(a["number"], CLEARING_LABEL)
            elif op == "unmark_clearing":
                gh.remove_label(a["number"], CLEARING_LABEL)
            elif op == "digest":
                digest = digest_signal(a["sigs"], a["total"])
                if a.get("number"):
                    gh.update_body(a["number"], render_body(digest, gh.repo, site_url))
                else:
                    gh.ensure_labels(labels_for(digest))
                    gh.create(digest, site_url)
            done[op] += 1
        except Exception as exc:  # noqa: BLE001  (one failing issue must not stop the rest)
            done["errors"] += 1
            print(f"::warning::{op} {a.get('fp', '')} failed: {exc}")
    return done


# --------------------------------------------------------------------------------------------- CLI
def _github(args) -> GitHub:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN is not set")
    return GitHub(repo, token)


def _site_url(repo: str) -> str:
    owner, _, name = repo.partition("/")
    return f"https://{owner}.github.io/{name}" if owner and name else ""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("sync", "raise", "resolve"):
        sp = sub.add_parser(name)
        sp.add_argument("--repo", default="")
        sp.add_argument("--dry-run", action="store_true")
    s = sub.choices["sync"]
    s.add_argument("--signals", required=True)
    s.add_argument("--max-new", type=int, default=5)
    s.add_argument("--min-severity", choices=SEVERITIES, default="low", help="only open issues at or above this severity")
    s.add_argument("--live-data-ok", action="store_true", help="the signal computation really ran on live data: allows closing issues")
    s.add_argument("--max-open", type=int, default=MAX_OPEN_DEFAULT, help="cap on open ops-signal issues; the overflow goes into one digest issue")
    r = sub.choices["raise"]
    for a in ("--id", "--type", "--severity", "--title", "--detail"):
        r.add_argument(a, required=True)
    r.add_argument("--automation", default=None)
    r.add_argument("--run-url", default="")
    d = sub.choices["resolve"]
    d.add_argument("--id", required=True)
    d.add_argument("--note", default="Resolved - the check passes again.")
    args = ap.parse_args(argv)
    gh = _github(args) if not args.dry_run or os.environ.get("GITHUB_TOKEN") else None
    if gh is None:
        raise SystemExit("dry-run still needs GITHUB_TOKEN to read the existing issues")
    now = datetime.now(timezone.utc)
    site = _site_url(gh.repo)
    issues = gh.list_ops_issues()
    if args.cmd == "sync":
        doc = json.loads(Path(args.signals).read_text(encoding="utf-8"))
        trusted, why = report_trust(doc)
        may_close = bool(args.live_data_ok and trusted)
        if args.live_data_ok and not trusted:
            print(f"::warning::not closing any issue: this report cannot prove an all-clear ({why})")
        actions = plan(normalize_signals(doc.get("signals") if isinstance(doc, dict) else doc), issues, now, args.max_new, may_close,
                       args.min_severity, args.max_open)
    elif args.cmd == "raise":
        sig = manual_signal(args.id, args.type, args.severity, args.title, args.detail, args.automation, args.run_url)
        actions = plan([sig], issues, now, 1, False)
        for a in actions:
            if a["op"] == "comment":
                a["sig"]["last_seen"] = sig["last_seen"]
    else:
        actions = plan([], [i for i in issues if fingerprint_of(i.get("body")) == args.id], now, 0, True, closable_types=None)
        actions = [a for a in actions if a["op"] != "mark_clearing"] + [
            {"op": "close", "number": a["number"], "fp": a["fp"], "reason": "completed", "note": "Resolved."}
            for a in actions if a["op"] == "mark_clearing"]
        for a in actions:
            if a["op"] == "close":
                a["note"] = sanitize(args.note, 300)
    res = apply(gh, actions, site, args.dry_run)
    print(json.dumps(res))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### ops-signal issues ({args.cmd})\n\n" + ", ".join(f"{k}: {v}" for k, v in res.items()) + "\n")
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())

# health proof 01:06:19
