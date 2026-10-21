"""Believable synthetic fleet history for demos, README screenshots and smoke tests.

``python -m automation_logging sample --out ./sample`` writes ``archive/``, ``manifest.yaml`` and ``alerting.yaml``.
History is generated relative to *now* (override with ``--now``) and is deterministic for a given seed. It contains: steady
daily runs, a partial run with item failures, a network incident that retried and recovered, a logic-error failure streak, a
hung run, a missed run, a silent host, a drifted host and a run inside a maintenance window.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import synthetic as syn
from .common import miniyaml

HOSTS = ["VDI-0101", "VDI-0102", "VDI-0103", "VDI-0204", "VDI-0305", "VDI-0306"]


def build(now: datetime | None = None, seed: int = 7, days: int = 14) -> tuple[list[dict], dict]:
    """Return (artefacts, manifest)."""
    rnd = random.Random(seed)
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    today = now.replace(hour=0, minute=0, second=0)
    arts: list[dict] = []

    autos = {
        "automation-invoice-router": syn.automation_entry("Pull open AP invoices, classify with LLM, route to AP team", "ap-automation@example.com",
                                                          version="1.2.0", rings={"dev": "1.3.0", "pilot": "1.2.0", "broad": "1.2.0"},
                                                          versions={"1.3.0": {"url": "a-1.3.0.zip", "sha256": "0" * 64}, "1.2.0": {"url": "a-1.2.0.zip", "sha256": "1" * 64},
                                                                    "1.1.0": {"url": "a-1.1.0.zip", "sha256": "2" * 64}},
                                                          slots=[{"id": "daily", "type": "daily", "time": "09:00"}], baseline=180000,
                                                          catalog={"minutes_per_item": 4}),
        "automation-certification-site": syn.automation_entry("Quarterly compliance report submission on portal Y", "compliance-ops@example.com",
                                                         version="2.0.1", slots=[{"id": "daily", "type": "daily", "time": "07:30"}],
                                                         baseline=300000, classification="confidential", llm_allowed="internal",
                                                         maintenance=[{"daily": "02:00-04:00"}], catalog={"minutes_per_item": 12}),
        "automation-example-hello": syn.automation_entry("Platform smoke-test automation", "platform@example.com", version="1.0.0",
                                                         slots=[{"id": "hourly", "type": "hourly", "every": 6}], baseline=20000),
        "automation-notifier": syn.automation_entry("Sends Teams/email notifications from signals.json", "platform@example.com", version="0.3.0",
                                                    slots=[{"id": "poll", "type": "poll", "every_minutes": 15}], baseline=30000,
                                                    triggers=[{"on_automation": "automation-invoice-router", "on_status": "failed", "slot": "poll"}]),
    }
    manifest = syn.manifest_dict(autos)

    def at(day_offset: int, hh: int, mm: int = 0, jitter: bool = True) -> datetime:
        base = today - timedelta(days=day_offset) + timedelta(hours=hh, minutes=mm)
        return base + timedelta(seconds=rnd.randint(0, 90) if jitter else 0)

    hosts_for = {"automation-invoice-router": HOSTS[:3], "automation-certification-site": HOSTS[3:5], "automation-example-hello": HOSTS[:2] + HOSTS[5:],
                 "automation-notifier": HOSTS[:1]}
    for a, hs in hosts_for.items():
        arts.append(syn.lifecycle_record("installed", a, hs[0], at(days, 8)))

    def make(automation, host, start, **kw):
        arts.append(syn.run_record(automation, start, host=host, **kw))
        return arts[-1]

    by_key: dict[tuple, list[dict]] = {}
    for d in range(days, -1, -1):
        for hi, host in enumerate(hosts_for["automation-invoice-router"]):
            start = at(d, 9, hi * 2)
            if start > now - timedelta(minutes=30):
                continue
            n = rnd.randint(35, 60)
            items = [(f"INV-{d}{hi}{i:03d}", "success", rnd.randint(700, 1900), None) for i in range(n)]
            for k in range(rnd.randint(2, 5)):
                items[k] = (items[k][0], "skipped", rnd.randint(300, 600), "duplicate")
            retries = []
            if d == 6 and hi == 1:  # network incident: retried twice, recovered
                retries = [{"attempt": 1, "category": "NETWORK_ERROR", "decision": "retry", "source": "deterministic",
                            "reasoning": "network error - reconnect, wait, retry", "outcome": "failed_again", "wait_s": 60},
                           {"attempt": 2, "category": "NETWORK_ERROR", "decision": "retry", "source": "deterministic",
                            "reasoning": "network error - reconnect, wait, retry", "outcome": "recovered", "wait_s": 60}]
            r = make("automation-invoice-router", host, start, version="1.2.0", ring="pilot" if hi else "broad",
                     duration_ms=rnd.randint(150000, 190000), items=items, retries=retries, params={"date_range": "yesterday"})
            by_key.setdefault(("automation-invoice-router", host), []).append(r)
        for hi, host in enumerate(hosts_for["automation-certification-site"]):
            start = at(d, 7, 30)
            if start > now - timedelta(minutes=30) or d == 2:   # nobody ran on day -2: MISSED
                continue
            items = [(f"RPT-{d}-{i}", "success", rnd.randint(8000, 15000), None) for i in range(rnd.randint(4, 8))]
            r = make("automation-certification-site", host, start, version="2.0.1", duration_ms=rnd.randint(240000, 310000), items=items,
                     params={"quarter": "Q3"})
            by_key.setdefault(("automation-certification-site", host), []).append(r)
        for host in hosts_for["automation-example-hello"]:
            if host == "VDI-0306" and d < 3:
                continue  # this machine went dark 3 days ago -> SILENT
            for hh in (0, 6, 12, 18):
                start = at(d, hh, 0)
                if start <= now - timedelta(minutes=30):
                    make("automation-example-hello", host, start, version="1.0.0", duration_ms=rnd.randint(8000, 14000),
                         items=[("ping", "success", 400, None)], llm_calls=0)
    for d in (3, 1):
        make("automation-notifier", HOSTS[0], at(d, 12, 15), version="0.3.0", slot="poll", duration_ms=9000, llm_calls=0,
             items=[("alert-1", "success", 800, None)])

    # ---- scenarios applied to the most recent runs so the dashboard always shows live conditions ----
    def latest(automation, host, n=1):
        return by_key[(automation, host)][-n:]

    for r in latest("automation-invoice-router", HOSTS[2], 3):  # LOGIC_ERROR streak (critical)
        r.update(status="failed", exit_code=1, items={"total": 0, "success": 0, "skipped": 0, "failed": 0},
                 error={"category": "LOGIC_ERROR", "type": "KeyError", "message": "KeyError: 'vendor_id' in route_invoice",
                        "traceback_tail": ["  File main.py, line 88, in route_invoice", "KeyError: 'vendor_id'"], "attempts": 1},
                 synopsis="automation-invoice-router failed after 2s: KeyError 'vendor_id' while routing; no items were processed.")
        r["events"] = [e for e in r["events"] if e["event"] != "item_processed"]
    r = latest("automation-invoice-router", HOSTS[0])[0]  # "successful" run hiding 12 failed items
    done = 0
    for e in r["events"]:
        if e["event"] == "item_processed" and e["data"]["outcome"] == "success" and done < 12:
            e["data"].update(outcome="failed", error_category="data_error", duration_ms=3400)
            done += 1
    r["items"] = {"total": r["items"]["total"], "success": r["items"]["success"] - done, "skipped": r["items"]["skipped"], "failed": done}
    r.update(status="partial", synopsis=f"Routed {r['items']['success']} invoices to AP; {done} failed with missing vendor data and need review.")
    r = latest("automation-certification-site", HOSTS[3])[0]     # hung then killed
    r.update(status="hung", exit_code=1, duration_ms=3_600_000, hung_detected=True,
             error={"category": "SYSTEM_UNAVAILABLE", "type": "Escalation", "message": "run exceeded timeout_s and was killed", "attempts": 1},
             synopsis="automation-certification-site hung waiting on the portal and was killed after 60 minutes.")
    r = latest("automation-certification-site", HOSTS[4])[0]     # slow + stuck on an old version, self-heal failing
    r.update(version="2.0.0", duration_ms=900_000, self_heal_errors=None)
    r["automation_version"] = "2.0.0"
    r["self_heal"] = {"installed": [], "offline": False, "errors": ["automation-certification-site 2.0.1: sha256 mismatch (staged download corrupt)"]}
    r["synopsis"] = "automation-certification-site finished slowly (15m) on version 2.0.0; self-heal could not install 2.0.1."
    # UNEXPECTED: ran inside the 02:00-04:00 maintenance window yesterday
    make("automation-certification-site", HOSTS[4], at(1, 2, 30), version="2.0.1", duration_ms=250000, items=[("RPT-X", "success", 9000, None)])

    # heartbeats: daily login pings; VDI-0306 stops 3 days ago
    ver = {"automation-certification-site": "2.0.1", "automation-invoice-router": "1.2.0"}
    for host in HOSTS:
        autos_on = {a: ("2.0.0" if (a == "automation-certification-site" and host == HOSTS[4]) else ver.get(a, "1.0.0" if a != "automation-notifier" else "0.3.0"))
                    for a, hs in hosts_for.items() if host in hs}
        last_day = 3 if host == "VDI-0306" else 0
        for d in range(days, last_day - 1, -1):
            ts = at(d, 6, 0)
            if ts <= now:
                arts.append(syn.heartbeat_record(host, ts, automations=autos_on, ring="pilot", phase="login"))
    return arts, manifest


ALERTING_YAML = """# automation-logging alert routing (miniyaml subset). Enable more channels by adding them to `routing`
# and setting their environment variables as protected CI variables.
channels:
  - {name: outbox, type: file, path: alerts-outbox.jsonl}
  - {name: teams, type: webhook, format: teams, url_env: TEAMS_WEBHOOK_URL}
  - {name: jira, type: jira, issue_type: Bug}
routing:
  critical: [outbox]        # e.g. [outbox, teams, jira]
  high: [outbox]
  medium: [outbox]
  low: []
owners: {}
quiet_hours: {timezone: UTC, window: "22:00-06:00", min_severity: critical}
dedupe_window_h: 24
re_alert_interval_h: 24
notify_resolved: true
"""


def write(out: str | Path, now: datetime | None = None, seed: int = 7, days: int = 14) -> dict:
    out = Path(out)
    arts, manifest = build(now, seed, days)
    syn.write_archive(out / "archive", arts)
    (out / "manifest.yaml").write_text(miniyaml.dumps(manifest), encoding="utf-8")
    (out / "alerting.yaml").write_text(ALERTING_YAML, encoding="utf-8")
    return {"artefacts": len(arts), "out": str(out)}
