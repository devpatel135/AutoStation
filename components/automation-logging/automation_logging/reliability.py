"""Reliability, error and remediation analytics (the Orchestrator-grade observability layer).

Everything here is a pure function of archive runs (already validated by ``validate.py``) plus an injectable ``now``:
no I/O, no clock, no network. The site pages, the metrics export and the extra compliance signals all build on it, so a number is
computed once and shown the same everywhere.

Definitions (hand-checkable, see tests/test_reliability.py):

* counted run      a run that is not ``halted_paused`` / ``skipped_maintenance`` and not an ``adhoc`` / ``dev`` slot.
* failure          ``failed``, ``self_heal_failed`` or ``hung``.  ``success`` and ``partial`` count as ok.
* error budget     ``(1 - SLO) * runs`` failures are allowed in the window; ``consumed = failures / allowed``.
* burn rate        ``(failures / runs in the window) / (1 - SLO)``  (1.0 = spending the budget exactly on schedule).
* failure episode  starts at the first failed run after an ok run (or at the first run) and ends at the next ok run.
                   MTTR = mean(recovery run start - episode start); MTBF = mean(next episode start - previous recovery start).
                   Episodes are tracked per (automation, machine) so interleaved machines do not hide each other.
* flakiness        flips (ok<->failed transitions) / (runs - 1) over the last ``flaky_window`` counted runs.
* duration anomaly modified z-score ``0.6745 * (x - median) / MAD`` of a successful run against the previous ``baseline_n``
                   successful runs (MAD floored at 5% of the median so a perfectly steady baseline cannot divide by zero).
* error signature  stable id of "the same error": paths, uuids, hex, urls, ips and digits are normalised away.

Telemetry is attacker-authored (public installer credential): every string that leaves this module is clamped and stripped of
control characters; the HTML layers still escape it.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .common import SEVERITY_ORDER, fingerprint, iso, parse_iso, percentile

UNCOUNTED = ("halted_paused", "skipped_maintenance")
FAIL = ("failed", "self_heal_failed", "hung")
OK = ("success", "partial")
SKIP_SLOTS = ("adhoc", "dev")
MANUAL_EVENTS = ("manual_step_required", "untrusted_redirect")
MANUAL_STATES = ("manual_step_required", "untrusted_redirect")
REMEDIATION_OUTCOMES = ("fixed", "not_fixed", "skipped", "escalated", "recommended")

DEFAULTS = {
    "slo_target": 0.95,          # share of counted runs that must be ok
    "slo_window_days": 30,
    "fast_burn": 10.0,           # burn rate over the last 24 h that raises ERROR_BUDGET_BURN (high)
    "slow_burn": 2.0,            # burn rate over the last 7 d (with < 50 % budget left) that raises it (medium)
    "burn_min_runs": 3,          # never alert on fewer runs than this in the window
    "flaky_window": 20,
    "flaky_min_runs": 6,
    "flaky_flips": 4,
    "flaky_score": 0.4,
    "baseline_n": 20,
    "baseline_min": 8,
    "anomaly_z": 3.5,
    "anomaly_ratio": 1.5,
    "manual_step_h": 24,         # PERSISTENT_MANUAL_STEP after this many hours without a later success
    "machine_min_failures": 3,
    "machine_fail_rate": 0.6,
    "regression_window_h": 48,   # a new error signature this soon after a new version appears is a regression
    "remediation_min_attempts": 3,
}

KNOWN_CATEGORIES = ("NETWORK_ERROR", "AUTH_EXPIRED", "SYSTEM_UNAVAILABLE", "DATA_ERROR", "LOGIC_ERROR", "UNKNOWN")

CATEGORY_RUNBOOK = {
    "NETWORK_ERROR": "Check the target system and the VPN/proxy, then re-run (registry trigger-add).",
    "AUTH_EXPIRED": "Refresh the certificate / browser session / credential, then re-run.",
    "SYSTEM_UNAVAILABLE": "Check the target system's status and the VPN; retry once it is back.",
    "DATA_ERROR": "Look at the input data or an upstream change; fix the data, then re-run.",
    "LOGIC_ERROR": "Code defect: fix it and release a new version (a retry will not help).",
    "UNKNOWN": "Read the run's stderr.log and failure evidence; classify it so a playbook can handle it next time.",
}

_CTRL_RANGES = [(0, 31), (127, 159), (0x200B, 0x200F), (0x2028, 0x202E), (0x2066, 0x2069), (0xFEFF, 0xFEFF)]  # C0/C1 (newlines too), zero-width, bidi
_CTRL = re.compile("[" + "".join(chr(a) + "-" + chr(b) for a, b in _CTRL_RANGES) + "]")


# ------------------------------------------------------------------------------------------------------------------ basics
def clean(v, n: int = 200) -> str:
    """Printable, clamped string from any telemetry value ('' for containers)."""
    if v is None or isinstance(v, (dict, list, tuple, set)):
        return ""
    return _CTRL.sub("", str(v))[:n]


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e15 else None


def norm_category(v) -> str:
    """Error categories are a closed set (wrapper_core.retry.Category). Anything else collapses to UNKNOWN, so forged telemetry can neither
    inject text nor inflate the number of distinct categories (metric cardinality)."""
    c = clean(v, 40).upper()
    return c if c in KNOWN_CATEGORIES else "UNKNOWN"


def _t(r: dict) -> datetime:
    return parse_iso(r["started_at"])


def is_fail(r: dict) -> bool:
    return r.get("status") in FAIL


def series(runs) -> list[dict]:
    """Counted runs, oldest first."""
    return sorted((r for r in runs if r.get("status") not in UNCOUNTED and r.get("slot") not in SKIP_SLOTS), key=_t)   # by instant, not by string


def _events(run: dict, name: str):
    return [e["data"] if isinstance(e.get("data"), dict) else {} for e in (run.get("events") or []) if isinstance(e, dict) and e.get("event") == name]


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _round(x, nd=4):
    return None if x is None else round(x, nd)


def slo_for(entry: dict | None, cfg: dict) -> dict:
    """SLO of one automation: the manifest entry's ``slo: {target, window_days}`` overrides the fleet defaults (bounds-checked)."""
    entry = entry or {}
    cat = entry.get("catalog") if isinstance(entry.get("catalog"), dict) else {}
    s = entry.get("slo") if isinstance(entry.get("slo"), dict) else (cat.get("slo") if isinstance(cat.get("slo"), dict) else {})
    target = _num(s.get("target"))
    if target is not None and target > 1:
        target = target / 100.0
    if target is None or not (0.5 <= target < 1.0):
        target = float(cfg.get("slo_target", DEFAULTS["slo_target"]))
    days = _num(s.get("window_days"))
    if days is None or not (1 <= days <= 365):
        days = float(cfg.get("slo_window_days", DEFAULTS["slo_window_days"]))
    return {"target": round(min(max(target, 0.5), 0.99999), 5), "window_days": int(days)}


