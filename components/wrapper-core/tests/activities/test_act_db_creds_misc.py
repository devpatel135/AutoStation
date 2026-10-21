from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from wrapper_core import retry, runctx
from wrapper_core.activities import credentials, db, images, notify
from wrapper_core.activities._common import NotAvailable
from wrapper_core.retry import AuthExpired, DataError, LogicError, NetworkError, SystemUnavailable

WIN = sys.platform == "win32"


# --- sqlite / db ------------------------------------------------------------------------------------------
def test_sqlite_dict_rows_and_commit_rollback(tmp_path):
    p = tmp_path / "d" / "x.db"
    with db.sqlite(p) as c:
        db.execute(c, "CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT, amt REAL)")
        assert db.execute(c, "INSERT INTO t(name, amt) VALUES (?, ?)", ("a", 1.5)) == 1
        db.execute(c, "INSERT INTO t(name, amt) VALUES (?, ?)", ("b", 2.5))
    with db.sqlite(p, readonly=True) as c:
        assert db.query(c, "SELECT * FROM t ORDER BY id") == [{"id": 1, "name": "a", "amt": 1.5}, {"id": 2, "name": "b", "amt": 2.5}]
        with pytest.raises(DataError):
            db.execute(c, "INSERT INTO t(name) VALUES ('x')")           # read-only
    with pytest.raises(RuntimeError):
        with db.sqlite(p) as c:
            db.execute(c, "DELETE FROM t")
            raise RuntimeError("boom")
    with db.sqlite(p) as c:
        assert len(db.query(c, "SELECT * FROM t")) == 2                    # rolled back
        with pytest.raises(DataError):
            db.query(c, "SELECT * FROM nope")
        with pytest.raises(DataError):
            db.execute(c, "INSERT INTO t(id) VALUES ('a','b')")


