from __future__ import annotations

import http.client
import json
import threading
import urllib.parse

import pytest

from wrapper_core import runctx
from wrapper_core.activities import approvals as ap
from wrapper_core.paths import Paths


def make(**kw):
    return ap.create("Pay invoice INV-1", "Vendor Acme", fields={"vendor": "Acme", "amount": "120.00"}, notify=False, **kw)


# --- lifecycle --------------------------------------------------------------------------------------------
def test_create_decide_and_audit_trail():
    t = make(automation="auto-a")
    assert t.status == "pending" and ap.ID_RX.match(t.id) and len(t.id) >= 20
    assert [x.id for x in ap.list_tasks("pending")] == [t.id]
    d = ap.decide(t.id, "approve", by="alice", note="matches PO")
    assert d.status == "approved" and d.decided_by == "alice"
    got = ap.get(t.id)
    assert [a["event"] for a in got.audit] == ["created", "approve"] and got.audit[1]["by"] == "alice" and got.audit[1]["note"] == "matches PO"
    assert ap.list_tasks("pending") == []


def test_second_decision_is_refused():
    t = make()
    ap.decide(t.id, "reject", by="alice")
    with pytest.raises(ap.ApprovalError, match="already rejected"):
        ap.decide(t.id, "approve", by="bob")
    assert ap.get(t.id).status == "rejected"


def test_decisions_are_restricted():
    t = make()
    for bad in ("APPROVE", "yes", "", "approve; rm -rf", None):
        with pytest.raises(ap.ApprovalError):
            ap.decide(t.id, bad)
    assert ap.get(t.id).status == "pending"