# ------------------------------------------------------------------------------------------------------- budget / burn
def error_budget(runs, now: datetime, target: float, window_days: int) -> dict:
    start = now - timedelta(days=window_days)
    rs = [r for r in series(runs) if _t(r) >= start]
    n = len(rs)
    failed = sum(1 for r in rs if is_fail(r))
    allowed = (1.0 - target) * n
    consumed = (failed / allowed) if allowed > 0 else None
    return {"target": target, "window_days": window_days, "runs": n, "failed": failed,
            "success_rate": _round((n - failed) / n) if n else None, "allowed_failures": _round(allowed),
            "consumed": _round(consumed), "remaining": _round(1.0 - consumed) if consumed is not None else None}


def burn_rate(runs, now: datetime, hours: float, target: float, min_runs: int = 3):
    start = now - timedelta(hours=hours)
    rs = [r for r in series(runs) if _t(r) >= start]
    if len(rs) < min_runs:
        return None
    rate = sum(1 for r in rs if is_fail(r)) / len(rs)
    return _round(rate / (1.0 - target))


# ------------------------------------------------------------------------------------------------- episodes / MTTR / MTBF
def episodes(runs) -> list[dict]:
    """Failure episodes per (automation, host). ``end`` is the recovery run's start (None while still failing)."""
    groups: dict = defaultdict(list)
    for r in series(runs):
        groups[(r.get("automation"), r.get("host"))].append(r)
    out = []
    for (auto, host), rs in groups.items():
        cur = None
        for r in rs:
            if is_fail(r):
                if cur is None:
                    cur = {"automation": auto, "host": host, "start": _t(r), "end": None, "runs": 0, "run_ids": []}
                cur["runs"] += 1
                if len(cur["run_ids"]) < 5:
                    cur["run_ids"].append(r["run_id"])
            elif cur is not None:
                cur["end"] = _t(r)
                out.append(cur)
                cur = None
        if cur is not None:
            out.append(cur)
    return sorted(out, key=lambda e: e["start"])


