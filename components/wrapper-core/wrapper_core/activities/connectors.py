"""Connectors (UiPath Integration Service): Microsoft Graph (mail, OneDrive/SharePoint files), Teams / Slack webhooks, Jira, generic OAuth2.

Thin, hardened wrappers over :mod:`wrapper_core.activities.http`; every failure is one of the platform's classified exceptions
(AuthExpired / SystemUnavailable / NetworkError / DataError), so ``wrapper.run_item`` and the retry engine handle them as usual.

    from wrapper_core.activities import connectors
    g = connectors.Graph("graph-app", tenant="contoso.onmicrosoft.com")          # credential "graph-app": user = client id, password = client secret
    g.send_mail("robot@contoso.com", ["ap@contoso.com"], "Daily report", "All good.")
    g.upload_file("out/report.xlsx", "Reports/2026/report.xlsx", user="robot@contoso.com")
    connectors.slack_post("slack-webhook", "Nightly run finished")                 # credential value = the webhook URL
    connectors.Jira("https://contoso.atlassian.net", "jira-robot").create_issue("OPS", "Portal down", "Login page returns 503")

Rules every connector enforces
* Secrets come ONLY from the credentials API (Windows Credential Manager; see ``activities.credentials``). There is no parameter that accepts a token, client
  secret, password or webhook URL as a plain argument, so none can arrive through slot parameters, the manifest or the environment by accident.
* https only, to an allow-listed host (exact names or ``*.suffix``). Defaults: Graph ``graph.microsoft.com`` + ``login.microsoftonline.com``; Teams
  ``*.webhook.office.com`` / ``outlook.office.com`` / ``*.logic.azure.com`` / ``*.powerplatform.com``; Slack ``hooks.slack.com``; Jira ``*.atlassian.net``. Extra hosts
  (sovereign clouds, Jira Data Center) must be passed explicitly in ``allowed_hosts``. Cross-origin redirects drop credentials (the http client's rule).
* Webhook URLs are secrets: the http client runs with ``redact_path`` so they never reach telemetry or error text.
* Retries: the http client retries idempotent calls (GET/PUT/DELETE) on 5xx/timeouts and every call on 429, honouring ``Retry-After`` (capped at 60 s).
  POST is never retried after a 5xx (a retried "send mail" could send twice) - let ``run_item``/your own logic decide.
* Response bodies are capped (50 MB, ``download`` streams with a size limit), inputs are validated, texts are length-capped, and path segments are quoted.
* ``allow_test_hosts=True`` (or ``AMX_ALLOW_TEST_HOSTS=1``) additionally accepts plain http to loopback addresses - for tests against a fake server only.
"""
from __future__ import annotations

import os
import re
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..retry import AuthExpired, DataError
from . import credentials, http
from ._common import emit

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_HOSTS = ("graph.microsoft.com", "login.microsoftonline.com")
TEAMS_HOSTS = ("*.webhook.office.com", "outlook.office.com", "*.logic.azure.com", "*.powerplatform.com")
SLACK_HOSTS = ("hooks.slack.com",)
JIRA_HOSTS = ("*.atlassian.net",)
MAX_SIMPLE_UPLOAD = 4 * 1024 * 1024
MAX_DOWNLOAD = 500 * 1024 * 1024
_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\Z")
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")
_GUID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z")
_DRIVE_ID = re.compile(r"^[A-Za-z0-9!_.,-]{1,256}\Z")
_JIRA_PROJECT = re.compile(r"^[A-Z][A-Z0-9_]{1,19}\Z")
_JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,19}-\d{1,9}\Z")


# --- guards -----------------------------------------------------------------------------------------------
def _test_hosts(flag: bool) -> bool:
    return bool(flag) or os.environ.get("AMX_ALLOW_TEST_HOSTS") == "1"


def _host_allowed(host: str, allowed: Iterable[str]) -> bool:
    h = host.lower().rstrip(".")
    for a in allowed:
        a = a.lower()
        if (a.startswith("*.") and h.endswith(a[1:]) and len(h) > len(a) - 1) or h == a:
            return True
    return False


def check_url(url: str, allowed_hosts: Iterable[str], allow_test_hosts: bool = False) -> str:
    """Return ``url`` if it is https (or loopback http in test mode), has no credentials/odd port, and its host is allow-listed; else DataError."""
    p = urllib.parse.urlsplit(str(url))
    host = (p.hostname or "").lower()
    if not host or p.username or p.password or any(ord(c) < 33 or c == chr(92) for c in str(url)):
        raise DataError("connector URL is malformed or contains credentials")
    if _test_hosts(allow_test_hosts) and host in ("127.0.0.1", "localhost", "::1") and p.scheme in ("http", "https"):
        return str(url)
    if p.scheme != "https":
        raise DataError("connectors only talk https")
    if p.port not in (None, 443):
        raise DataError("connector URL must use the default https port")
    if not _host_allowed(host, allowed_hosts):
        raise DataError(f"host {host!r} is not allowed for this connector (allowed: {', '.join(allowed_hosts)})")
    return str(url)


