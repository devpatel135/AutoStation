from __future__ import annotations

import base64
import email
import email.policy
import json
import socketserver
import sys
import threading
from email.message import EmailMessage

import pytest

from wrapper_core import retry, runctx
from wrapper_core.activities import mail
from wrapper_core.activities._common import ActivityError, NotAvailable
from wrapper_core.retry import AuthExpired, DataError, NetworkError, SystemUnavailable


# --- fake SMTP server --------------------------------------------------------------------------------------
class FakeSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _SMTPHandler)
        self.messages = []
        self.user, self.password = "robot", "s3cret"
        self.data_reply = b"250 queued\r\n"
        self.refuse = ("refused",)
        threading.Thread(target=self.serve_forever, daemon=True).start()


class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        srv, w, r = self.server, self.wfile.write, self.rfile.readline
        w(b"220 fake ESMTP\r\n")
        sender, rcpts = None, []
        while True:
            line = r()
            if not line:
                return
            cmd = line.decode().strip()
            up = cmd.upper()
            if up.startswith("EHLO"):
                w(b"250-fake\r\n250-AUTH PLAIN\r\n250 8BITMIME\r\n")
            elif up.startswith("HELO"):
                w(b"250 fake\r\n")
            elif up.startswith("AUTH PLAIN"):
                parts = base64.b64decode(cmd.split()[2]).split(b"\0")
                ok = parts[1].decode() == srv.user and parts[2].decode() == srv.password
                w(b"235 ok\r\n" if ok else b"535 5.7.8 bad credentials\r\n")
            elif up.startswith("MAIL FROM"):
                sender, rcpts = cmd[10:].strip(" <>"), []
                w(b"250 ok\r\n")
            elif up.startswith("RCPT TO"):
                addr = cmd[8:].strip(" <>")
                if any(x in addr for x in srv.refuse):
                    w(b"550 no such user\r\n")
                else:
                    rcpts.append(addr)
                    w(b"250 ok\r\n")
            elif up == "DATA":
                w(b"354 go\r\n")
                buf = b""
                while True:
                    ln = r()
                    if ln == b".\r\n" or not ln:
                        break
                    buf += ln[1:] if ln.startswith(b"..") else ln
                if srv.data_reply.startswith(b"250"):
                    srv.messages.append((sender, list(rcpts), buf))
                w(srv.data_reply)
            elif up in ("RSET", "NOOP"):
                w(b"250 ok\r\n")
            elif up == "QUIT":
                w(b"221 bye\r\n")
                return


@pytest.fixture
def smtp():
    s = FakeSMTP()
    yield s
    s.shutdown()
    s.server_close()


def send(smtp, **kw):
    args = dict(host="127.0.0.1", port=smtp.server_address[1], sender="robot@x.test", to=["a@x.test"], subject="Hello", body="body",
                tls=None, timeout=5)
    args.update(kw)
    return mail.send_smtp(**args)


def test_send_smtp_with_attachment_html_cc_bcc(smtp, tmp_path):
    f = tmp_path / "report.csv"
    f.write_text("a,b\n1,2\n")
    mid = send(smtp, to="a@x.test; b@x.test", cc="c@x.test", bcc=["hidden@x.test"], html="<b>hi</b>", attachments=[f],
               username="robot", password="s3cret", subject="Ünïcode ✓")
    sender, rcpts, raw = smtp.messages[0]
    assert sender == "robot@x.test" and rcpts == ["a@x.test", "b@x.test", "c@x.test", "hidden@x.test"]
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert msg["Subject"] == "Ünïcode ✓" and msg["Message-ID"] == mid and "hidden@x.test" not in str(msg["To"]) + str(msg["Cc"])
    assert "hi" in msg.get_body(preferencelist=("html",)).get_content()
    att = list(msg.iter_attachments())[0]
    assert att.get_filename() == "report.csv" and att.get_payload(decode=True).startswith(b"a,b")
    blob = json.dumps(runctx.current().read_events())
    assert "a@x.test" not in blob and "s3cret" not in blob and "Ünïcode" not in blob      # no addresses / subjects / secrets in telemetry
    assert [e for e in runctx.current().read_events() if e["data"].get("activity") == "mail.send_smtp"][0]["data"]["recipients"] == 4