def mttr_mtbf(eps: list[dict]) -> dict:
    """MTTR / MTBF in seconds (None when there is nothing to average)."""
    rec = [(e["end"] - e["start"]).total_seconds() for e in eps if e["end"] is not None]
    by: dict = defaultdict(list)
    for e in eps:
        by[(e["automation"], e["host"])].append(e)
    gaps = []
    for lst in by.values():
        lst.sort(key=lambda e: e["start"])
        for a, b in zip(lst, lst[1:]):
            if a["end"] is not None:
                gaps.append((b["start"] - a["end"]).total_seconds())
    return {"episodes": len(eps), "open": sum(1 for e in eps if e["end"] is None), "mttr_s": _round(sum(rec) / len(rec), 1) if rec else None,
            "mtbf_s": _round(sum(gaps) / len(gaps), 1) if gaps else None}


def streaks(runs) -> dict:
    cur = best = 0
    for r in series(runs):
        cur = cur + 1 if is_fail(r) else 0
        best = max(best, cur)
    return {"current_failures": cur, "max_failures": best}


def flakiness(runs, window: int = 20, min_runs: int = 6, flips_needed: int = 4, score_needed: float = 0.4) -> dict:
    rs = series(runs)[-window:]
    flags = [is_fail(r) for r in rs]
    flips = sum(1 for a, b in zip(flags, flags[1:]) if a != b)
    score = flips / (len(flags) - 1) if len(flags) >= 2 else None
    flaky = bool(len(flags) >= min_runs and flips >= flips_needed and score is not None and score >= score_needed)
    return {"runs": len(flags), "flips": flips, "score": _round(score), "flaky": flaky}


# ----------------------------------------------------------------------------------------------------- duration analysis
def duration_anomalies(runs, baseline_n: int = 20, baseline_min: int = 8, z_limit: float = 3.5, ratio: float = 1.5) -> list[dict]:
    ok = [r for r in series(runs) if r.get("status") in OK and _num(r.get("duration_ms")) is not None and r["duration_ms"] > 0]
    out = []
    for i, r in enumerate(ok):
        base = [x["duration_ms"] for x in ok[max(0, i - baseline_n):i]]
        if len(base) < baseline_min:
            continue
        med = _median(base)
        mad = _median([abs(x - med) for x in base])
        scale = max(mad, 0.05 * med, 1.0)
        z = 0.6745 * (r["duration_ms"] - med) / scale
        slow = z >= z_limit and r["duration_ms"] / med >= ratio
        fast = z <= -z_limit and r["duration_ms"] / med <= 1.0 / ratio / 1.5
        if slow or fast:
            out.append({"run_id": r["run_id"], "started_at": r["started_at"], "duration_ms": r["duration_ms"], "baseline_ms": round(med),
                        "z": round(z, 2), "direction": "slow" if slow else "fast", "host": r.get("host")})
    return out


def duration_trend(runs, now: datetime, days: int = 14) -> list[dict]:
    rows = {}
    for i in range(days - 1, -1, -1):
        d = (now - timedelta(days=i)).date().isoformat()
        rows[d] = []
    for r in series(runs):
        d = _t(r).date().isoformat()
        if d in rows and r.get("status") in OK and _num(r.get("duration_ms")) is not None:
            rows[d].append(r["duration_ms"])
    return [{"date": d, "n": len(v), "p50": percentile(v, 50), "p95": percentile(v, 95), "p99": percentile(v, 99)} for d, v in rows.items()]


def on_time(expected: list, runs, grace: timedelta, now: datetime) -> dict:
    """``expected`` = [(slot_id, utc datetime)] from analytics.expected_runs; poll slots are exempt upstream.
    Indexed (bisect over the time-sorted runs, next-expected per slot precomputed): O(slots log runs), not O(slots x runs) - the
    archive is attacker-influenced, so this must not be quadratic."""
    from bisect import bisect_left
    exp = sorted(expected, key=lambda x: x[1])
    slot_ids = {s for s, _ in exp}
    res = {"expected": 0, "on_time": 0, "late": 0, "missed": 0}
    timed = sorted(((_t(r), r) for r in runs), key=lambda x: x[0])
    times = [t for t, _ in timed]
    next_of: list = [None] * len(exp)
    last_by_slot: dict = {}
    for i in range(len(exp) - 1, -1, -1):
        next_of[i] = last_by_slot.get(exp[i][0])
        last_by_slot[exp[i][0]] = exp[i][1]
    for i, (slot_id, e) in enumerate(exp):
        if now < e + grace:
            continue
        nxt = next_of[i] or e + timedelta(days=366)
        end = min(nxt, e + max(grace * 4, timedelta(hours=6)))
        first = None
        for j in range(bisect_left(times, e - timedelta(minutes=10)), len(times)):
            if times[j] >= end:
                break
            r = timed[j][1]
            if r.get("slot") == slot_id or r.get("slot") not in slot_ids:
                first = times[j]
                break
        res["expected"] += 1
        if first is None:
            res["missed"] += 1
        elif first <= e + grace:
            res["on_time"] += 1
        else:
            res["late"] += 1
    res["rate"] = _round(res["on_time"] / res["expected"]) if res["expected"] else None
    return res