def _text(value: Any, limit: int, what: str, single_line: bool = False, required: bool = False) -> str:
    s = "" if value is None else str(value)
    if single_line:
        s = " ".join(s.replace("\r", " ").replace("\n", " ").split())
    s = "".join(ch for ch in s if ch in "\t\n\r" or ord(ch) >= 32)
    if required and not s.strip():
        raise ValueError(f"{what} is required")
    if len(s) > limit:
        raise ValueError(f"{what} is longer than {limit} characters")
    return s


def _emails(value: Any, what: str, required: bool = True) -> List[str]:
    items = [value] if isinstance(value, str) else list(value or [])
    if required and not items:
        raise ValueError(f"{what}: at least one address is required")
    if len(items) > 100:
        raise ValueError(f"{what}: at most 100 recipients")
    for a in items:
        if not isinstance(a, str) or not _EMAIL.fullmatch(a):
            raise ValueError(f"{what}: {str(a)[:60]!r} is not a valid e-mail address")
    return items


def _quote_path(remote: str) -> str:
    """Drive-relative path -> URL-quoted, rejecting traversal and odd characters."""
    if not isinstance(remote, str) or len(remote) > 400 or chr(92) in remote or any(ord(c) < 32 for c in remote):
        raise DataError("remote path is invalid")
    parts = [p for p in remote.strip("/").split("/") if p != ""]
    if any(p in (".", "..") for p in parts):
        raise DataError("remote path must not contain '.' or '..' segments")
    return "/".join(urllib.parse.quote(p, safe="") for p in parts)


# --- OAuth2 (client credentials) -----------------------------------------------------------------------------
_cache: Dict[Tuple[str, str, str, str], http.OAuth2ClientCredentials] = {}
_cache_lock = threading.Lock()


def oauth2_auth(token_url: str, credential: str, scope: Optional[str] = None, audience: Optional[str] = None,
                allowed_hosts: Sequence[str] = (), allow_test_hosts: bool = False) -> http.OAuth2ClientCredentials:
    """OAuth2 client-credentials auth object (usable as ``auth=`` of an ``http.Client``). The client id/secret are read from the stored credential
    ``credential`` (user = client id, password = client secret). ``allowed_hosts`` is REQUIRED: the secret is only ever sent to those token hosts.
    Tokens are cached in memory per (token_url, credential, scope, audience) and refreshed a minute before expiry."""
    if not allowed_hosts:
        raise ValueError("oauth2: allowed_hosts is required (the hosts the client secret may be sent to)")
    check_url(token_url, allowed_hosts, allow_test_hosts)
    key = (token_url, credential, scope or "", audience or "")
    with _cache_lock:
        if key not in _cache:
            cred = credentials.get_credential(credential)
            _cache[key] = http.OAuth2ClientCredentials(token_url, cred.username, str(cred.password), scope=scope, audience=audience)
        return _cache[key]


def oauth2_token(token_url: str, credential: str, scope: Optional[str] = None, audience: Optional[str] = None,
                 allowed_hosts: Sequence[str] = (), allow_test_hosts: bool = False) -> str:
    """Fetch (or reuse the cached) access token. Treat the return value as a secret: never log it."""
    emit("connectors.oauth2_token", host=urllib.parse.urlsplit(token_url).netloc)
    return oauth2_auth(token_url, credential, scope, audience, allowed_hosts, allow_test_hosts).token()


def clear_token_cache() -> None:
    with _cache_lock:
        _cache.clear()