def test_send_smtp_error_classification(smtp, tmp_path):
    with pytest.raises(AuthExpired):
        send(smtp, username="robot", password="WRONG")
    with pytest.raises(DataError):
        send(smtp, to=["x@refused.test"])                                 # every recipient refused
    with pytest.raises(DataError):
        send(smtp, to=["ok@x.test", "y@refused.test"])                    # partial refusal is surfaced too
    smtp.data_reply = b"451 4.3.0 try again later\r\n"
    with pytest.raises(SystemUnavailable):
        send(smtp)
    smtp.data_reply = b"554 5.7.1 rejected as spam\r\n"
    with pytest.raises(DataError):
        send(smtp)
    with pytest.raises(NetworkError):
        mail.send_smtp("127.0.0.1", 1, "a@x.test", "b@x.test", "s", tls=None, timeout=2)
    with pytest.raises(DataError):
        send(smtp, to=[])
    with pytest.raises(DataError):
        send(smtp, attachments=[tmp_path / "missing.bin"])
    assert retry.classify_exception(AuthExpired("x")) is retry.Category.AUTH_EXPIRED


def test_send_smtp_credential_by_name(smtp, monkeypatch):
    monkeypatch.setattr("wrapper_core.secrets.credman_read", lambda t: "robot:s3cret" if t == "AUTOMAGIX/cred/smtp" else None)
    send(smtp, credential="smtp")
    assert len(smtp.messages) == 1


# --- fake IMAP server ---------------------------------------------------------------------------------------
def make_mail(subject, body, attachments=(), msgid=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = "sender@x.test", "robot@x.test", subject, "Mon, 05 Oct 2026 10:00:00 +0000"
    m["Message-ID"] = msgid or f"<{subject}@x.test>"
    m.set_content(body)
    for name, data in attachments:
        m.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return m.as_bytes()


class FakeIMAP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, messages):
        super().__init__(("127.0.0.1", 0), _IMAPHandler)
        self.messages = [{"raw": raw, "seen": False} for raw in messages]
        self.user, self.password = "robot", "s3cret"
        self.selected = []
        threading.Thread(target=self.serve_forever, daemon=True).start()