# ---------------------------------------------------------------------------------------------------------- error taxonomy
_PATH = re.compile(r"(?:[a-z]:\\[^\s'\"<>|]+|(?:/[\w.@%~+-]+){2,})", re.I)
_URL = re.compile(r"https?://[^\s'\"<>]+", re.I)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_HEX = re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I)
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_NUM = re.compile(r"\d+")
_QUOTED = re.compile(r"'[^']{0,80}'|\"[^\"]{0,80}\"")
_WS = re.compile(r"\s+")


def normalise_error(message) -> str:
    m = clean(message, 1000).lower()
    for rx, rep in ((_URL, "URL"), (_PATH, "PATH"), (_UUID, "ID"), (_IP, "IP"), (_HEX, "ID"), (_QUOTED, "STR"), (_NUM, "N")):
        m = rx.sub(rep, m)
    return _WS.sub(" ", m).strip()[:160]


def error_signature(category, message) -> str:
    return fingerprint(clean(category, 40) or "UNKNOWN", normalise_error(message), n=10)


def _run_error(r: dict):
    e = r.get("error") if isinstance(r.get("error"), dict) else {}
    return norm_category(e.get("category")), clean(e.get("message"), 300), clean(e.get("type"), 80)


def failure_runs(runs) -> list[dict]:
    return [r for r in series(runs) if is_fail(r) or (r.get("error") and r.get("status") in ("partial",))]


def error_taxonomy(runs, now: datetime, trend_days: int = 14, top: int = 25) -> dict:
    sigs: dict = {}
    cats = Counter()
    for r in failure_runs(runs):
        cat, msg, typ = _run_error(r)
        sig = error_signature(cat, msg or typ)
        s = sigs.setdefault(sig, {"signature": sig, "category": cat, "sample": msg or typ or "(no message)", "count": 0, "first_seen": r["started_at"],
                                  "last_seen": r["started_at"], "hosts": set(), "versions": set(), "automations": set(), "run_ids": [], "daily": Counter()})
        s["count"] += 1
        s["last_seen"] = max(s["last_seen"], r["started_at"])
        s["first_seen"] = min(s["first_seen"], r["started_at"])
        s["hosts"].add(clean(r.get("host"), 100) or "?")
        s["versions"].add(clean(r.get("automation_version"), 40) or "?")
        s["automations"].add(r["automation"])
        s["run_ids"] = (s["run_ids"] + [r["run_id"]])[-5:]
        s["daily"][_t(r).date().isoformat()] += 1
        cats[cat] += 1
    days = [(now - timedelta(days=i)).date().isoformat() for i in range(trend_days - 1, -1, -1)]
    rows = []
    for s in sigs.values():
        rows.append({"signature": s["signature"], "category": s["category"], "sample": s["sample"], "count": s["count"],
                     "first_seen": s["first_seen"], "last_seen": s["last_seen"], "hosts": sorted(s["hosts"])[:20],
                     "versions": sorted(s["versions"])[:20], "automations": sorted(s["automations"])[:20], "run_ids": s["run_ids"],
                     "trend": [s["daily"].get(d, 0) for d in days]})
    rows.sort(key=lambda s: (-s["count"], s["signature"]))
    cat_trend = {}
    for c in cats:
        cat_trend[c] = [0] * len(days)
    for r in failure_runs(runs):
        c = _run_error(r)[0]
        d = _t(r).date().isoformat()
        if d in days:
            cat_trend[c][days.index(d)] += 1
    return {"categories": dict(cats.most_common()), "category_trend": cat_trend, "signatures": rows[:top], "signature_total": len(rows),
            "failures": sum(cats.values()), "days": days}


def release_times(runs) -> dict:
    """automation -> {version: first start seen}."""
    out: dict = defaultdict(dict)
    for r in sorted(runs, key=lambda r: r["started_at"]):
        v = clean(r.get("automation_version"), 40)
        if v and v not in out[r["automation"]]:
            out[r["automation"]][v] = _t(r)
    return out