def test_sqlite_busy_is_system_unavailable(tmp_path):
    p = tmp_path / "busy.db"
    with db.sqlite(p) as c:
        db.execute(c, "CREATE TABLE t(a)")
    import sqlite3
    holder = sqlite3.connect(str(p), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        c2 = db.connect_sqlite(p, timeout=0.2)
        with pytest.raises(SystemUnavailable):
            db.execute(c2, "INSERT INTO t VALUES (1)")
        c2.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_odbc_request_building_is_strict():
    r = db.build_odbc_request("SELECT * FROM t WHERE a=? AND b=?", [1, "x'; DROP TABLE t;--"], 20, "query")
    assert r == {"sql": "SELECT * FROM t WHERE a=? AND b=?", "params": [1, "x'; DROP TABLE t;--"], "timeout": 20, "mode": "query"}
    with pytest.raises(DataError):
        db.build_odbc_request("x", [{"a": 1}], 10, "query")
    with pytest.raises(ValueError):
        db.build_odbc_request("x", [], 10, "weird")
    assert "$A.sql" in db.ODBC_SCRIPT and "AMX_CONNSTR" in db.ODBC_SCRIPT and "CommandText = [string]$A.sql" in db.ODBC_SCRIPT


def test_odbc_connection_string_resolution(monkeypatch):
    assert db._connstr("DSN=x", None) == "DSN=x"
    monkeypatch.setenv("AMX_CONNSTR_ERP", "DSN=erp;Trusted_Connection=yes")
    monkeypatch.setattr("wrapper_core.secrets.credman_read", lambda t: None)
    assert db._connstr(None, "erp") == "DSN=erp;Trusted_Connection=yes"
    monkeypatch.setattr("wrapper_core.secrets.credman_read", lambda t: "DSN=from-credman" if t == "AUTOMAGIX/connstr/erp" else None)
    assert db._connstr(None, "erp") == "DSN=from-credman"
    with pytest.raises(LogicError):
        db._connstr(None, "missing")
    with pytest.raises(LogicError):
        db._connstr(None, None)


@pytest.mark.skipif(WIN, reason="off-Windows behaviour")
def test_odbc_needs_windows_off_windows():
    with pytest.raises(NotAvailable):
        db.query_odbc("DSN=x", "SELECT 1")


@pytest.mark.skipif(not WIN, reason="runs real powershell.exe + ODBC")
def test_odbc_unknown_dsn_is_reported_as_missing_driver_and_never_echoes_the_connection_string():
    secret = "PWD=Sup3rS3cret!"
    with pytest.raises((NotAvailable, DataError, NetworkError)) as ei:
        db.query_odbc(f"DSN=amx-definitely-not-a-dsn;UID=u;{secret}", "SELECT 1", timeout=20)
    assert "Sup3rS3cret" not in str(ei.value) and "Sup3rS3cret" not in json.dumps(runctx.current().read_events())


def _text_driver_name():
    if not WIN:
        return None
    from wrapper_core.activities._common import powershell_json
    try:
        names = powershell_json("ConvertTo-Json -Compress -InputObject @(Get-OdbcDriver | Where-Object { $_.Name -like '*Text Driver*' } | ForEach-Object { $_.Name })", timeout=60)
    except Exception:  # noqa: BLE001
        return None
    return (names or [None])[0]


@pytest.mark.skipif(not WIN or not _text_driver_name(), reason="needs the Microsoft Text ODBC driver (64-bit) on this Windows host")
def test_odbc_integration_with_text_driver(tmp_path):
    (tmp_path / "people.csv").write_text("id,name\n1,Ann\n2,Bob\n", encoding="utf-8")
    cs = f"Driver={{{_text_driver_name()}}};Dbq={tmp_path};Extensions=asc,csv,tab,txt;"
    try:
        rows = db.query_odbc(cs, "SELECT id, name FROM [people.csv] WHERE id = ?", [2])
    except NotAvailable:
        pytest.skip("the Text driver is registered but not usable from this 64-bit host (Jet text driver is 32-bit only)")
    assert rows == [{"id": 2, "name": "Bob"}]


# --- credentials --------------------------------------------------------------------------------------------
def test_credentials_both_formats_and_masking(monkeypatch):
    store = {"AUTOMAGIX/cred/sap": "svc-sap:p@ss:word", "AUTOMAGIX/cred/erp": json.dumps({"username": "erp", "password": "Pw!"})}
    monkeypatch.setattr("wrapper_core.secrets.credman_read", lambda t: store.get(t))
    user, pwd = credentials.get_credential("sap")
    assert user == "svc-sap" and pwd == "p@ss:word"                         # only the first colon splits
    assert credentials.get_credential("erp") == ("erp", "Pw!")
    cred = credentials.get_credential("sap")
    assert "p@ss" not in repr(cred) and "p@ss" not in repr(cred.password) and "p@ss" not in repr([cred])
    assert isinstance(pwd, str)                                             # still usable as a normal string
    blob = json.dumps(runctx.current().read_events())
    assert "p@ss" not in blob and "svc-sap" not in blob                      # telemetry carries the credential NAME only
    with pytest.raises(credentials.CredentialNotFound) as ei:
        credentials.get_credential("nope")
    assert retry.classify_exception(ei.value) is retry.Category.LOGIC_ERROR
    store["AUTOMAGIX/cred/bad"] = "nocolon"
    with pytest.raises(credentials.CredentialNotFound):
        credentials.get_credential("bad")


def test_credentials_dev_env_fallback_and_assets(monkeypatch, tmp_path):
    monkeypatch.setattr("wrapper_core.secrets.credman_read", lambda t: None)
    monkeypatch.setenv("AMX_CRED_MY_APP", "u:p")
    assert credentials.get_credential("my-app") == ("u", "p")
    monkeypatch.setenv("AMX_ASSET_API_URL", "https://override")
    assert credentials.get_asset("api-url", "d") == "https://override"
    monkeypatch.delenv("AMX_ASSET_API_URL")
    assert credentials.get_asset("api-url", "default") == "default"
    from wrapper_core.paths import Paths
    p = Paths().ensure()
    p.assets_json.write_text(json.dumps({"api-url": "https://from-manifest"}))
    assert credentials.get_asset("api-url") == "https://from-manifest"


@pytest.mark.skipif(not WIN, reason="Credential Manager is Windows-only")
def test_credential_manager_roundtrip_through_the_activity():
    name = f"amx-test-{threading.get_ident()}"
    try:
        credentials.store_credential(name, "robot", "pa:ss")
        u, p = credentials.get_credential(name)
        assert (u, p) == ("robot", "pa:ss")
    finally:
        credentials.delete_credential(name)
    with pytest.raises(credentials.CredentialNotFound):
        credentials.get_credential(name)


# --- images (optional deps) ---------------------------------------------------------------------------------
def _have(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


@pytest.mark.skipif(_have("pytesseract") and _have("PIL"), reason="OCR deps installed here")
def test_ocr_reports_missing_dependencies_clearly(tmp_path):
    with pytest.raises(NotAvailable, match="pytesseract"):
        images.ocr_image(str(tmp_path / "x.png"))
    assert retry.classify_exception(NotAvailable("x")) is retry.Category.LOGIC_ERROR


@pytest.mark.skipif(_have("pyautogui"), reason="pyautogui installed here")
def test_find_on_screen_reports_missing_dependency(tmp_path):
    with pytest.raises(NotAvailable, match="pyautogui"):
        images.find_on_screen(str(tmp_path / "t.png"))


# --- notify -----------------------------------------------------------------------------------------------------
@pytest.fixture
def hook():
    got = []
    mode = {"status": 200, "body": b"1"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(mode["status"])
            self.send_header("Content-Length", str(len(mode["body"])))
            self.end_headers()
            self.wfile.write(mode["body"])

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/hook/SECRET-PATH", got, mode
    srv.shutdown()
    srv.server_close()


def test_teams_webhook_posts_a_card_and_never_logs_the_url(hook):
    url, got, _ = hook
    notify.teams_webhook(url, "Run failed", "line1\nline2", {"automation": "invoice-router", "items": 5})
    card = got[0]
    assert card["@type"] == "MessageCard" and card["title"] == "Run failed" and card["text"] == "line1<br>line2"
    assert card["sections"][0]["facts"] == [{"name": "automation", "value": "invoice-router"}, {"name": "items", "value": "5"}]
    assert "SECRET-PATH" not in json.dumps(runctx.current().read_events())


def test_teams_webhook_from_credential_and_error_classification(hook, monkeypatch):
    url, got, mode = hook
    monkeypatch.setattr("wrapper_core.secrets.credman_read", lambda t: url if t == "AUTOMAGIX/cred/teams" else None)
    notify.teams_webhook("teams", "hi")
    assert got
    mode["status"] = 404
    with pytest.raises(AuthExpired):
        notify.teams_webhook(url, "x")
    mode["status"] = 500
    with pytest.raises(SystemUnavailable):
        notify.teams_webhook(url, "x")
    mode["status"] = 400
    with pytest.raises(DataError):
        notify.teams_webhook(url, "x")
    with pytest.raises(NetworkError):
        notify.teams_webhook("http://127.0.0.1:1/h", "x", timeout=2)
    with pytest.raises(credentials.CredentialNotFound):
        notify.teams_webhook("no-such-credential", "x")
    monkeypatch.setattr("wrapper_core.secrets.credman_read", lambda t: "http://evil.example.com/h")
    with pytest.raises(DataError):
        notify.teams_webhook("plain-http", "x")                              # plain http (non-loopback) is refused


@pytest.mark.skipif(WIN, reason="off-Windows behaviour")
def test_clipboard_and_screenshot_need_windows(tmp_path):
    with pytest.raises(NotAvailable):
        notify.clipboard.get()
    with pytest.raises(NotAvailable):
        notify.clipboard.set("x")
    with pytest.raises(NotAvailable):
        notify.screenshot_screen(tmp_path / "s.png")


def test_clipboard_script_passes_text_as_data_not_code():
    src = open(notify.__file__, encoding="utf-8").read()
    assert 'Set-Clipboard -Value ([string]$A.text)' in src                    # no string interpolation of user text into PowerShell