# --- Microsoft Graph --------------------------------------------------------------------------------------------
class Graph:
    """Microsoft Graph with app-only (client credentials) auth. Needs the matching application permissions (Mail.Send, Files.ReadWrite.All, ...)."""

    def __init__(self, credential: str, tenant: str, scope: str = GRAPH_SCOPE, base_url: str = GRAPH_BASE, token_url: Optional[str] = None,
                 allowed_hosts: Sequence[str] = (), allow_test_hosts: bool = False, client: Optional[http.Client] = None):
        if not (_TENANT.fullmatch(tenant or "") or _GUID.fullmatch(tenant or "")):
            raise ValueError("tenant must be a tenant id (GUID) or domain name")
        self.allowed = tuple(GRAPH_HOSTS) + tuple(allowed_hosts)
        self.allow_test = allow_test_hosts
        self.base_url = check_url(base_url, self.allowed, allow_test_hosts).rstrip("/")
        token_url = token_url or f"https://login.microsoftonline.com/{urllib.parse.quote(tenant, safe='.-')}/oauth2/v2.0/token"
        auth = oauth2_auth(token_url, credential, scope, None, self.allowed, allow_test_hosts)
        self.client = client or http.Client(self.base_url, auth=auth, retries=3)

    # -- helpers
    @staticmethod
    def _owner(user: Optional[str], drive_id: Optional[str]) -> str:
        if bool(user) == bool(drive_id):
            raise ValueError("give exactly one of user (a mailbox/UPN or id) or drive_id")
        if drive_id:
            if not _DRIVE_ID.fullmatch(drive_id) or set(drive_id) <= {"."}:  # "." and ".." would walk the Graph URL
                raise ValueError("drive_id has invalid characters")
            return f"/drives/{urllib.parse.quote(drive_id, safe='')}"
        if not (_EMAIL.fullmatch(user or "") or _GUID.fullmatch(user or "")):
            raise ValueError("user must be an e-mail/UPN or a GUID")
        return f"/users/{urllib.parse.quote(user, safe='@')}/drive"

    # -- mail
    def send_mail(self, user: str, to: Any, subject: str, body: str, html: bool = False, cc: Any = None, save_to_sent: bool = True) -> None:
        """Send as ``user`` (needs Mail.Send application permission). One POST, never auto-retried after a 5xx; retried on 429."""
        if not (_EMAIL.fullmatch(user or "") or _GUID.fullmatch(user or "")):
            raise ValueError("user must be an e-mail/UPN or a GUID")
        msg = {"subject": _text(subject, 255, "subject", single_line=True, required=True),
               "body": {"contentType": "HTML" if html else "Text", "content": _text(body, 500_000, "body")},
               "toRecipients": [{"emailAddress": {"address": a}} for a in _emails(to, "to")]}
        if cc:
            msg["ccRecipients"] = [{"emailAddress": {"address": a}} for a in _emails(cc, "cc", required=False)]
        self.client.post(f"/users/{urllib.parse.quote(user, safe='@')}/sendMail", json={"message": msg, "saveToSentItems": bool(save_to_sent)})
        emit("connectors.graph.send_mail", recipients=len(msg["toRecipients"]))

    # -- files (OneDrive / SharePoint document libraries)
    def list_files(self, folder_path: str = "", user: Optional[str] = None, drive_id: Optional[str] = None, max_items: int = 1000) -> List[Dict[str, Any]]:
        owner = self._owner(user, drive_id)
        folder = _quote_path(folder_path)
        path = f"{owner}/root:/{folder}:/children" if folder else f"{owner}/root/children"
        out: List[Dict[str, Any]] = []
        url: Optional[str] = path
        params: Optional[Dict[str, Any]] = {"$top": 200}
        pages = 0
        while url and len(out) < max_items and pages < 50:
            data = self.client.get_json(url, params=params)
            for it in data.get("value", []):
                out.append({"id": it.get("id"), "name": it.get("name"), "size": it.get("size"), "folder": "folder" in it,
                            "modified": it.get("lastModifiedDateTime")})
            nxt = data.get("@odata.nextLink")
            url, params, pages = (check_url(nxt, self.allowed, self.allow_test) if nxt else None), None, pages + 1
        emit("connectors.graph.list_files", count=len(out))
        return out[:max_items]

    def get_file(self, remote_path: str, dest: "str | os.PathLike", user: Optional[str] = None, drive_id: Optional[str] = None,
                 max_bytes: int = MAX_DOWNLOAD) -> str:
        """Download a file (the content redirect goes to a pre-authenticated storage URL; credentials are not forwarded to it)."""
        owner = self._owner(user, drive_id)
        saved = self.client.download(f"{owner}/root:/{_quote_path(remote_path)}:/content", dest, max_bytes=min(int(max_bytes), MAX_DOWNLOAD))
        emit("connectors.graph.get_file")
        return saved

    def upload_file(self, local_path: "str | os.PathLike", remote_path: str, user: Optional[str] = None, drive_id: Optional[str] = None,
                    conflict: str = "replace") -> Dict[str, Any]:
        """Upload up to 4 MB with one PUT (larger files need Graph upload sessions: not supported here, DataError says so)."""
        if conflict not in ("replace", "rename", "fail"):
            raise ValueError("conflict must be replace, rename or fail")
        p = Path(local_path)
        if not p.is_file():
            raise DataError(f"local file not found: {p.name}")
        size = p.stat().st_size
        if size > MAX_SIMPLE_UPLOAD:
            raise DataError(f"{p.name} is {size} bytes; simple upload is limited to {MAX_SIMPLE_UPLOAD} (use an upload session for larger files)")
        owner = self._owner(user, drive_id)
        r = self.client.put(f"{owner}/root:/{_quote_path(remote_path)}:/content", data=p.read_bytes(),
                            params={"@microsoft.graph.conflictBehavior": conflict}, headers={"Content-Type": "application/octet-stream"})
        emit("connectors.graph.upload_file", bytes=size)
        j = r.json() if r.content else {}
        return {"id": j.get("id"), "name": j.get("name"), "size": j.get("size")}