def regressions(runs, window_h: float = 48.0) -> list[dict]:
    """New error signature appearing within ``window_h`` of a new version showing up, absent on every earlier version."""
    rel = release_times(runs)
    by_auto: dict = defaultdict(list)
    for r in failure_runs(runs):
        by_auto[r["automation"]].append(r)
    out = []
    for auto, frs in by_auto.items():
        vers = rel.get(auto) or {}
        groups: dict = defaultdict(list)
        for r in frs:
            cat, msg, typ = _run_error(r)
            groups[error_signature(cat, msg or typ)].append(r)
        for sig, g in groups.items():
            first = g[0]
            v = clean(first.get("automation_version"), 40)
            if not v or v not in vers:
                continue
            released = vers[v]
            earlier = sorted((t, vv) for vv, t in vers.items() if t < released)
            if not earlier:
                continue            # the very first version: there is no baseline to regress from
            if not (released <= _t(first) <= released + timedelta(hours=window_h)):
                continue
            if any(vers.get(clean(x.get("automation_version"), 40), released) < released for x in g):
                continue            # also seen on an older version: an old problem, not a regression
            cat, msg, typ = _run_error(first)
            out.append({"automation": auto, "signature": sig, "category": cat, "sample": msg or typ or "(no message)", "version": v,
                        "previous_version": earlier[-1][1], "released_at": iso(released), "first_seen": first["started_at"],
                        "last_seen": g[-1]["started_at"], "count": len(g), "run_ids": [x["run_id"] for x in g][-5:]})
    return sorted(out, key=lambda x: (x["first_seen"], x["automation"]))


def machine_correlation(runs, now: datetime, lookback_h: float = 72.0, min_failures: int = 3, fail_rate: float = 0.6) -> dict:
    """Which machines / versions the failures concentrate on (a failure that follows a machine is a machine problem)."""
    start = now - timedelta(hours=lookback_h)
    rs = [r for r in series(runs) if _t(r) >= start]
    by_host: dict = defaultdict(list)
    for r in rs:
        by_host[clean(r.get("host"), 100) or "?"].append(r)
    hosts = []
    total_runs = sum(len(v) for v in by_host.values())
    total_fail = sum(1 for v in by_host.values() for r in v if is_fail(r))
    for h, hr in sorted(by_host.items()):
        f = [r for r in hr if is_fail(r)]
        autos = sorted({r["automation"] for r in f})
        n_other = total_runs - len(hr)                      # O(hosts), not O(hosts x runs): totals minus this host
        other_rate = ((total_fail - len(f)) / n_other) if n_other else None
        rate = len(f) / len(hr) if hr else 0.0
        unhealthy = bool(len(f) >= min_failures and rate >= fail_rate and (len(autos) >= 2 or (other_rate is not None and other_rate < 0.3)))
        hosts.append({"host": h, "runs": len(hr), "failed": len(f), "fail_rate": _round(rate), "automations_failing": autos[:20],
                      "other_hosts_fail_rate": _round(other_rate), "unhealthy": unhealthy, "run_ids": [r["run_id"] for r in f][-5:]})
    by_version: dict = defaultdict(lambda: [0, 0])
    for r in rs:
        k = (r["automation"], clean(r.get("automation_version"), 40) or "?")
        by_version[k][0] += 1
        by_version[k][1] += 1 if is_fail(r) else 0
    versions = [{"automation": a, "version": v, "runs": n, "failed": f, "fail_rate": _round(f / n)} for (a, v), (n, f) in sorted(by_version.items())]
    return {"hosts": hosts, "versions": versions}


