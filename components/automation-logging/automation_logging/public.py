"""The PUBLIC view of the telemetry: what may appear on the public GitHub Pages site and in public GitHub issues.

The repository and the site are public, the credential that writes telemetry ships inside the installer, and failures can happen
on pages that show customer data. So the public site is built ONLY from a sanitized copy of the archive (``sanitize_archive``): it
keeps counts, statuses, durations, versions, automation names and a HASHED machine id, and drops everything that is free text or
identifies a person or a machine:

* failure evidence (screenshots, DOM snapshots) is not copied at all - it stays in the private logs repository;
* run ``params`` / ``result`` / event ``data`` (apart from numbers and a few fixed-vocabulary words), tracebacks, free-text error
  messages, synopses, exit details, user hashes and halt reasons are dropped;
* host names become ``machine-<6 hex>`` (HMAC-SHA256 under ``SITE_ID_SALT``);
* an error keeps its CATEGORY, a stable SIGNATURE hash (so "the same error" is still countable) and a one-line label from a fixed
  vocabulary.

Opt-in per automation: ``catalog.public_detail: true`` in the manifest AND ``data_classification`` of ``public`` or ``internal``
re-enables a scrubbed error line and the synopsis for that automation only (never for confidential/restricted).

Salt: ``SITE_ID_SALT`` (a GitHub secret/variable) when set. Otherwise a per-repository constant is used so site and issues agree
without any setup - but that constant is public, so a guessable host name (``VDI-0204``) can be recovered by a dictionary attack.
Set ``SITE_ID_SALT`` to make the ids non-reversible; changing it re-labels every machine.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path

from .common import error_fingerprint

SALT_ENV = "SITE_ID_SALT"
KINDS = ("run", "heartbeat", "lifecycle")           # evidence is deliberately NOT here
PUBLIC_CLASSIFICATIONS = ("public", "internal")
DEFAULT_REPO = "devpatel135/AutoStation"

_SAFE_WORD = re.compile(r"^[A-Za-z0-9_.:+-]{1,48}$")
_SAFE_EVENT = re.compile(r"^[a-z][a-z0-9_]{0,60}$")
_SAFE_CATEGORY = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,40}$")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL = re.compile(r"(?i)\b(?:https?|ftp|file)://\S+")
_WIN_PATH = re.compile(r"(?i)\b[A-Z]:\\[^\s'\"<>|]*")
_UNIX_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:home|users|var|tmp|etc|opt|usr|mnt)/[^\s'\"<>|]*", re.I)
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# event-data keys whose STRING values are allowed through (fixed vocabularies); numbers and booleans are always allowed
_WORD_KEYS = frozenset({"outcome", "status", "reason", "category", "error_category", "decision", "source", "caller", "playbook", "action",
                        "phase", "template", "state", "kind", "mode", "step"})
_DROP_KEYS = frozenset({"item_id", "id", "name", "message", "url", "path", "detail", "error", "params", "result", "host", "user", "account"})

# one line per error category; anything else gets the generic line. Fixed text: no telemetry value ever reaches a public page through here.
CATEGORY_LABELS = {
    "NETWORK_ERROR": "network problem (the service could not be reached)",
    "TIMEOUT": "an operation timed out",
    "AUTH_ERROR": "authentication failed or expired",
    "SIGNIN_REQUIRED": "a person needs to sign in",
    "AUTH_EXPIRED": "a sign-in expired",
    "MANUAL_STEP_REQUIRED": "a manual step is required",
    "SYSTEM_UNAVAILABLE": "a required system was unavailable",
    "DATA_ERROR": "unexpected or invalid data",
    "SELECTOR_NOT_FOUND": "a page element could not be found",
    "DEVTOOLS_BLOCKED": "browser automation is blocked by device policy",
    "STALE_LOCK": "a leftover lock blocked the run",
    "STALE_BROWSER_PROFILE_LOCK": "a leftover browser profile lock blocked the run",
    "CORRUPT_BROWSER_PROFILE": "the browser profile was damaged",
    "MISSING_FILE": "a required file was missing",
    "COMPONENT_CORRUPT": "an installed component was damaged",
    "DISK_LOW": "the machine was low on disk space",
    "NODE_MISSING": "the Node runtime was missing",
    "EDGE_MISSING": "Microsoft Edge was missing",
    "GIT_MISSING": "git was missing",
    "PYTHON_RUNTIME_BROKEN": "the bundled Python runtime was broken",
    "DEPENDENCY_VERSION_MISMATCH": "component versions did not match",
    "TLS_FAILURE": "a secure connection could not be established",
    "DNS_FAILURE": "a name could not be resolved",
    "PROXY_FAILURE": "a proxy blocked the connection",
    "NETWORK_UNREACHABLE": "the network was unreachable",
    "CLOCK_SKEW": "the machine clock is wrong",
    "SCHEDULED_TASK_MISSING": "the scheduled task was missing",
    "SCHEDULED_TASK_DISABLED": "the scheduled task was disabled",
    "UNTRUSTED_REDIRECT": "a sign-in redirected somewhere that is not trusted",
    "SYSTEM_ERROR": "an internal error occurred",
    "LOGIC_ERROR": "the automation hit an unexpected condition",
    "UNKNOWN": "an error occurred (see the private logs for the detail)",
}
GENERIC_LABEL = "an error occurred (see the private logs for the detail)"


# ------------------------------------------------------------------------------------------------ identity
def site_salt(repo: str | None = None) -> str:
    """The salt for machine ids: SITE_ID_SALT when set, else a per-repository constant (see the module docstring for the trade-off)."""
    env = (os.environ.get(SALT_ENV) or "").strip()
    if env:
        return env
    return "autostation-public-view:" + (repo or os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO).lower()


def machine_id(host: str | None, salt: str) -> str | None:
    """``machine-<6 hex>``: stable per host and salt, not reversible without the salt (HMAC-SHA256)."""
    if not host:
        return None
    digest = hmac.new(salt.encode("utf-8"), str(host).strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()
    return "machine-" + digest[:6]


def category_label(category) -> str:
    cat = category if isinstance(category, str) and _SAFE_CATEGORY.match(category) else "UNKNOWN"
    return CATEGORY_LABELS.get(cat, GENERIC_LABEL)


def scrub_text(text, limit: int = 160) -> str:
    """For the per-automation public_detail opt-in only: remove e-mail addresses, URLs, file paths and IP addresses, one line, capped."""
    t = "" if text is None else str(text)
    t = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", t)
    for rx, repl in ((_EMAIL, "[email]"), (_URL, "[url]"), (_WIN_PATH, "[path]"), (_UNIX_PATH, "[path]"), (_IPV4, "[ip]")):
        t = rx.sub(repl, t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[: limit - 1] + "…" if len(t) > limit else t


def public_detail_allowed(entry) -> bool:
    """catalog.public_detail: true AND a data classification that is public or internal (never confidential/restricted, never unset)."""
    if not isinstance(entry, dict):
        return False
    cat = entry.get("catalog") if isinstance(entry.get("catalog"), dict) else {}
    return cat.get("public_detail") is True and str(entry.get("data_classification") or "").lower() in PUBLIC_CLASSIFICATIONS


# ------------------------------------------------------------------------------------------------ records
def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e15


def _safe_word(v) -> bool:
    return isinstance(v, str) and bool(_SAFE_WORD.match(v))


def _event(e, detail_ok: bool):
    if not isinstance(e, dict) or not isinstance(e.get("event"), str) or not _SAFE_EVENT.match(e["event"]):
        return None
    out: dict = {"event": e["event"]}
    if isinstance(e.get("ts"), str):
        out["ts"] = e["ts"]
    data = e.get("data") if isinstance(e.get("data"), dict) else {}
    keep: dict = {}
    for k, v in data.items():
        if not isinstance(k, str) or not _SAFE_EVENT.match(k) or k in _DROP_KEYS:
            continue
        if isinstance(v, bool) or _num(v):
            keep[k] = v
        elif k in _WORD_KEYS and _safe_word(v):
            keep[k] = v
    if keep:
        out["data"] = keep
    return out


def sanitize_run(rec: dict, salt: str, detail_ok: bool = False) -> dict:
    out: dict = {"schema_version": 1, "kind": "run"}
    for k in ("run_id", "automation", "ring", "slot", "status", "started_at", "ended_at", "expected_finish_at"):
        if isinstance(rec.get(k), str):
            out[k] = rec[k]
    for k in ("automation_version", "wrapper_version", "framework_version"):
        if isinstance(rec.get(k), str) and _SAFE_VERSION.match(rec[k]):
            out[k] = rec[k]
    if _num(rec.get("duration_ms")):
        out["duration_ms"] = rec["duration_ms"]
    if isinstance(rec.get("hung_detected"), bool):
        out["hung_detected"] = rec["hung_detected"]
    hid = machine_id(rec.get("host"), salt)
    if hid:
        out["host"] = hid
    items = rec.get("items")
    if isinstance(items, dict):
        out["items"] = {k: v for k, v in items.items() if isinstance(k, str) and _SAFE_EVENT.match(k) and isinstance(v, int) and not isinstance(v, bool)}
    if rec.get("synopsis_source") in ("llm", "deterministic"):
        out["synopsis_source"] = rec["synopsis_source"]
    if detail_ok and isinstance(rec.get("synopsis"), str):
        out["synopsis"] = scrub_text(rec["synopsis"], 300)
    sh = rec.get("self_heal")
    if isinstance(sh, dict):
        errs = sh.get("errors")
        out["self_heal"] = {"errors": ["self-heal reported an error"] * min(len(errs), 3)} if isinstance(errs, list) and errs else {}
    rem = rec.get("remediation")
    if isinstance(rem, dict):
        r: dict = {}
        if isinstance(rem.get("category"), str) and _SAFE_CATEGORY.match(rem["category"]):
            r["category"] = rem["category"]
        if isinstance(rem.get("fixed"), bool):
            r["fixed"] = rem["fixed"]
        if isinstance(rem.get("playbooks"), list):
            r["playbooks"] = [p for p in rem["playbooks"][:20] if _safe_word(p)]
        out["remediation"] = r
    err = rec.get("error")
    if isinstance(err, dict):
        cat = err.get("category") if isinstance(err.get("category"), str) and _SAFE_CATEGORY.match(err["category"]) else "UNKNOWN"
        raw_msg = err.get("message") if isinstance(err.get("message"), str) else ""
        e: dict = {"category": cat, "signature": error_fingerprint(cat, raw_msg),
                   "message": scrub_text(raw_msg, 160) if (detail_ok and raw_msg) else category_label(cat)}
        if _num(err.get("attempts")):
            e["attempts"] = err["attempts"]
        out["error"] = e
    events = rec.get("events")
    if isinstance(events, list):
        out["events"] = [ev for ev in (_event(x, detail_ok) for x in events[:5000]) if ev]
    return out


def sanitize_heartbeat(rec: dict, salt: str) -> dict:
    out: dict = {"schema_version": 1, "kind": "heartbeat"}
    for k in ("ts", "phase", "ring", "automation", "run_id", "expected_finish_at"):
        if isinstance(rec.get(k), str):
            out[k] = rec[k]
    out["host"] = machine_id(rec.get("host"), salt)
    v = rec.get("versions")
    if isinstance(v, dict):
        vv: dict = {}
        for k, x in v.items():
            if k == "automations" and isinstance(x, dict):
                vv["automations"] = {a: b for a, b in x.items() if isinstance(a, str) and _safe_word(a) and (b is None or (isinstance(b, str) and _SAFE_VERSION.match(b)))}
            elif isinstance(k, str) and _SAFE_WORD.match(k) and (x is None or (isinstance(x, str) and _SAFE_VERSION.match(x))):
                vv[k] = x
        out["versions"] = vv
    sp = rec.get("spool")
    if isinstance(sp, dict):
        out["spool"] = {k: sp[k] for k in ("count", "bytes", "oldest_age_s") if _num(sp.get(k))}
    if isinstance(rec.get("logging_auth_failed"), dict):
        out["logging_auth_failed"] = {"reason": "rejected"}
    return out


def sanitize_lifecycle(rec: dict, salt: str) -> dict:
    out: dict = {"schema_version": 1, "kind": "lifecycle"}
    for k in ("ts", "event", "automation"):
        if isinstance(rec.get(k), str):
            out[k] = rec[k]
    if isinstance(out.get("event"), str) and not _SAFE_EVENT.match(out["event"]):
        out["event"] = "other"
    hid = machine_id(rec.get("host"), salt)
    if hid:
        out["host"] = hid
    det = rec.get("detail")
    if isinstance(det, dict):
        out["detail"] = {k: v for k, v in det.items() if isinstance(k, str) and _SAFE_EVENT.match(k) and k not in _DROP_KEYS and (isinstance(v, bool) or _num(v))}
    return out


def sanitize_record(kind: str, rec: dict, salt: str, entry: dict | None = None) -> dict | None:
    if kind == "run":
        return sanitize_run(rec, salt, public_detail_allowed(entry))
    if kind == "heartbeat":
        return sanitize_heartbeat(rec, salt)
    if kind == "lifecycle":
        return sanitize_lifecycle(rec, salt)
    return None


# ------------------------------------------------------------------------------------------------ archive
def sanitize_archive(src, dst, manifest: dict | None, salt: str) -> dict:
    """Copy ``src`` (ingest archive: ``<kind>/<name>/YYYY/MM/DD/<id>.json``) to ``dst`` keeping only the public view. Evidence is not copied.
    The folder name of heartbeat / host-level lifecycle artefacts is the machine id, so the copy re-validates exactly like the original."""
    src, dst = Path(src), Path(dst)
    autos = (manifest or {}).get("automations") or {}
    stats = {"copied": 0, "skipped": 0, "evidence_withheld": 0, "hosts": 0}
    hosts: set = set()
    dst.mkdir(parents=True, exist_ok=True)
    if (src / "evidence").is_dir():
        stats["evidence_withheld"] = sum(1 for _ in (src / "evidence").glob("*/*/*/*/*.json"))
    for kind in KINDS:
        base = src / kind
        if not base.is_dir():
            continue
        for f in base.glob("*/*/*/*/*.json"):
            rel = f.relative_to(base).parts
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(rec, dict) or rec.get("kind") != kind:
                    raise ValueError("not a record")
                entry = autos.get(rec.get("automation")) if kind == "run" else None
                pub = sanitize_record(kind, rec, salt, entry if isinstance(entry, dict) else None)
                if pub is None:
                    raise ValueError("no public form")
                name = rel[0]
                if kind == "heartbeat" or (kind == "lifecycle" and pub.get("automation") is None):
                    name = pub.get("host") or machine_id(name, salt)
                if rec.get("host"):
                    hosts.add(str(rec["host"]))
                target = dst / kind / name / rel[1] / rel[2] / rel[3] / f.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(pub, separators=(",", ":")), encoding="utf-8")
                stats["copied"] += 1
            except Exception:  # noqa: BLE001 - an unreadable / odd record is simply not published
                stats["skipped"] += 1
    stats["hosts"] = len(hosts)
    try:  # the verdict of the last ingest is about data quality, not about content: it carries only counters
        ing = json.loads((src / "_ingest_stats.json").read_text(encoding="utf-8"))
        if isinstance(ing, dict):
            (dst / "_ingest_stats.json").write_text(json.dumps({k: v for k, v in ing.items() if isinstance(v, int) and not isinstance(v, bool)}), encoding="utf-8")
    except (OSError, ValueError):
        pass
    stats["raw_hosts"] = sorted(hosts)
    return stats


# ------------------------------------------------------------------------------------------------ leak check
_TEXT_SUFFIXES = (".html", ".json", ".js", ".css", ".txt", ".prom", ".svg", ".md", ".yaml", ".yml", ".ps1", ".xml")


def find_leaks(site_dir, raw_hosts, extra_markers=()) -> list[str]:
    """Scan a built site for what must never be public: an evidence folder, a raw host name, a traceback. Returns human-readable problems
    (the offending value is never echoed back in full). The registry/ folder (signed manifest, packages) is not telemetry and is skipped."""
    site_dir = Path(site_dir)
    problems: list[str] = []
    for p in site_dir.rglob("evidence"):
        if p.is_dir() and p.parent.name in ("dashboard", "jobs", "views") or (p.is_dir() and "evidence" == p.name and (p.parent / "index.html").exists()):
            problems.append(f"an evidence folder was published: {p.relative_to(site_dir).as_posix()}")
    hosts = [h for h in raw_hosts if isinstance(h, str) and len(h) >= 3]
    markers = [m for m in extra_markers if isinstance(m, str) and m]
    for f in site_dir.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in _TEXT_SUFFIXES or "registry" in f.relative_to(site_dir).parts[:1]:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        low = text.lower()
        for h in hosts:
            if h.lower() in low:
                problems.append(f"raw host name {h[:3]}*** found in {f.relative_to(site_dir).as_posix()}")
        for m in markers:
            if m.lower() in low:
                problems.append(f"{m[:3]}*** found in {f.relative_to(site_dir).as_posix()}")
        if "traceback (most recent call last)" in low:
            problems.append(f"a traceback was published in {f.relative_to(site_dir).as_posix()}")
    return problems
