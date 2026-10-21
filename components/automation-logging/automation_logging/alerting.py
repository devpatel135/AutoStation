"""Alerting: compliance signals -> typed alerts -> channels (Jira, webhook, email, GitLab issue, file/stdout).

Delivery state lives in ``state/alerts.json`` (fingerprint -> first_sent, last_sent, count, resolved). Rules:

* a new signal id            -> ``open`` alert
* still active after ``re_alert_interval_h``  -> ``reminder``
* severity increased          -> ``escalated`` (immediately)
* signal id disappeared       -> ``resolved`` (if ``notify_resolved``)
* reappears within ``dedupe_window_h`` of the last alert -> reactivated silently (flap suppression)
* quiet hours suppress everything below ``min_severity`` (state untouched, so it fires when the window ends)
* an alert only counts as sent if at least one channel accepted it; otherwise it is retried next run

HTTP and SMTP are injectable, so tests never touch the network. Add a channel: subclass ``Channel``, register in ``CHANNELS``.
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from .common import SEVERITY_ORDER, fingerprint, iso, miniyaml, parse_iso, read_json, wc_manifest, write_json, timezone

DEFAULT_CONFIG = {
    "channels": [{"name": "outbox", "type": "file", "path": "alerts-outbox.jsonl"}],
    "routing": {"critical": ["outbox"], "high": ["outbox"], "medium": ["outbox"], "low": []},
    "owners": {},
    "quiet_hours": None,
    "dedupe_window_h": 24,
    "re_alert_interval_h": 24,
    "notify_resolved": True,
}


def load_config(path: str | Path | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and Path(path).exists():
        user = miniyaml.loads(Path(path).read_text(encoding="utf-8")) or {}
        cfg.update(user)
    return cfg


# ------------------------------------------------------------------------------------------ transports
class HttpTransport:
    """Default urllib transport. ``request`` returns (status, body_bytes) and never raises on HTTP errors."""

    def request(self, method: str, url: str, headers: dict | None = None, body: bytes | None = None, timeout: int = 30):
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-configured URLs
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


# ------------------------------------------------------------------------------------------ alert content
def enrich(signals: list[dict], data) -> list[dict]:
    """Add retry history + run synopsis + log path from the evidence runs (in place)."""
    for s in signals:
        runs = [data.runs_by_id[r] for r in s.get("evidence", []) if r in data.runs_by_id]
        if runs:
            last = runs[-1]
            s.setdefault("synopsis", last.get("synopsis"))
            s["log_path"] = last.get("_path")
            hist = []
            for r in runs[-3:]:
                for e in r.get("events", []):
                    if e.get("event") == "retry_event":
                        d = e.get("data") or {}
                        hist.append({k: d.get(k) for k in ("attempt", "category", "decision", "source", "reasoning", "outcome")}
                                    | {"run_id": r["run_id"]})
            s["retry_history"] = hist[-8:]
    return signals


def build_alert(signal: dict, kind: str, now: datetime, log_base_url: str | None = None, owner_email: str | None = None) -> dict:
    diag = signal.get("diagnosis") or None
    path = signal.get("log_path")
    link = (log_base_url.rstrip("/") + "/" + path) if (log_base_url and path) else path
    sev = signal["severity"]
    scope = signal.get("automation") or ", ".join(signal.get("automations") or []) or signal.get("host") or "fleet"
    title = f"[AUTOSTATION {sev.upper()}] {signal['type']} - {scope}" + (f" on {signal['host']}" if signal.get("host") else "")
    if kind == "resolved":
        title = f"[AUTOSTATION RESOLVED] {signal['type']} - {scope}"
    return {"alert_id": signal["id"], "kind": kind, "ts": iso(now), "severity": sev, "signal_type": signal["type"],
            "automation": signal.get("automation"), "host": signal.get("host"), "ring": signal.get("ring"),
            "title": title, "detail": signal.get("detail"), "error_category": signal.get("error_category"),
            "error_message": signal.get("error_message"), "diagnosis": diag, "diagnosis_reason": signal.get("diagnosis_reason"),
            "retry_history": signal.get("retry_history") or [], "synopsis": signal.get("synopsis"), "log_link": link,
            "evidence": signal.get("evidence") or [], "owner": owner_email or signal.get("owner"),
            "first_seen": signal.get("first_seen")}


def render_text(a: dict) -> str:
    lines = [a["title"], "", a.get("detail") or ""]
    if a.get("error_category"):
        lines.append(f"Error category: {a['error_category']}")
    if a.get("synopsis"):
        lines.append(f"Run synopsis: {a['synopsis']}")
    d = a.get("diagnosis")
    if d:
        lines += ["", "LLM diagnosis (" + str(d.get("confidence", "?")) + " confidence):", f"  Root cause: {d.get('root_cause')}",
                  f"  Suggested action: {d.get('suggested_action')}"]
    if a.get("retry_history"):
        lines += ["", "Retry history:"] + [f"  #{r.get('attempt')} {r.get('category')} -> {r.get('decision')} ({r.get('source')}): {r.get('outcome')}"
                                           for r in a["retry_history"]]
    lines += ["", f"Owner: {a.get('owner') or 'unassigned'}", f"Logs: {a.get('log_link') or 'n/a'}", f"Alert id: {a['alert_id']}"]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------ channels
class ChannelError(RuntimeError):
    pass


class Channel:
    def __init__(self, cfg: dict, ctx: "Context"):
        self.cfg, self.ctx, self.name = cfg, ctx, cfg["name"]

    def send(self, alert: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


class Context:
    def __init__(self, http=None, smtp_factory=None, env=None, state_dir: Path | None = None, stdout=None):
        self.http = http or HttpTransport()
        self.smtp_factory = smtp_factory or smtplib.SMTP
        self.env = os.environ if env is None else env
        self.state_dir = Path(state_dir or ".")
        self.stdout = stdout or sys.stdout


class FileChannel(Channel):
    def send(self, alert):
        p = Path(self.cfg.get("path", "alerts-outbox.jsonl"))
        if not p.is_absolute():
            p = self.ctx.state_dir / p
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"channel": self.name, **alert}, default=str) + "\n")
        return {"ok": True, "detail": str(p)}


class StdoutChannel(Channel):
    def send(self, alert):
        print(json.dumps({"channel": self.name, **alert}, default=str), file=self.ctx.stdout)
        return {"ok": True, "detail": "stdout"}


class WebhookChannel(Channel):
    COLORS = {"critical": "B91C1C", "high": "D97706", "medium": "2563EB", "low": "6B7280"}

    def _url(self):
        url = self.cfg.get("url") or self.ctx.env.get(self.cfg.get("url_env", "ALERT_WEBHOOK_URL"))
        if not url:
            raise ChannelError(f"webhook {self.name}: no url / {self.cfg.get('url_env', 'ALERT_WEBHOOK_URL')} not set")
        return url

    def payload(self, a: dict) -> dict:
        fmt = self.cfg.get("format", "json")
        text = render_text(a)
        if fmt == "teams":
            facts = [{"name": k, "value": str(v)} for k, v in (("Automation", a.get("automation")), ("Signal", a["signal_type"]),
                     ("Severity", a["severity"]), ("Host", a.get("host")), ("Error", a.get("error_category")),
                     ("Owner", a.get("owner"))) if v]
            return {"@type": "MessageCard", "@context": "http://schema.org/extensions", "summary": a["title"],
                    "themeColor": self.COLORS.get(a["severity"], "6B7280"), "title": a["title"],
                    "sections": [{"facts": facts, "text": text.replace("\n", "\n\n")}]}
        if fmt == "slack":
            return {"text": a["title"], "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "*" + a["title"] + "*\n```" + text + "```"}}]}
        return {"alert": a, "text": text}

    def send(self, alert):
        status, body = self.ctx.http.request("POST", self._url(), {"Content-Type": "application/json"},
                                             json.dumps(self.payload(alert)).encode("utf-8"))
        if not 200 <= status < 300:
            raise ChannelError(f"webhook {self.name}: HTTP {status}")
        return {"ok": True, "detail": f"HTTP {status}"}


class EmailChannel(Channel):
    def send(self, alert):
        env = self.ctx.env
        host = env.get("SMTP_HOST")
        if not host:
            raise ChannelError("email: SMTP_HOST not set")
        to = list(self.cfg.get("to") or [])
        owner = alert.get("owner")
        if self.cfg.get("to_owner", True) and owner and "@" in str(owner):
            to.append(owner)
        if not to:
            raise ChannelError("email: no recipients")
        msg = EmailMessage()
        msg["Subject"] = alert["title"]
        msg["From"] = env.get("SMTP_FROM", "autostation@localhost")
        msg["To"] = ", ".join(sorted(set(to)))
        msg.set_content(render_text(alert))
        smtp = self.ctx.smtp_factory(host, int(env.get("SMTP_PORT", "25")))
        try:
            if env.get("SMTP_STARTTLS", "0") == "1":
                smtp.starttls()
            if env.get("SMTP_USER"):
                smtp.login(env["SMTP_USER"], env.get("SMTP_PASSWORD", ""))
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "detail": msg["To"]}


class JiraChannel(Channel):
    """Create-or-comment with fingerprint de-dupe: JQL on label ``autostation-<alert_id>`` (open issues only)."""

    def _auth(self):
        env = self.ctx.env
        token = env.get("JIRA_TOKEN")
        if not token:
            raise ChannelError("jira: JIRA_TOKEN not set")
        if env.get("JIRA_USER"):
            import base64
            return "Basic " + base64.b64encode(f"{env['JIRA_USER']}:{token}".encode()).decode()
        return "Bearer " + token  # Jira Data Center personal access token

    def send(self, alert):
        env = self.ctx.env
        base, project = env.get("JIRA_URL"), env.get("JIRA_PROJECT") or self.cfg.get("project")
        if not base or not project:
            raise ChannelError("jira: JIRA_URL / JIRA_PROJECT not set")
        base = base.rstrip("/")
        hdr = {"Authorization": self._auth(), "Content-Type": "application/json", "Accept": "application/json"}
        label = "autostation-" + alert["alert_id"]
        jql = f'project = "{project}" AND labels = "{label}" AND statusCategory != Done'
        st, body = self.ctx.http.request("GET", f"{base}/rest/api/2/search?jql={urllib.parse.quote(jql)}&fields=key&maxResults=1", hdr)
        if st != 200:
            raise ChannelError(f"jira search HTTP {st}")
        issues = json.loads(body or b"{}").get("issues") or []
        text = render_text(alert)
        if issues:
            key = issues[0]["key"]
            st, _ = self.ctx.http.request("POST", f"{base}/rest/api/2/issue/{key}/comment", hdr,
                                          json.dumps({"body": f"{alert['kind'].upper()}: {text}"}).encode())
            if st not in (200, 201):
                raise ChannelError(f"jira comment HTTP {st}")
            return {"ok": True, "detail": f"commented {key}"}
        if alert["kind"] == "resolved":
            return {"ok": True, "detail": "no open issue to resolve"}
        fields = {"project": {"key": project}, "summary": alert["title"][:250], "description": text,
                  "issuetype": {"name": self.cfg.get("issue_type", "Bug")},
                  "labels": ["autostation", label, "autostation-" + alert["signal_type"].lower()]}
        st, body = self.ctx.http.request("POST", f"{base}/rest/api/2/issue", hdr, json.dumps({"fields": fields}).encode())
        if st not in (200, 201):
            raise ChannelError(f"jira create HTTP {st}")
        return {"ok": True, "detail": "created " + str(json.loads(body or b"{}").get("key"))}


class GitLabIssueChannel(Channel):
    def send(self, alert):
        env = self.ctx.env
        api, pid, token = env.get("GITLAB_API_URL"), env.get("GITLAB_ALERT_PROJECT_ID"), env.get("GITLAB_ALERT_TOKEN")
        if not (api and pid and token):
            raise ChannelError("gitlab-issue: GITLAB_API_URL / GITLAB_ALERT_PROJECT_ID / GITLAB_ALERT_TOKEN not set")
        api = api.rstrip("/")
        hdr = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
        label = "autostation::" + alert["alert_id"]
        st, body = self.ctx.http.request("GET", f"{api}/projects/{pid}/issues?state=opened&labels={urllib.parse.quote(label)}", hdr)
        if st != 200:
            raise ChannelError(f"gitlab list HTTP {st}")
        found = json.loads(body or b"[]")
        text = render_text(alert)
        if found:
            iid = found[0]["iid"]
            st, _ = self.ctx.http.request("POST", f"{api}/projects/{pid}/issues/{iid}/notes", hdr, json.dumps({"body": f"**{alert['kind']}**\n\n{text}"}).encode())
            if st not in (200, 201):
                raise ChannelError(f"gitlab note HTTP {st}")
            if alert["kind"] == "resolved":
                self.ctx.http.request("PUT", f"{api}/projects/{pid}/issues/{iid}", hdr, json.dumps({"state_event": "close"}).encode())
            return {"ok": True, "detail": f"noted #{iid}"}
        if alert["kind"] == "resolved":
            return {"ok": True, "detail": "no open issue to resolve"}
        st, body = self.ctx.http.request("POST", f"{api}/projects/{pid}/issues", hdr, json.dumps(
            {"title": alert["title"][:250], "description": text, "labels": f"autostation,{label},severity::{alert['severity']}"}).encode())
        if st not in (200, 201):
            raise ChannelError(f"gitlab create HTTP {st}")
        return {"ok": True, "detail": "created #" + str(json.loads(body or b"{}").get("iid"))}


CHANNELS = {"file": FileChannel, "stdout": StdoutChannel, "webhook": WebhookChannel, "email": EmailChannel,
            "jira": JiraChannel, "gitlab-issue": GitLabIssueChannel}


# ------------------------------------------------------------------------------------------ engine
def in_quiet_hours(q: dict | None, now: datetime) -> bool:
    if not q or not q.get("window"):
        return False
    import re
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", str(q["window"]))
    if not m:
        return False
    tz = wc_manifest._tz(q.get("timezone")) or timezone.utc
    t = now.astimezone(tz).time()
    start, end = (int(m[1]), int(m[2])), (int(m[3]), int(m[4]))
    cur = (t.hour, t.minute)
    return (start <= cur < end) if start <= end else (cur >= start or cur < end)


class AlertEngine:
    def __init__(self, config: dict, state: dict | None = None, ctx: Context | None = None, log_base_url: str | None = None):
        self.cfg = config
        self.state = state if state is not None else {}
        self.ctx = ctx or Context()
        self.log_base_url = log_base_url or os.environ.get("LOG_BASE_URL")
        self.channels = {}
        for c in config.get("channels") or []:
            if c["type"] not in CHANNELS:
                raise ValueError(f"unknown alert channel type {c['type']!r}")
            self.channels[c["name"]] = CHANNELS[c["type"]](c, self.ctx)

    def _route(self, signal: dict) -> list[str]:
        owner_cfg = (self.cfg.get("owners") or {}).get(signal.get("automation") or "") or {}
        names = owner_cfg.get("channels") or (self.cfg.get("routing") or {}).get(signal["severity"]) or []
        return [n for n in names if n in self.channels]

    def _deliver(self, signal: dict, kind: str, now: datetime, dry_run: bool, summary: dict) -> bool:
        owner_cfg = (self.cfg.get("owners") or {}).get(signal.get("automation") or "") or {}
        alert = build_alert(signal, kind, now, self.log_base_url, owner_cfg.get("email"))
        names = self._route(signal)
        if not names:
            summary["suppressed"].append({"id": signal["id"], "reason": "no-route", "kind": kind})
            return False
        if dry_run:
            summary["would_send"].append({"id": signal["id"], "kind": kind, "channels": names, "title": alert["title"]})
            return True
        ok = False
        for n in names:
            try:
                res = self.channels[n].send(alert)
                ok = ok or res.get("ok", False)
                summary["sent"].append({"id": signal["id"], "kind": kind, "channel": n, "detail": res.get("detail")})
            except Exception as exc:  # noqa: BLE001 - one broken channel must not block the others
                summary["errors"].append({"id": signal["id"], "channel": n, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return ok

    def process(self, signals: list[dict], now: datetime, dry_run: bool = False) -> dict:
        summary = {"sent": [], "would_send": [], "suppressed": [], "resolved": [], "errors": [], "dry_run": dry_run}
        cfg = self.cfg
        interval = timedelta(hours=float(cfg.get("re_alert_interval_h", 24)))
        dedupe = timedelta(hours=float(cfg.get("dedupe_window_h", 24)))
        quiet = in_quiet_hours(cfg.get("quiet_hours"), now)
        min_sev = (cfg.get("quiet_hours") or {}).get("min_severity", "critical")
        state = self.state if not dry_run else json.loads(json.dumps(self.state))
        active = {s["id"]: s for s in signals}

        for s in signals:
            sid, st = s["id"], state.get(s["id"])
            if quiet and SEVERITY_ORDER[s["severity"]] > SEVERITY_ORDER.get(min_sev, 0):
                summary["suppressed"].append({"id": sid, "reason": "quiet-hours"})
                continue
            kind = None
            if st is None:
                kind = "open"
            elif st.get("resolved"):
                if st.get("last_sent") and now - parse_iso(st["last_sent"]) < dedupe:
                    st["resolved"] = False
                    st.pop("resolved_at", None)
                    summary["suppressed"].append({"id": sid, "reason": "flap-suppressed"})
                    continue
                kind = "open"
            elif SEVERITY_ORDER[s["severity"]] < SEVERITY_ORDER.get(st.get("severity"), 99):
                kind = "escalated"
            elif st.get("last_sent") is None or now - parse_iso(st["last_sent"]) >= interval:
                kind = "reminder"
            else:
                summary["suppressed"].append({"id": sid, "reason": "dedupe"})
                continue
            if self._deliver(s, kind, now, dry_run, summary):
                cur = st if (st and kind != "open") else {"first_sent": iso(now), "count": 0}
                cur.update(last_sent=iso(now), count=cur.get("count", 0) + 1, resolved=False, severity=s["severity"],
                           automation=s.get("automation"), type=s["type"], host=s.get("host"), title=s.get("detail"))
                cur.pop("resolved_at", None)
                state[sid] = cur

        for sid, st in list(state.items()):
            if sid in active or st.get("resolved"):
                continue
            if quiet:
                continue  # hold resolved notices until quiet hours end
            sig = {"id": sid, "type": st.get("type", "?"), "severity": st.get("severity", "medium"), "automation": st.get("automation"),
                   "host": st.get("host"), "detail": "Condition cleared: no longer detected by the analytics pipeline.",
                   "evidence": []}
            ok = True
            if cfg.get("notify_resolved", True):
                ok = self._deliver(sig, "resolved", now, dry_run, summary)
            if ok:
                st["resolved"], st["resolved_at"] = True, iso(now)
                summary["resolved"].append(sid)
        if not dry_run:
            self.state = state
        return summary


def run(signals: list[dict], config_path, state_path, now: datetime, dry_run: bool = False, ctx: Context | None = None,
        log_base_url: str | None = None) -> dict:
    cfg = load_config(config_path)
    state = read_json(state_path, {}) or {}
    ctx = ctx or Context(state_dir=Path(state_path).parent)
    eng = AlertEngine(cfg, state, ctx, log_base_url)
    summary = eng.process(signals, now, dry_run)
    if not dry_run:
        write_json(state_path, eng.state)
    return summary