# --- webhooks (Teams, Slack) ---------------------------------------------------------------------------------------
def _webhook(credential: str, allowed: Sequence[str], payload: Dict[str, Any], allow_test_hosts: bool, label: str) -> None:
    url = check_url(credentials.get_raw(credential).strip(), allowed, allow_test_hosts)
    # the URL path is the secret: redact_path keeps it out of telemetry and error messages
    resp = http.Client(redact_path=True, retries=3).post(url, json=payload)
    emit(f"connectors.{label}", status=resp.status)


def teams_post(credential: str, text: str, title: Optional[str] = None, allowed_hosts: Sequence[str] = (), allow_test_hosts: bool = False) -> None:
    """Post a message card to a Teams channel via an incoming-webhook / Workflows URL stored as credential ``credential`` (text <= 20,000 chars)."""
    card = {"@type": "MessageCard", "@context": "https://schema.org/extensions", "text": _text(text, 20_000, "text", required=True)}
    if title:
        card["title"] = _text(title, 200, "title", single_line=True)
    _webhook(credential, tuple(TEAMS_HOSTS) + tuple(allowed_hosts), card, allow_test_hosts, "teams_post")


def slack_post(credential: str, text: str, allowed_hosts: Sequence[str] = (), allow_test_hosts: bool = False) -> None:
    """Post to Slack via an incoming-webhook URL stored as credential ``credential`` (text <= 20,000 chars; ``mrkdwn`` links are not auto-expanded)."""
    _webhook(credential, tuple(SLACK_HOSTS) + tuple(allowed_hosts), {"text": _text(text, 20_000, "text", required=True), "link_names": 0},
             allow_test_hosts, "slack_post")


# --- Jira ------------------------------------------------------------------------------------------------------------
class Jira:
    """Jira Cloud (REST v2) with basic auth from credential ``credential`` (user = account e-mail, password = API token)."""

    def __init__(self, base_url: str, credential: str, allowed_hosts: Sequence[str] = (), allow_test_hosts: bool = False,
                 client: Optional[http.Client] = None):
        self.base_url = check_url(base_url, tuple(JIRA_HOSTS) + tuple(allowed_hosts), allow_test_hosts).rstrip("/")
        cred = credentials.get_credential(credential)
        self.client = client or http.Client(self.base_url, auth=http.BasicAuth(cred.username, str(cred.password)), retries=3)

    def create_issue(self, project: str, summary: str, description: str = "", issue_type: str = "Task", labels: Optional[Sequence[str]] = None) -> Dict[str, str]:
        if not _JIRA_PROJECT.fullmatch(project or ""):
            raise ValueError("project must be a Jira project key such as OPS")
        fields: Dict[str, Any] = {"project": {"key": project}, "summary": _text(summary, 255, "summary", single_line=True, required=True),
                                  "description": _text(description, 30_000, "description"),
                                  "issuetype": {"name": _text(issue_type, 60, "issue_type", single_line=True, required=True)}}
        if labels:
            ls = list(labels)
            if len(ls) > 20 or any(not isinstance(x, str) or not re.fullmatch(r"[^\s]{1,255}", x) for x in ls):
                raise ValueError("labels: at most 20, each 1-255 characters without spaces")
            fields["labels"] = ls
        j = self.client.post("/rest/api/2/issue", json={"fields": fields}).json()
        emit("connectors.jira.create_issue")
        return {"key": str(j.get("key", "")), "id": str(j.get("id", ""))}

    def transition_issue(self, key: str, transition: str, comment: Optional[str] = None) -> None:
        """Move ``key`` through a workflow transition given by name or id; DataError lists what is possible if it is not available."""
        if not _JIRA_KEY.fullmatch(key or ""):
            raise ValueError("key must look like OPS-123")
        want = _text(transition, 100, "transition", single_line=True, required=True).lower()
        avail = self.client.get_json(f"/rest/api/2/issue/{key}/transitions").get("transitions", [])
        match = next((t for t in avail if str(t.get("id")) == want or str(t.get("name", "")).lower() == want), None)
        if match is None:
            names = ", ".join(str(t.get("name")) for t in avail[:10])
            raise DataError(f"transition {transition!r} is not available for {key} (available: {names or 'none'})")
        body: Dict[str, Any] = {"transition": {"id": str(match["id"])}}
        if comment:
            body["update"] = {"comment": [{"add": {"body": _text(comment, 30_000, "comment")}}]}
        self.client.post(f"/rest/api/2/issue/{key}/transitions", json=body)
        emit("connectors.jira.transition_issue")
