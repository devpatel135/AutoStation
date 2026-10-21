"""Mail activities (UiPath: Send SMTP Mail Message, Get IMAP Mail Messages, Send/Get Outlook Mail Message).

* ``send_smtp`` / ``read_imap``: stdlib smtplib / imaplib, TLS aware, attachments saved with sanitised file names.
* ``outlook_send`` / ``outlook_read``: the desktop Outlook COM object through Windows PowerShell (acts as the signed-in user;
  needs Outlook installed and a profile). They raise ``NotAvailable`` off Windows or without Outlook.

Errors are classified for the retry engine: bad credentials -> AuthExpired; connection problems -> NetworkError; 4xx SMTP replies ->
SystemUnavailable; rejected recipients / malformed input -> DataError. Message bodies, addresses and passwords are never logged
(only counts and sizes).
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import mimetypes
import os
import re
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import make_msgid, parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from ..retry import AuthExpired, DataError, NetworkError, SystemUnavailable
from ._common import ActivityError, NotAvailable, emit, powershell_json, require_windows

Addresses = Union[str, Sequence[str]]
MAX_ATTACHMENT = 50 * 1024 * 1024


def _addrs(v: Optional[Addresses]) -> List[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [a.strip() for a in re.split(r"[;,]", v) if a.strip()]
    return [str(a).strip() for a in v if str(a).strip()]


def _resolve_login(username, password, credential):
    if credential:
        from .credentials import get_credential
        c = get_credential(credential)
        return c.username, str(c.password)
    return username, (str(password) if password is not None else None)


_RESERVED_NAME = re.compile(r"^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9]|LPT[0-9])(\..*)?$", re.I)


def safe_filename(name: str, default: str = "attachment") -> str:
    """File name for an attachment received from the outside: no directories, no characters Windows forbids, no trailing
    dots/spaces, and never a DOS device name (``NUL``, ``COM1``, ``CON.txt`` would write to the device, not a file)."""
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")[:150].rstrip(". ")
    if not name:
        return default
    return "_" + name if _RESERVED_NAME.match(name) else name


def _unique(directory: Path, name: str) -> Path:
    p = directory / name
    stem, suffix, n = p.stem, p.suffix, 1
    while p.exists():
        p = directory / f"{stem} ({n}){suffix}"
        n += 1
    return p


# --- SMTP -----------------------------------------------------------------------------------------------
def build_message(sender: str, to: Addresses, subject: str, body: str = "", html: Optional[str] = None,
                  attachments: Iterable["str | os.PathLike"] = (), cc: Optional[Addresses] = None, reply_to: Optional[str] = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(_addrs(to)), subject
    if _addrs(cc):
        msg["Cc"] = ", ".join(_addrs(cc))
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Message-ID"] = make_msgid(domain=(sender.rpartition("@")[2].strip("<> ") or socket.gethostname()))  # default does reverse DNS
    msg.set_content(body or "")
    if html:
        msg.add_alternative(html, subtype="html")
    for a in attachments:
        p = Path(a)
        if not p.is_file():
            raise DataError(f"attachment not found: {p.name}")
        if p.stat().st_size > MAX_ATTACHMENT:
            raise DataError(f"attachment too large: {p.name}")
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    return msg


def send_smtp(host: str, port: int = 587, sender: Optional[str] = None, to: Addresses = (), subject: str = "", body: str = "",
              html: Optional[str] = None, attachments: Iterable["str | os.PathLike"] = (), username: Optional[str] = None,
              password: Optional[str] = None, credential: Optional[str] = None, tls: Optional[str] = "starttls",
              cc: Optional[Addresses] = None, bcc: Optional[Addresses] = None, timeout: float = 30,
              helo_name: Optional[str] = None) -> str:
    """Send a message; returns its Message-ID. ``tls``: ``"starttls"`` (default), ``"ssl"`` or ``None`` (plain, for internal relays).
    Credentials may be given as `username`/`password` or by Credential Manager name via `credential`."""
    attachments = list(attachments)
    n_attachments = len(attachments)
    rcpt = _addrs(to) + _addrs(cc) + _addrs(bcc)
    if not rcpt:
        raise DataError("no recipients")
    if not sender:
        raise DataError("sender is required")
    msg = build_message(sender, to, subject, body, html, attachments, cc)
    user, pwd = _resolve_login(username, password, credential)
    helo = helo_name or socket.gethostname()  # smtplib's default (getfqdn) does reverse-DNS lookups that can take seconds
    try:
        if tls == "ssl":
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(host, port, local_hostname=helo, timeout=timeout, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(host, port, local_hostname=helo, timeout=timeout)
        with smtp:
            smtp.ehlo()
            if tls == "starttls":
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if user:
                smtp.login(user, pwd or "")
            refused = smtp.send_message(msg, to_addrs=rcpt)
        if refused:
            raise DataError(f"{len(refused)} recipient(s) refused by the server")
    except smtplib.SMTPAuthenticationError as exc:
        raise AuthExpired(f"SMTP authentication failed ({exc.smtp_code})") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise DataError("all recipients were refused by the server") from exc
    except smtplib.SMTPResponseException as exc:
        if 400 <= exc.smtp_code < 500:
            raise SystemUnavailable(f"SMTP temporary failure {exc.smtp_code}") from exc
        raise DataError(f"SMTP rejected the message ({exc.smtp_code})") from exc
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, ConnectionError, socket.timeout, socket.gaierror, ssl.SSLError, OSError) as exc:
        raise NetworkError(f"SMTP connection problem: {type(exc).__name__}") from exc
    emit("mail.send_smtp", recipients=len(rcpt), attachments=n_attachments)
    return msg["Message-ID"]


# --- IMAP -----------------------------------------------------------------------------------------------
def _parse_message(raw: bytes, save_to: Optional[Path]) -> Dict[str, Any]:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body_text = body_html = ""
    try:
        t = msg.get_body(preferencelist=("plain",))
        h = msg.get_body(preferencelist=("html",))
        body_text = t.get_content() if t is not None else ""
        body_html = h.get_content() if h is not None else ""
    except (LookupError, ValueError):
        body_text = ""
    atts = []
    for part in msg.iter_attachments():
        fname = safe_filename(part.get_filename() or "")
        data = part.get_payload(decode=True) or b""
        if len(data) > MAX_ATTACHMENT:
            atts.append({"filename": fname, "size": len(data), "path": None, "skipped": "too large"})
            continue
        entry: Dict[str, Any] = {"filename": fname, "size": len(data), "content_type": part.get_content_type(), "path": None}
        if save_to is not None:
            save_to.mkdir(parents=True, exist_ok=True)
            dest = _unique(save_to, fname)
            dest.write_bytes(data)
            entry["path"] = str(dest)
        atts.append(entry)
    date = None
    try:
        date = parsedate_to_datetime(str(msg["Date"])).isoformat() if msg["Date"] else None
    except (TypeError, ValueError):
        pass
    return {"message_id": str(msg["Message-ID"] or ""), "subject": str(msg["Subject"] or ""), "from": str(msg["From"] or ""),
            "to": str(msg["To"] or ""), "cc": str(msg["Cc"] or ""), "date": date, "body_text": body_text, "body_html": body_html,
            "attachments": atts}


def read_imap(host: str, username: Optional[str] = None, password: Optional[str] = None, credential: Optional[str] = None,
              port: int = 993, folder: str = "INBOX", search: str = "UNSEEN", limit: int = 50, mark_seen: bool = False,
              save_attachments_to: "str | os.PathLike | None" = None, ssl_: bool = True, timeout: float = 30) -> List[Dict[str, Any]]:
    """Return up to `limit` messages (oldest first) matching the IMAP `search` string (e.g. ``UNSEEN``, ``FROM "a@b.c" SINCE 01-Oct-2026``).
    Messages are fetched with BODY.PEEK (not marked read) unless ``mark_seen=True``."""
    user, pwd = _resolve_login(username, password, credential)
    save_to = Path(save_attachments_to) if save_attachments_to else None
    out: List[Dict[str, Any]] = []
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=timeout, ssl_context=ssl.create_default_context()) if ssl_ else imaplib.IMAP4(host, port, timeout=timeout)
        try:
            conn.login(user or "", pwd or "")
            typ, _ = conn.select(folder, readonly=not mark_seen)
            if typ != "OK":
                raise DataError(f"cannot open folder {folder!r}")
            typ, data = conn.uid("SEARCH", None, search)
            if typ != "OK":
                raise DataError("IMAP search failed")
            uids = data[0].split()[:limit] if data and data[0] else []
            for uid in uids:
                typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                rec = _parse_message(parts[0][1], save_to)
                rec["uid"] = uid.decode()
                out.append(rec)
                if mark_seen:
                    conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
    except imaplib.IMAP4.error as exc:
        text = str(exc)
        if re.search(r"auth|login|credentials|password", text, re.I):
            raise AuthExpired("IMAP authentication failed") from exc
        raise DataError(f"IMAP error: {text[:150]}") from exc
    except (ConnectionError, socket.timeout, socket.gaierror, ssl.SSLError, OSError, imaplib.IMAP4.abort) as exc:
        raise NetworkError(f"IMAP connection problem: {type(exc).__name__}") from exc
    emit("mail.read_imap", messages=len(out), attachments=sum(len(m["attachments"]) for m in out))
    return out


# --- Outlook (COM through PowerShell) ---------------------------------------------------------------------
_OUTLOOK_COMMON = r"""
try { $ol = New-Object -ComObject Outlook.Application } catch { Write-Error 'AMX_NO_OUTLOOK: Outlook is not installed or not accessible'; exit 3 }
"""

OUTLOOK_SEND_SCRIPT = _OUTLOOK_COMMON + r"""
$m = $ol.CreateItem(0)
$m.To = ($A.to -join ';')
if ($A.cc) { $m.CC = ($A.cc -join ';') }
$m.Subject = [string]$A.subject
if ($A.html) { $m.HTMLBody = [string]$A.body } else { $m.Body = [string]$A.body }
foreach ($f in @($A.attachments)) { if ($f) { [void]$m.Attachments.Add([string]$f) } }
if ($A.draft) { $m.Save() } else { $m.Send() }
ConvertTo-Json -Compress -InputObject @{ ok = $true; draft = [bool]$A.draft }
"""

OUTLOOK_READ_SCRIPT = _OUTLOOK_COMMON + r"""
$ns = $ol.GetNamespace('MAPI')
$ids = @{ inbox = 6; outbox = 4; sent = 5; drafts = 16; deleted = 3 }
$parts = @(([string]$A.folder) -split '/' | Where-Object { $_ })
$first = $parts[0].ToLower()
if ($ids.ContainsKey($first)) { $folder = $ns.GetDefaultFolder($ids[$first]); $rest = @($parts | Select-Object -Skip 1) }
else { $folder = $ns.GetDefaultFolder(6); $rest = $parts }
foreach ($p in $rest) { $folder = $folder.Folders.Item($p) }
$items = $folder.Items
if ($A.unread_only) { $items = $items.Restrict('[UnRead] = true') }
$items.Sort('[ReceivedTime]', $false)
$bad = [IO.Path]::GetInvalidFileNameChars()
$out = New-Object System.Collections.ArrayList
$it = $items.GetFirst()
while ($it -ne $null -and $out.Count -lt [int]$A.limit) {
    if ($it.Class -eq 43) {
        $atts = New-Object System.Collections.ArrayList
        foreach ($att in $it.Attachments) {
            $name = [string]$att.FileName
            foreach ($c in $bad) { $name = $name.Replace([string]$c, '_') }
            $name = $name.Trim().TrimEnd('.', ' ')
            if ($name.Length -gt 150) { $name = $name.Substring(0, 150).TrimEnd('.', ' ') }
            if ($name -eq '') { $name = 'attachment' }
            if ($name -match '^(?i)(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9]|LPT[0-9])(\..*)?$') { $name = '_' + $name }
            $path = $null
            if ($A.save_to) {
                [void](New-Item -ItemType Directory -Force -Path $A.save_to)
                $path = [IO.Path]::Combine([string]$A.save_to, $name)
                $n = 1
                while (Test-Path -LiteralPath $path) { $path = [IO.Path]::Combine([string]$A.save_to, ('{0} ({1}){2}' -f [IO.Path]::GetFileNameWithoutExtension($name), $n, [IO.Path]::GetExtension($name))); $n++ }
                $att.SaveAsFile($path)
            }
            [void]$atts.Add([pscustomobject]@{ filename = $name; size = [int]$att.Size; path = $path })
        }
        $sender = [string]$it.SenderEmailAddress
        try { if ($it.SenderEmailType -eq 'EX') { $sender = [string]$it.Sender.GetExchangeUser().PrimarySmtpAddress } } catch { }
        [void]$out.Add([pscustomobject]@{
            entry_id = [string]$it.EntryID; subject = [string]$it.Subject; sender = $sender; to = [string]$it.To; cc = [string]$it.CC
            received = ([datetime]$it.ReceivedTime).ToString('o'); unread = [bool]$it.UnRead
            body_text = [string]$it.Body; body_html = [string]$it.HTMLBody; attachments = @($atts)
        })
        if ($A.mark_read) { $it.UnRead = $false; $it.Save() }
    }
    $it = $items.GetNext()
}
ConvertTo-Json -InputObject @($out) -Compress -Depth 5
"""


def build_outlook_send_args(to: Addresses, subject: str, body: str, html: bool = False, cc: Optional[Addresses] = None,
                            attachments: Iterable["str | os.PathLike"] = (), draft: bool = False) -> Dict[str, Any]:
    atts = []
    for a in attachments:
        p = Path(a)
        if not p.is_file():
            raise DataError(f"attachment not found: {p.name}")
        atts.append(str(p.resolve()))
    if not _addrs(to):
        raise DataError("no recipients")
    return {"to": _addrs(to), "cc": _addrs(cc), "subject": subject, "body": body, "html": bool(html), "attachments": atts, "draft": bool(draft)}


def build_outlook_read_args(folder: str = "Inbox", unread_only: bool = True, limit: int = 50, save_to: "str | os.PathLike | None" = None,
                            mark_read: bool = False) -> Dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return {"folder": folder, "unread_only": bool(unread_only), "limit": int(limit),
            "save_to": str(Path(save_to).resolve()) if save_to else None, "mark_read": bool(mark_read)}


def _outlook(script: str, args: Dict[str, Any], timeout: float) -> Any:
    require_windows("Outlook automation")
    try:
        return powershell_json(script, args, timeout=timeout, what="Outlook")
    except ActivityError as exc:
        if "AMX_NO_OUTLOOK" in str(exc) or "80040154" in str(exc) or "class not registered" in str(exc).lower():
            raise NotAvailable("Outlook is not installed or has no profile for this user") from exc
        raise


def outlook_send(to: Addresses, subject: str, body: str = "", html: bool = False, cc: Optional[Addresses] = None,
                 attachments: Iterable["str | os.PathLike"] = (), draft: bool = False, timeout: float = 120) -> None:
    """Send (or save as draft) through the signed-in user's Outlook. ``html=True`` treats `body` as HTML."""
    _outlook(OUTLOOK_SEND_SCRIPT, build_outlook_send_args(to, subject, body, html, cc, attachments, draft), timeout)
    emit("mail.outlook_send", recipients=len(_addrs(to)), draft=draft)


def outlook_read(folder: str = "Inbox", unread_only: bool = True, limit: int = 50, save_attachments_to: "str | os.PathLike | None" = None,
                 mark_read: bool = False, timeout: float = 300) -> List[Dict[str, Any]]:
    """Read mail (oldest first) from `folder` (``Inbox``, ``Inbox/Invoices``, ``Sent``...). Returns plain dicts."""
    rows = _outlook(OUTLOOK_READ_SCRIPT, build_outlook_read_args(folder, unread_only, limit, save_attachments_to, mark_read), timeout) or []
    emit("mail.outlook_read", messages=len(rows))
    return rows