class _IMAPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        srv, w = self.server, self.wfile.write
        w(b"* OK fake IMAP4rev1 ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            parts = line.decode().strip().split(" ", 2)
            tag, cmd = parts[0], parts[1].upper()
            rest = parts[2] if len(parts) > 2 else ""
            if cmd == "CAPABILITY":
                w(b"* CAPABILITY IMAP4rev1\r\n" + tag.encode() + b" OK done\r\n")
            elif cmd == "LOGIN":
                u, p = rest.replace('"', "").split(" ", 1)
                if (u, p) == (srv.user, srv.password):
                    w(tag.encode() + b" OK LOGIN completed\r\n")
                else:
                    w(tag.encode() + b" NO [AUTHENTICATIONFAILED] Invalid credentials\r\n")
            elif cmd in ("SELECT", "EXAMINE"):
                srv.selected.append((cmd, rest))
                w(f"* {len(srv.messages)} EXISTS\r\n* 0 RECENT\r\n".encode() + tag.encode() + b" OK [READ-WRITE] done\r\n")
            elif cmd == "UID":
                sub, _, args = rest.partition(" ")
                sub = sub.upper()
                if sub == "SEARCH":
                    want = [str(i + 1) for i, m in enumerate(srv.messages) if not (("UNSEEN" in args.upper()) and m["seen"])]
                    w(("* SEARCH " + " ".join(want) + "\r\n").encode() + tag.encode() + b" OK SEARCH done\r\n")
                elif sub == "FETCH":
                    uid = int(args.split(" ", 1)[0])
                    raw = srv.messages[uid - 1]["raw"]
                    w(f"* {uid} FETCH (UID {uid} BODY[] {{{len(raw)}}}\r\n".encode() + raw + b")\r\n" + tag.encode() + b" OK FETCH done\r\n")
                elif sub == "STORE":
                    uid = int(args.split(" ", 1)[0])
                    if "\\Seen" in args:
                        srv.messages[uid - 1]["seen"] = True
                    w(f"* {uid} FETCH (UID {uid} FLAGS (\\Seen))\r\n".encode() + tag.encode() + b" OK STORE done\r\n")
            elif cmd == "LOGOUT":
                w(b"* BYE bye\r\n" + tag.encode() + b" OK LOGOUT done\r\n")
                return
            else:
                w(tag.encode() + b" BAD unknown\r\n")


@pytest.fixture
def imap():
    msgs = [make_mail("one", "first body", [("../../evil.txt", b"E1"), ("inv.pdf", b"%PDF-fake")]),
            make_mail("two", "second body"),
            make_mail("three", "third body", [("inv.pdf", b"another")])]
    s = FakeIMAP(msgs)
    yield s
    s.shutdown()
    s.server_close()


def read(imap, **kw):
    args = dict(host="127.0.0.1", port=imap.server_address[1], username="robot", password="s3cret", ssl_=False, timeout=5)
    args.update(kw)
    return mail.read_imap(**args)


def test_read_imap_fetches_parses_and_saves_attachments_safely(imap, tmp_path):
    out = tmp_path / "att"
    msgs = read(imap, save_attachments_to=out)
    assert [m["subject"] for m in msgs] == ["one", "two", "three"] and msgs[0]["uid"] == "1"
    assert msgs[0]["body_text"].strip() == "first body" and msgs[0]["from"] == "sender@x.test" and msgs[0]["date"].startswith("2026-10-05T10:00:00")
    saved = [a["path"] for m in msgs for a in m["attachments"]]
    assert all(str(out) in p for p in saved)                                # path traversal in the file name is neutralised
    assert (out / "evil.txt").read_bytes() == b"E1" and (out / "inv.pdf").read_bytes() == b"%PDF-fake" and (out / "inv (1).pdf").read_bytes() == b"another"
    assert not (tmp_path / "evil.txt").exists()
    assert imap.selected[-1][0] == "EXAMINE"                                # read-only unless mark_seen
    assert not any(m["seen"] for m in imap.messages)


def test_read_imap_limit_mark_seen_and_search(imap):
    msgs = read(imap, limit=2, mark_seen=True)
    assert [m["subject"] for m in msgs] == ["one", "two"] and [m["seen"] for m in imap.messages] == [True, True, False]
    assert imap.selected[-1][0] == "SELECT"
    again = read(imap, search="UNSEEN")
    assert [m["subject"] for m in again] == ["three"]
    assert len(read(imap, search="ALL")) == 3


def test_read_imap_errors(imap):
    with pytest.raises(AuthExpired):
        read(imap, password="wrong")
    with pytest.raises(NetworkError):
        mail.read_imap("127.0.0.1", "u", "p", port=1, ssl_=False, timeout=2)


def test_safe_filename():
    assert mail.safe_filename("../../etc/passwd") == "passwd"
    assert mail.safe_filename('a<b>:"c|d?.txt') == "a_b___c_d_.txt"
    assert mail.safe_filename("") == "attachment" and mail.safe_filename("...") == "attachment"
    assert mail.safe_filename("C:\\Users\\x\\y.docx") == "y.docx"


# --- Outlook (no live COM in tests) -----------------------------------------------------------------------------
def test_outlook_argument_building(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    a = mail.build_outlook_send_args("x@y.test; z@y.test", "S", "<p>hi</p>", html=True, cc="c@y.test", attachments=[f], draft=True)
    assert a["to"] == ["x@y.test", "z@y.test"] and a["cc"] == ["c@y.test"] and a["html"] and a["draft"] and a["attachments"] == [str(f.resolve())]
    with pytest.raises(DataError):
        mail.build_outlook_send_args([], "S", "b")
    with pytest.raises(DataError):
        mail.build_outlook_send_args("x@y.test", "S", "b", attachments=[tmp_path / "nope"])
    r = mail.build_outlook_read_args("Inbox/Invoices", unread_only=False, limit=5, save_to=tmp_path, mark_read=True)
    assert r == {"folder": "Inbox/Invoices", "unread_only": False, "limit": 5, "save_to": str(tmp_path.resolve()), "mark_read": True}
    with pytest.raises(ValueError):
        mail.build_outlook_read_args(limit=0)
    for script in (mail.OUTLOOK_SEND_SCRIPT, mail.OUTLOOK_READ_SCRIPT):
        assert "AMX_NO_OUTLOOK" in script and "$A." in script               # data arrives as JSON in $A, never spliced into the script


@pytest.mark.skipif(sys.platform == "win32", reason="off-Windows behaviour")
def test_outlook_needs_windows():
    with pytest.raises(NotAvailable):
        mail.outlook_read()
    with pytest.raises(NotAvailable):
        mail.outlook_send("a@b.test", "s")


def test_outlook_missing_is_not_available(monkeypatch):
    monkeypatch.setattr(mail, "require_windows", lambda what: None)

    def boom(*a, **k):
        raise ActivityError("Outlook failed (3): AMX_NO_OUTLOOK: Outlook is not installed or not accessible")

    monkeypatch.setattr(mail, "powershell_json", boom)
    with pytest.raises(NotAvailable):
        mail.outlook_read()
    monkeypatch.setattr(mail, "powershell_json", lambda *a, **k: [{"subject": "s", "entry_id": "1"}])
    assert mail.outlook_read(limit=1) == [{"subject": "s", "entry_id": "1"}]