def test_concurrent_deciders_only_one_wins():
    t = make()
    results = []

    def go(kind):
        try:
            ap.decide(t.id, kind, by=kind)
            results.append(kind)
        except ap.ApprovalError:
            pass
    threads = [threading.Thread(target=go, args=(k,)) for k in ("approve", "reject", "approve", "reject")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(results) == 1 and ap.get(t.id).status in ("approved", "rejected")


# --- expiry / default deny -----------------------------------------------------------------------------------
def test_expired_task_cannot_be_approved_and_wait_denies(monkeypatch):
    t = make(expires_in_s=60)
    future = ap.utcnow().timestamp() + 3600
    import datetime as dt
    monkeypatch.setattr(ap, "utcnow", lambda: dt.datetime.fromtimestamp(future, dt.timezone.utc))
    assert ap.expire_due() == 1
    assert ap.get(t.id).status == "expired"
    with pytest.raises(ap.ApprovalError):
        ap.decide(t.id, "approve")
    d = ap.wait(t.id, timeout_s=5, sleep=lambda s: None)
    assert d.approved is False and d.status == "expired"


def test_wait_times_out_with_denial_and_polls_until_decided():
    t = make()
    d = ap.wait(t.id, timeout_s=0.0, sleep=lambda s: None)
    assert d.approved is False and d.status == "timeout" and ap.get(t.id).status == "pending"
    calls = []

    def sleeper(s):
        calls.append(s)
        if len(calls) == 2:
            ap.decide(t.id, "approve", by="alice")
    d = ap.wait(t.id, timeout_s=100, poll_s=1, sleep=sleeper)
    assert d.approved is True and d.status == "approved" and d.decided_by == "alice"


def test_ttl_limits():
    for bad in (0, -5, ap.MAX_TTL_S + 1):
        with pytest.raises(ValueError):
            make(expires_in_s=bad)


# --- input validation / hostile ids ------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["../../etc/passwd", "..\\..\\x", "short", "a" * 41, "ok-but/slash0000000", "id with spaces 000000", "", None, 5])
def test_ids_can_never_address_other_files(bad):
    with pytest.raises(ap.ApprovalError):
        ap.get(bad)
    with pytest.raises(ap.ApprovalError):
        ap.decide(bad, "approve")


def test_create_validates_inputs():
    with pytest.raises(ValueError):
        ap.create("   ", notify=False)
    with pytest.raises(ValueError):
        ap.create("x" * 121, notify=False)
    with pytest.raises(ValueError):
        ap.create("t", "d" * 2001, notify=False)
    with pytest.raises(ValueError):
        ap.create("t", fields={"../evil": "x"}, notify=False)
    with pytest.raises(ValueError):
        ap.create("t", fields={"k": {"nested": 1}}, notify=False)
    with pytest.raises(ValueError):
        ap.create("t", fields={f"k{i}": "v" for i in range(31)}, notify=False)
    with pytest.raises(ValueError):
        ap.create("t", fields={"k": "v" * 501}, notify=False)
    with pytest.raises(ValueError):
        ap.create("t", automation="../x", notify=False)


def test_control_characters_are_stripped():
    t = ap.create("Pay" + chr(27) + "[31m now", "line1" + chr(10) + "line2" + chr(0), fields={"k": "a" + chr(7) + "b"}, notify=False)
    assert chr(27) not in t.title and chr(0) not in t.description and chr(7) not in t.fields["k"] and "line1" + chr(10) + "line2" == t.description


def test_pending_cap():
    p = Paths().ensure()
    for _ in range(3):
        make()
    ap.MAX_PENDING, saved = 3, ap.MAX_PENDING
    try:
        with pytest.raises(ap.ApprovalError, match="too many"):
            make()
    finally:
        ap.MAX_PENDING = saved
    assert p


def test_corrupt_record_is_ignored_not_fatal():
    t = make()
    (ap._dir() / f"{t.id}.json").write_text("{not json", encoding="utf-8")
    assert ap.list_tasks() == []
    with pytest.raises(ap.ApprovalError):
        ap.get(t.id)


def test_prune_keeps_pending_and_removes_old_decided():
    import os
    a, b = make(), make()
    ap.decide(b.id, "reject")
    old = os.path.getmtime(ap._dir() / f"{b.id}.json") - 90 * 86400
    os.utime(ap._dir() / f"{b.id}.json", (old, old))
    os.utime(ap._dir() / f"{a.id}.json", (old, old))
    assert ap.prune(30) == 1 and ap.get(a.id).status == "pending"


# --- telemetry -------------------------------------------------------------------------------------------------------
def test_events_carry_no_business_data(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOMAGIX_RUN_DIR", str(tmp_path / "run"))
    runctx.set_current(None)
    t = make(automation="auto-a")
    ap.decide(t.id, "approve", by="alice", note="secret note")
    ap.wait(t.id, timeout_s=1, sleep=lambda s: None)
    events = runctx.current().read_events()
    names = [e["event"] for e in events]
    assert "approval_requested" in names and "approval_decided" in names
    text = json.dumps(events, default=str)
    for leak in ("Pay invoice", "Acme", "120.00", "secret note", "alice"):
        assert leak not in text


# --- command line ------------------------------------------------------------------------------------------------------
def test_cli_list_show_approve_reject(capsys):
    t = make()
    assert ap.main(["list"]) == 0 and t.id in capsys.readouterr().out
    assert ap.main(["show", t.id]) == 0 and json.loads(capsys.readouterr().out)["id"] == t.id
    assert ap.main(["approve", t.id, "--note", "ok"]) == 0 and "approved" in capsys.readouterr().out
    assert ap.main(["reject", t.id]) == 1 and "already approved" in capsys.readouterr().err
    assert ap.main(["show", "../../x"]) == 1


# --- local approval page ------------------------------------------------------------------------------------------------
class Ui:
    def __init__(self):
        self.stop = threading.Event()
        self.info = {}
        ready = threading.Event()

        def on_ready(url, token, port):
            self.info.update(url=url, token=token, port=port)
            ready.set()
        self.th = threading.Thread(target=lambda: ap.serve(open_browser=False, idle_s=30, ready=on_ready, stop=self.stop), daemon=True)
        self.th.start()
        assert ready.wait(10)
        self.port, self.token = self.info["port"], self.info["token"]

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        if body is not None:
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        c.close()
        return r.status, dict(r.getheaders()), data

    def close(self):
        self.stop.set()
        self.th.join(5)


@pytest.fixture
def ui():
    u = Ui()
    yield u
    u.close()


def form(**kv):
    return urllib.parse.urlencode(kv)


def test_ui_requires_token_and_correct_host(ui):
    assert ui.req("GET", "/")[0] == 403
    assert ui.req("GET", "/?t=wrong")[0] == 403
    assert ui.req("GET", f"/?t={ui.token}", host="evil.example:80")[0] == 403              # DNS rebinding
    assert ui.req("GET", f"/?t={ui.token}", host=f"127.0.0.1:{ui.port + 1}")[0] == 403
    status, headers, body = ui.req("GET", f"/?t={ui.token}")
    assert status == 200 and "Nothing is waiting" in body
    low = {k.lower(): v for k, v in headers.items()}
    assert "default-src 'none'" in low["content-security-policy"] and low["referrer-policy"] == "no-referrer" and low["cache-control"] == "no-store"
    assert "<script" not in body.lower() and "http://" not in body and "https://" not in body             # nothing external, no scripts


def test_ui_approves_a_task_end_to_end(ui):
    t = make(automation="auto-a")
    status, _, body = ui.req("GET", f"/?t={ui.token}")
    assert status == 200 and "Pay invoice INV-1" in body
    status, _, body = ui.req("GET", f"/task/{t.id}?t={ui.token}")
    assert status == 200 and "Acme" in body and "name=decision value=approve" in body
    status, headers, _ = ui.req("POST", "/decide", form(t=ui.token, id=t.id, decision="approve", note="looks right"))
    assert status == 303 and "done=approved" in headers.get("Location", "")
    got = ap.get(t.id)
    assert got.status == "approved" and got.decision_note == "looks right"


def test_ui_rejects_forgery_and_bad_requests(ui):
    t = make()
    good = form(t=ui.token, id=t.id, decision="approve")
    assert ui.req("POST", "/decide", form(t="nope", id=t.id, decision="approve"))[0] == 403            # wrong token
    assert ui.req("POST", "/decide", good, headers={"Origin": "http://evil.example"})[0] == 403         # cross-origin
    assert ui.req("POST", "/decide", good, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert ui.req("POST", "/decide", good, headers={"Content-Type": "application/json"})[0] == 415
    assert ui.req("POST", "/decide", good, host="evil.example")[0] == 403
    assert ui.req("POST", "/decide", form(t=ui.token, id=t.id, decision="x" * 5000))[0] == 413          # oversized body
    assert ui.req("POST", "/decide", form(t=ui.token, id=t.id, decision="delete"))[0] == 409            # not a valid decision
    assert ui.req("POST", "/decide", form(t=ui.token, id="../../etc/passwd", decision="approve"))[0] == 409
    assert ui.req("GET", f"/task/../../etc/passwd?t={ui.token}")[0] == 404
    assert ap.get(t.id).status == "pending"


def test_ui_escapes_everything_shown(ui):
    evil = "<script>alert(1)</script>\"'>"
    t = ap.create(evil, evil, fields={"x y": evil}, automation="auto-a", notify=False)
    for path in (f"/?t={ui.token}", f"/task/{t.id}?t={ui.token}"):
        _, _, body = ui.req("GET", path)
        assert "<script>alert(1)" not in body and "&lt;script&gt;" in body


def test_ui_stops_after_many_bad_attempts():
    u = Ui()
    try:
        for _ in range(52):
            try:
                u.req("GET", "/?t=guess")
            except OSError:
                break
        u.th.join(10)
        assert not u.th.is_alive()
    finally:
        u.close()


def test_ui_binds_loopback_only():
    u = Ui()
    try:
        assert u.info["url"].startswith("http://127.0.0.1:")
    finally:
        u.close()
