"""automation-notifier: AUTOSTATION dog-fooding its own alerting (assessment: alerting option B).

Reads the compliance ``signals.json`` that the automation-logging pipeline publishes and notifies the owning team via
Teams webhook and/or email - the channel people actually watch. Headless, idempotent and quiet when nothing is new.

Parameters (schedule slot params / manifest assets):
    signals_url      http(s) URL or file path of signals.json   (asset: signals_url)
    channel          teams | email | outbox | teams,email         (default: outbox - safe until a webhook is configured)
    min_severity     low | medium | high | critical               (default: high)
    remind_hours     re-notify an unresolved signal after N hours (default: 24; 0 = never)
Secrets (never in the manifest): Teams webhook in Credential Manager ``AUTOMAGIX/notifier_webhook`` (or env
NOTIFIER_WEBHOOK_URL for dev); SMTP creds in ``AUTOMAGIX/notifier_smtp`` as ``user:password`` (optional).
"""
from __future__ import annotations

import json
import os
import smtplib
import time
import urllib.request
from email.message import EmailMessage
from pathlib import Path

import wrapper_core as wrapper
from wrapper_core import secrets as wsecrets

SEVERITY = {"low": 1, "medium": 2, "high": 3, "critical": 4}
NAME = "automation-notifier"


def data_dir() -> Path:
    d = Path(os.environ.get("AUTOMAGIX_HOME", Path.home() / ".automagix")) / "data" / NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_signals(location: str) -> list[dict]:
    if location.startswith(("http://", "https://")):
        req = urllib.request.Request(location, headers={"User-Agent": NAME})
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - operator-configured URL
            doc = json.loads(r.read().decode("utf-8"))
    else:
        doc = json.loads(Path(location).read_text(encoding="utf-8"))
    return doc["signals"] if isinstance(doc, dict) else doc


def render(sig: dict) -> tuple[str, str]:
    d = sig.get("diagnosis") or {}
    title = f"[{sig['severity'].upper()}] {sig['type']} - {sig['automation']}"
    lines = [f"Automation: {sig['automation']} ({sig.get('ring', '?')} ring, host {sig.get('host') or 'n/a'})",
             f"Signal: {sig['type']}  -  {sig.get('detail', '')}"]
    if d:
        lines += [f"Likely cause: {d.get('root_cause', '')}", f"Suggested action: {d.get('suggested_action', '')}"]
    if sig.get("evidence"):
        lines.append("Evidence runs: " + ", ".join(sig["evidence"][:5]))
    lines.append(f"Owner: {sig.get('owner', 'unassigned')}   Signal id: {sig['id']}")
    return title, "\n".join(lines)


def post_json(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
        if r.status >= 300:
            raise ConnectionError(f"webhook returned {r.status}")


def send_teams(title: str, body: str) -> None:
    url = os.environ.get("NOTIFIER_WEBHOOK_URL") or wsecrets.credman_read("AUTOMAGIX/notifier_webhook")
    if not url:
        raise wrapper.LogicError("Teams webhook is not configured (Credential Manager AUTOMAGIX/notifier_webhook)")
    post_json(url, {"@type": "MessageCard", "@context": "https://schema.org/extensions", "summary": title,
                    "title": title, "text": body.replace("\n", "<br>")})


def send_email(title: str, body: str, to: str, assets: dict) -> None:
    host = assets.get("smtp_host")
    if not host or not to:
        raise wrapper.LogicError("email channel needs asset smtp_host and an owner address")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = title, assets.get("mail_from", "autostation@localhost"), to
    msg.set_content(body)
    with smtplib.SMTP(host, int(assets.get("smtp_port", 25)), timeout=20) as s:
        cred = wsecrets.credman_read("AUTOMAGIX/notifier_smtp")
        if cred and ":" in cred:
            s.starttls()
            s.login(*cred.split(":", 1))
        s.send_message(msg)


def deliver(channels: list[str], sig: dict, assets: dict, outbox: Path) -> None:
    title, body = render(sig)
    for ch in channels:
        if ch == "teams":
            send_teams(title, body)
        elif ch == "email":
            send_email(title, body, sig.get("owner", ""), assets)
        elif ch == "outbox":
            with open(outbox, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "title": title, "body": body, "signal": sig["id"]}) + "\n")
        else:
            raise wrapper.LogicError(f"unknown channel {ch!r}")


@wrapper.entrypoint
def main() -> None:
    assets = wrapper.assets()
    location = wrapper.param("signals_url") or assets.get("signals_url")
    if not location:
        raise wrapper.LogicError("no signals_url (param or asset)")
    channels = [c.strip() for c in str(wrapper.param("channel", "outbox")).split(",") if c.strip()]
    min_sev = SEVERITY[str(wrapper.param("min_severity", "high"))]
    remind = float(wrapper.param("remind_hours", "24")) * 3600
    state_file = data_dir() / "notified.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    signals = wrapper.run_with_retry(lambda: load_signals(location))
    active = {s["id"]: s for s in signals}
    due = []
    for sid, sig in active.items():
        if SEVERITY.get(sig["severity"], 0) < min_sev:
            continue
        last = state.get(sid, {}).get("last_sent")
        if last is None or (remind and time.time() - last >= remind):
            due.append(sig)
    wrapper.log_event("start", {"signals": len(signals), "due": len(due), "channels": channels})

    sent = 0
    for sig in due:
        if wrapper.is_dry_run():
            wrapper.log_item(sig["id"], "skipped", reason="dry_run")
            continue
        wrapper.run_item(sig["id"], deliver, channels, sig, assets, data_dir() / "outbox.jsonl")
        st = state.setdefault(sig["id"], {"first_sent": time.time(), "count": 0})
        st["last_sent"], st["count"] = time.time(), st["count"] + 1
        sent += 1
    resolved = [sid for sid in state if sid not in active and not state[sid].get("resolved")]
    for sid in resolved:
        state[sid]["resolved"] = True
        wrapper.log_event("signal_resolved", {"signal": sid})
    if not wrapper.is_dry_run():
        state_file.write_text(json.dumps(state), encoding="utf-8")
    wrapper.set_result(sent=sent, resolved=len(resolved))


if __name__ == "__main__":
    raise SystemExit(main())