# ----------------------------------------------------------------------------------------------------------- remediation
def remediation_stats(runs) -> dict:
    """Aggregate ``remediation_attempt`` events and the run-level ``remediation`` summary (both optional, both untrusted)."""
    play: dict = defaultdict(Counter)
    cats: dict = defaultdict(Counter)
    recs: dict = {}
    unfixed_runs = fixed_runs = 0
    needs_human: dict = {}
    for r in series(runs):
        for d in _events(r, "remediation_attempt"):
            outcome = clean(d.get("outcome"), 20)
            if outcome not in REMEDIATION_OUTCOMES:
                continue
            pb = clean(d.get("playbook"), 80) or "unnamed"
            cat = norm_category(d.get("category"))
            play[pb][outcome] += 1
            play[pb]["category:" + cat] += 1
            cats[cat][outcome] += 1
            if outcome == "recommended":
                recs[(r["automation"], pb, clean(d.get("detail"), 300))] = {"automation": r["automation"], "playbook": pb, "category": cat,
                                                                           "detail": clean(d.get("detail"), 300), "ts": r["started_at"], "run_id": r["run_id"]}
        summ = r.get("remediation") if isinstance(r.get("remediation"), dict) else None
        if summ is not None:
            if summ.get("fixed") is True:
                fixed_runs += 1
            elif summ.get("fixed") is False:
                unfixed_runs += 1
                cat = norm_category(summ.get("category"))
                prev = needs_human.get(cat)
                needs_human[cat] = {"category": cat, "runs": (prev["runs"] if prev else 0) + 1, "last_seen": r["started_at"], "run_id": r["run_id"],
                                    "automation": r["automation"], "runbook": CATEGORY_RUNBOOK.get(cat, CATEGORY_RUNBOOK["UNKNOWN"])}
    pb_rows = []
    for pb, c in sorted(play.items()):
        tried = c["fixed"] + c["not_fixed"]
        pb_rows.append({"playbook": pb, "attempts": sum(c[o] for o in REMEDIATION_OUTCOMES), "fixed": c["fixed"], "not_fixed": c["not_fixed"],
                        "skipped": c["skipped"], "escalated": c["escalated"], "recommended": c["recommended"],
                        "fix_rate": _round(c["fixed"] / tried) if tried else None,
                        "categories": sorted(k[9:] for k in c if k.startswith("category:"))[:10]})
    cat_rows = []
    for cat, c in sorted(cats.items()):
        tried = c["fixed"] + c["not_fixed"]
        cat_rows.append({"category": cat, "attempts": sum(c[o] for o in REMEDIATION_OUTCOMES), "fixed": c["fixed"], "not_fixed": c["not_fixed"],
                         "escalated": c["escalated"], "fix_rate": _round(c["fixed"] / tried) if tried else None})
    return {"playbooks": pb_rows, "categories": cat_rows, "fixed_runs": fixed_runs, "unfixed_runs": unfixed_runs,
            "needs_human": sorted(needs_human.values(), key=lambda x: (-x["runs"], x["category"])),
            "recommended": sorted(recs.values(), key=lambda x: x["ts"], reverse=True)[:25]}


# -------------------------------------------------------------------------------------------------- manual-step detection
def run_needs_person(r: dict) -> bool:
    if any(e.get("event") in MANUAL_EVENTS for e in (r.get("events") or []) if isinstance(e, dict)):
        return True
    res = r.get("result") if isinstance(r.get("result"), dict) else {}
    return clean(res.get("state"), 40) in MANUAL_STATES


def manual_step_streaks(runs) -> list[dict]:
    """Per (automation, host): the open stretch of runs that stopped for a person, with no later run that did not."""
    groups: dict = defaultdict(list)
    for r in series(runs):
        groups[(r["automation"], r.get("host"))].append(r)
    out = []
    for (auto, host), rs in groups.items():
        stretch = []
        for r in rs:
            stretch = stretch + [r] if run_needs_person(r) else []
        if stretch:
            out.append({"automation": auto, "host": host, "since": stretch[0]["started_at"], "last": stretch[-1]["started_at"],
                        "runs": len(stretch), "run_ids": [x["run_id"] for x in stretch][-5:]})
    return out


# ------------------------------------------------------------------------------------------------------ report builders
def automation_reliability(name: str, entry: dict | None, runs, now: datetime, cfg: dict, expected=None, grace: timedelta | None = None) -> dict:
    c = dict(DEFAULTS, **{k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
    slo = slo_for(entry, c)
    mine = [r for r in runs if r["automation"] == name]
    eps = episodes(mine)
    fl = flakiness(mine, c["flaky_window"], c["flaky_min_runs"], c["flaky_flips"], c["flaky_score"])
    anomalies = duration_anomalies(mine, c["baseline_n"], c["baseline_min"], c["anomaly_z"], c["anomaly_ratio"])
    budget = error_budget(mine, now, slo["target"], slo["window_days"])
    fast = burn_rate(mine, now, 24, slo["target"], c["burn_min_runs"])
    slow = burn_rate(mine, now, 24 * 7, slo["target"], c["burn_min_runs"])
    ok_durs = [r["duration_ms"] for r in series(mine) if r.get("status") in OK and _num(r.get("duration_ms")) is not None]
    out = {"slo": slo, "budget": budget, "burn_fast": fast, "burn_slow": slow, "episodes": mttr_mtbf(eps), "streaks": streaks(mine),
           "flakiness": fl, "duration": {"p50": percentile(ok_durs, 50), "p95": percentile(ok_durs, 95), "p99": percentile(ok_durs, 99),
                                          "trend": duration_trend(mine, now), "anomalies": anomalies[-5:]},
           "on_time": on_time(expected, mine, grace, now) if expected is not None and grace is not None else None}
    return out


def fleet_reliability(per_auto: dict, runs, now: datetime) -> dict:
    rs = series(runs)
    n = len(rs)
    f = sum(1 for r in rs if is_fail(r))
    eps = mttr_mtbf(episodes(runs))
    return {"runs": n, "failed": f, "success_rate": _round((n - f) / n) if n else None, "mttr_s": eps["mttr_s"], "mtbf_s": eps["mtbf_s"],
            "open_episodes": eps["open"], "flaky": sorted(a for a, v in per_auto.items() if v["flakiness"]["flaky"]),
            "budget_exhausted": sorted(a for a, v in per_auto.items() if v["budget"]["remaining"] is not None and v["budget"]["remaining"] < 0)}


# ------------------------------------------------------------------------------------------------------------- signals
def extra_signals(data, manifest: dict, now: datetime, cfg: dict, make, expected_fn=None) -> list[dict]:
    """The observability signals. ``make`` is analytics._sig (injected: no import cycle). Recomputed every build, so a signal that is no
    longer true simply disappears and alerting resolves it; fingerprints use stable windows so a persisting condition is deduplicated."""
    c = dict(DEFAULTS, **{k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
    autos = manifest.get("automations") or {}
    lookback = now - timedelta(hours=cfg.get("lookback_h", 72))
    runs = [r for r in data.runs]
    out: list[dict] = []
    names = sorted({r["automation"] for r in runs})
    for name in names:
        entry = autos.get(name) or {}
        if entry.get("status") == "paused" or (manifest.get("fleet") or {}).get("paused"):
            continue
        owner = entry.get("owner")
        mine = series([r for r in runs if r["automation"] == name])
        if not mine or _t(mine[-1]) < lookback:
            continue
        last = mine[-1]
        ring = last.get("ring")
        slo = slo_for(entry, c)
        bud = error_budget(mine, now, slo["target"], slo["window_days"])
        fast = burn_rate(mine, now, 24, slo["target"], c["burn_min_runs"])
        slow = burn_rate(mine, now, 24 * 7, slo["target"], c["burn_min_runs"])
        fails24 = [r for r in mine if is_fail(r) and _t(r) >= now - timedelta(hours=24)]
        fails7 = [r for r in mine if is_fail(r) and _t(r) >= now - timedelta(days=7)]
        window = None
        if fast is not None and fast >= c["fast_burn"] and len(fails24) >= 2:
            window, sev, ev = "fast", "high", fails24
            why = f"burning the error budget {fast:.1f}x too fast over 24 h"
        elif slow is not None and slow >= c["slow_burn"] and len(fails7) >= 2 and (bud["remaining"] is None or bud["remaining"] < 0.5):
            window, sev, ev = "slow", "medium", fails7
            why = f"burning the error budget {slow:.1f}x too fast over 7 d"
        elif bud["remaining"] is not None and bud["remaining"] < 0 and bud["failed"] >= 2 and bud["runs"] >= c["burn_min_runs"]:
            window, sev, ev = "exhausted", "medium", [r for r in mine if is_fail(r)][-5:]
            why = "error budget for the window is used up"
        if window:
            out.append(make("ERROR_BUDGET_BURN", sev, name, None, ring, window,
                            f"{name}: {why} (SLO {slo['target'] * 100:.2f}% over {slo['window_days']} d; {bud['failed']} of {bud['runs']} runs failed, "
                            f"{(bud['remaining'] or 0) * 100:.0f}% of the budget left).", _t(ev[0]), now, [r["run_id"] for r in ev[-5:]], owner,
                            slo=slo["target"], budget_remaining=bud["remaining"], burn_fast=fast, burn_slow=slow))
        fl = flakiness(mine, c["flaky_window"], c["flaky_min_runs"], c["flaky_flips"], c["flaky_score"])
        if fl["flaky"]:
            out.append(make("FLAKY", "medium", name, None, ring, "flaky",
                            f"{name} flipped between ok and failed {fl['flips']} times in its last {fl['runs']} runs (flip score {fl['score']}): "
                            "intermittent failure, fix the root cause instead of relying on retries.", _t(mine[-fl["runs"]]), _t(last),
                            [r["run_id"] for r in mine[-5:]], owner, flip_score=fl["score"]))
        an = duration_anomalies(mine, c["baseline_n"], c["baseline_min"], c["anomaly_z"], c["anomaly_ratio"])
        if an and an[-1]["run_id"] == last["run_id"]:
            a = an[-1]
            out.append(make("DURATION_ANOMALY", "low", name, a.get("host"), ring, a["run_id"],
                            f"Latest run took {a['duration_ms'] // 1000}s against a typical {a['baseline_ms'] // 1000}s (robust z {a['z']}, {a['direction']}).",
                            _t(last), _t(last), [a["run_id"]], owner, z=a["z"], direction=a["direction"]))
    for rg in regressions(runs, c["regression_window_h"]):
        if parse_iso(rg["last_seen"]) < lookback:
            continue
        entry = autos.get(rg["automation"]) or {}
        if entry.get("status") == "paused":
            continue
        out.append(make("REGRESSION_AFTER_RELEASE", "high", rg["automation"], None, None, rg["signature"] + "|" + rg["version"],
                        f"New error {rg['category']} first seen on {rg['version']} (new {rg['released_at']}), never on {rg['previous_version']}: "
                        f"{rg['sample'][:160]}", parse_iso(rg["first_seen"]), parse_iso(rg["last_seen"]), rg["run_ids"], entry.get("owner"),
                        error_category=rg["category"], error_message=rg["sample"][:300], error_fp=rg["signature"], version=rg["version"],
                        previous_version=rg["previous_version"], consecutive=rg["count"]))
    # remediation that keeps failing
    attempts: dict = defaultdict(list)
    for r in series(runs):
        if _t(r) < now - timedelta(days=7):
            continue
        for d in _events(r, "remediation_attempt"):
            oc = clean(d.get("outcome"), 20)
            if oc in ("fixed", "not_fixed", "escalated"):
                attempts[(r["automation"], clean(d.get("playbook"), 80) or "unnamed")].append((r, oc))
    for (auto, pb), lst in sorted(attempts.items()):
        bad = [x for x in lst if x[1] in ("not_fixed", "escalated")]
        good = [x for x in lst if x[1] == "fixed"]
        if len(bad) >= c["remediation_min_attempts"] and len(bad) > 2 * len(good):
            entry = autos.get(auto) or {}
            out.append(make("REMEDIATION_FAILING", "medium", auto, None, lst[-1][0].get("ring"), pb,
                            f"Playbook '{pb}' failed to fix {len(bad)} of {len(lst)} attempt(s) in 7 days: it is not helping; "
                            "fix the cause or retire the playbook.", _t(bad[0][0]), _t(bad[-1][0]), [x[0]["run_id"] for x in bad][-5:],
                            entry.get("owner"), playbook=pb))
    # a person has to do something and nobody did
    for st in manual_step_streaks(runs):
        since = parse_iso(st["since"])
        if now - since >= timedelta(hours=c["manual_step_h"]) and parse_iso(st["last"]) >= lookback - timedelta(hours=24):
            entry = autos.get(st["automation"]) or {}
            hrs = int((now - since).total_seconds() // 3600)
            out.append(make("PERSISTENT_MANUAL_STEP", "high" if hrs >= 3 * c["manual_step_h"] else "medium", st["automation"], st["host"], None, "manual",
                            f"{st['automation']} on {st['host']} has needed a person for {hrs} h ({st['runs']} run(s)): sign in / complete the manual step "
                            "(see the run's manual_step_required event).", since, parse_iso(st["last"]), st["run_ids"], entry.get("owner")))
    mc = machine_correlation(runs, now, cfg.get("lookback_h", 72), c["machine_min_failures"], c["machine_fail_rate"])
    for h in mc["hosts"]:
        if h["unhealthy"]:
            out.append(make("MACHINE_UNHEALTHY", "high", None, h["host"], None, "machine",
                            f"{h['failed']} of {h['runs']} runs on {h['host']} failed ({h['fail_rate']:.0%})"
                            + (f" while other machines fail {h['other_hosts_fail_rate']:.0%}" if h["other_hosts_fail_rate"] is not None else "")
                            + f"; affected: {', '.join(h['automations_failing'][:5])}. Likely a machine problem (profile, VPN, disk, session).",
                            now - timedelta(hours=cfg.get("lookback_h", 72)), now, h["run_ids"], None, automations=h["automations_failing"]))
    out.sort(key=lambda s: (SEVERITY_ORDER[s["severity"]], s["first_seen"]))
    return out
