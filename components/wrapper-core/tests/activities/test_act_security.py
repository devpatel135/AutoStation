"""Hardening regressions for the activities library: HTTP redirects/schemes/pagination, hostile workbooks and zips,
CSV formula injection, attachment names, SendKeys escaping."""
from __future__ import annotations

import csv
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wrapper_core import retry
from wrapper_core.activities import excel, files, http, mail, queue, windows_ui
from wrapper_core.retry import DataError


# --- HTTP ------------------------------------------------------------------------------------------------------
class Two:
    """Two loopback origins; ``a`` redirects to ``b``. Each records the headers it received."""

    def __init__(self):
        self.seen = {"a": [], "b": []}
        outer = self

        def handler(tag):
            class H(BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def _reply(self, code, headers=(), body=b""):
                    self.send_response(code)
                    for k, v in headers:
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    outer.seen[tag].append({k.lower(): v for k, v in self.headers.items()})
                    b = outer.b.server_address[1]
                    if tag == "a" and self.path == "/to-b":
                        return self._reply(302, [("Location", f"http://127.0.0.1:{b}/landing")])
                    if tag == "a" and self.path == "/same":
                        return self._reply(302, [("Location", "/landing")])
                    if tag == "a" and self.path == "/to-file":
                        return self._reply(302, [("Location", "file:///C:/Windows/win.ini")])
                    if tag == "a" and self.path == "/link":
                        return self._reply(200, [("Content-Type", "application/json"), ("Link", f'<http://127.0.0.1:{b}/p2>; rel="next"')], b'[{"id":1}]')
                    if tag == "a" and self.path == "/cursor":
                        return self._reply(200, [("Content-Type", "application/json")], f'{{"items":[1],"next":"http://127.0.0.1:{b}/p2"}}'.encode())
                    if tag == "a" and self.path == "/loop":
                        a = outer.a.server_address[1]
                        return self._reply(200, [("Content-Type", "application/json"), ("Link", f'<http://127.0.0.1:{a}/loop>; rel="next"')], b'[{"id":1}]')
                    self._reply(200, [("Content-Type", "application/json")], b'{"ok": true}')
            return H

        self.a = ThreadingHTTPServer(("127.0.0.1", 0), handler("a"))
        self.b = ThreadingHTTPServer(("127.0.0.1", 0), handler("b"))
        for s in (self.a, self.b):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.a_url = f"http://127.0.0.1:{self.a.server_address[1]}"
        self.b_url = f"http://127.0.0.1:{self.b.server_address[1]}"

    def close(self):
        for s in (self.a, self.b):
            s.shutdown()
            s.server_close()


@pytest.fixture
def two():
    t = Two()
    yield t
    t.close()


def test_only_http_and_https_urls_are_requested(tmp_path):
    c = http.client(retries=0)
    for bad in ("file:///C:/Windows/win.ini", "ftp://example.com/x", "gopher://x/y", "javascript:alert(1)", "//no-scheme/x"):
        with pytest.raises(DataError):
            c.get(bad)
    with pytest.raises(DataError):
        http.download("file:///C:/Windows/win.ini", tmp_path / "x")
    with pytest.raises(DataError):
        http.OAuth2ClientCredentials("file:///x", "id", "secret").token()


def test_cross_origin_redirect_strips_every_credential(two):
    c = http.client(two.a_url, headers={"X-Api-Key": "api-key-value", "Cookie": "sid=1"}, auth=http.BearerToken("bearer-secret"), retries=0)
    assert c.get_json("/to-b") == {"ok": True}
    first, landed = two.seen["a"][0], two.seen["b"][0]
    assert first["authorization"] == "Bearer bearer-secret" and first["x-api-key"] == "api-key-value"
    assert "authorization" not in landed and "x-api-key" not in landed and "cookie" not in landed


def test_same_origin_redirect_keeps_credentials(two):
    c = http.client(two.a_url, auth=http.BearerToken("bearer-secret"), retries=0)
    assert c.get_json("/same") == {"ok": True}
    assert [h.get("authorization") for h in two.seen["a"]] == ["Bearer bearer-secret", "Bearer bearer-secret"]


def test_redirect_to_file_scheme_is_refused(two):
    with pytest.raises((DataError, retry.NetworkError)):
        http.client(two.a_url, retries=0).get("/to-file")


def test_https_to_http_redirect_is_refused():
    import urllib.request
    h = http._SafeRedirect()
    req = urllib.request.Request("https://api.example.com/a", headers={"Authorization": "Bearer x"})
    with pytest.raises(DataError, match="https to http"):
        h.redirect_request(req, None, 302, "Found", {}, "http://api.example.com/b")
    ok = h.redirect_request(req, None, 302, "Found", {}, "https://api.example.com/b")
    assert ok.get_header("Authorization") == "Bearer x"
    cross = h.redirect_request(req, None, 302, "Found", {}, "https://other.example.com/b")
    assert cross.get_header("Authorization") is None


def test_paginate_refuses_next_links_on_another_origin(two):
    c = http.client(two.a_url, auth=http.BearerToken("bearer-secret"), retries=0)
    with pytest.raises(DataError, match="different origin"):
        list(c.paginate("/link", style="link"))
    with pytest.raises(DataError, match="different origin"):
        list(c.paginate("/cursor", style="next_key", items_key="items"))
    assert two.seen["b"] == []  # nothing, in particular no Authorization header, ever reached the other origin


def test_paginate_detects_a_next_link_loop(two):
    with pytest.raises(DataError, match="loops back"):
        list(http.client(two.a_url, retries=0).paginate("/loop", style="link"))


# --- hostile workbooks ----------------------------------------------------------------------------------------
def _xlsx(path, parts):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return path


WB = ('<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>')
RELS = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')


def _sheet(rows_xml):
    return f'<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{rows_xml}</sheetData></worksheet>'


def test_xlsx_with_entity_bomb_is_refused(tmp_path):
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
            '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]><worksheet><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>&c;</t></is></c></row></sheetData></worksheet>')
    f = _xlsx(tmp_path / "bomb.xlsx", {"xl/workbook.xml": WB, "xl/_rels/workbook.xml.rels": RELS, "xl/worksheets/sheet1.xml": bomb})
    with pytest.raises(DataError, match="DOCTYPE/ENTITY"):
        excel.read_xlsx(f)


def test_xlsx_doctype_in_workbook_part_and_utf16_are_refused(tmp_path):
    xxe = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///C:/Windows/win.ini">]>' + WB
    f = _xlsx(tmp_path / "xxe.xlsx", {"xl/workbook.xml": xxe, "xl/_rels/workbook.xml.rels": RELS, "xl/worksheets/sheet1.xml": _sheet("")})
    with pytest.raises(DataError):
        excel.read_xlsx(f)
    f2 = _xlsx(tmp_path / "u16.xlsx", {"xl/workbook.xml": WB.encode("utf-16"), "xl/_rels/workbook.xml.rels": RELS, "xl/worksheets/sheet1.xml": _sheet("")})
    with pytest.raises(DataError, match="UTF-16"):
        excel.read_xlsx(f2)


def test_xlsx_zip_bomb_part_is_capped_while_streaming(tmp_path, monkeypatch):
    monkeypatch.setattr(excel, "MAX_PART_BYTES", 1024 * 1024)
    big = _sheet("<row r=\"1\"><c r=\"A1\" t=\"inlineStr\"><is><t>" + "x" * (3 * 1024 * 1024) + "</t></is></c></row>")
    f = _xlsx(tmp_path / "big.xlsx", {"xl/workbook.xml": WB, "xl/_rels/workbook.xml.rels": RELS, "xl/worksheets/sheet1.xml": big})
    assert f.stat().st_size < 100_000                                     # tiny on disk
    with pytest.raises(DataError, match="exceeds"):
        excel.read_xlsx(f)


def test_xlsx_total_cap_and_member_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(excel, "MAX_TOTAL_BYTES", 2000)
    f = _xlsx(tmp_path / "t.xlsx", {"xl/workbook.xml": WB, "xl/_rels/workbook.xml.rels": RELS, "xl/worksheets/sheet1.xml": _sheet("<row r=\"1\"><c r=\"A1\"><v>1</v></c></row>" * 100)})
    with pytest.raises(DataError, match="expands beyond"):
        excel.read_xlsx(f)
    monkeypatch.undo()
    monkeypatch.setattr(excel, "MAX_MEMBERS", 3)
    f2 = _xlsx(tmp_path / "m.xlsx", {"xl/workbook.xml": WB, "a": "1", "b": "2", "c": "3"})
    with pytest.raises(DataError, match="too many parts"):
        excel.read_xlsx(f2)


def test_xlsx_absurd_row_and_sparse_grid_are_refused(tmp_path):
    parts = {"xl/workbook.xml": WB, "xl/_rels/workbook.xml.rels": RELS}
    f = _xlsx(tmp_path / "r.xlsx", {**parts, "xl/worksheets/sheet1.xml": _sheet('<row r="999999999"><c r="A1"><v>1</v></c></row>')})
    with pytest.raises(DataError, match="row number"):
        excel.read_xlsx(f)
    f2 = _xlsx(tmp_path / "c.xlsx", {**parts, "xl/worksheets/sheet1.xml": _sheet('<row r="1"><c r="ZZZZ1"><v>1</v></c></row>')})
    with pytest.raises(DataError, match="column index"):
        excel.read_xlsx(f2)
    sparse = _sheet('<row r="1"><c r="XFD1"><v>1</v></c></row><row r="1048576"><c r="A1"><v>2</v></c></row>')
    f3 = _xlsx(tmp_path / "s.xlsx", {**parts, "xl/worksheets/sheet1.xml": sparse})
    with pytest.raises(DataError, match="too sparse"):
        excel.read_xlsx(f3)


def test_normal_workbook_roundtrip_is_unaffected(tmp_path):
    f = tmp_path / "ok.xlsx"
    excel.write_xlsx(f, [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])
    assert excel.read_xlsx(f) == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


# --- files.unzip_safe ---------------------------------------------------------------------------------------------
def test_unzip_safe_streams_and_caps_real_size(tmp_path):
    z = tmp_path / "bomb.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.bin", b"\0" * (4 * 1024 * 1024))
        zf.writestr("b.bin", b"\0" * (4 * 1024 * 1024))
    dest = tmp_path / "out"
    with pytest.raises(DataError, match="size limit"):
        files.unzip_safe(z, dest, max_total_bytes=1024 * 1024)
    assert not list(dest.rglob("*.bin"))                                   # partial output removed


def test_unzip_safe_default_cap_is_500_mb():
    import inspect
    assert inspect.signature(files.unzip_safe).parameters["max_total_bytes"].default == 500 * 1024 * 1024


def test_unzip_safe_rejects_symlinks_and_traversal(tmp_path):
    z = tmp_path / "ln.zip"
    info = zipfile.ZipInfo("link")
    info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(DataError, match="symbolic link"):
        files.unzip_safe(z, tmp_path / "o1")
    z2 = tmp_path / "t.zip"
    with zipfile.ZipFile(z2, "w") as zf:
        zf.writestr("../evil.txt", "x")
    with pytest.raises(DataError, match="unsafe path"):
        files.unzip_safe(z2, tmp_path / "o2")


def test_unzip_safe_still_extracts_normal_archives(tmp_path):
    z = tmp_path / "ok.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("d/a.txt", "hello")
    out = files.unzip_safe(z, tmp_path / "out")
    assert [p.read_text() for p in out] == ["hello"]


# --- CSV formula injection ---------------------------------------------------------------------------------------------
EVIL = ["=HYPERLINK(\"http://evil\",\"x\")", "+1+1", "-2+3", "@SUM(A1)", "\t=1", "\r=1", "plain", "a=b", "", "  =1"]


def _read_raw(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def test_write_csv_neutralises_formulas_by_default(tmp_path):
    out = tmp_path / "o.csv"
    excel.write_csv(out, [{"v": v, "n": 5, "neg": -5} for v in EVIL])
    cells = [r[0] for r in _read_raw(out)[1:]]
    for original, written in zip(EVIL, cells):
        if original.startswith(("=", "+", "-", "@", "\t", "\r")):
            assert written == "'" + original
        else:
            assert written == original
    assert _read_raw(out)[1][1:] == ["5", "-5"]                             # real numbers are not text, untouched


def test_write_csv_header_cells_are_neutralised_and_opt_out_works(tmp_path):
    out = tmp_path / "o.csv"
    excel.write_csv(out, [{"=cmd|' /C calc'!A0": "x"}])
    assert _read_raw(out)[0] == ["'=cmd|' /C calc'!A0"]
    excel.write_csv(out, [{"v": "=1+1"}], escape_formulas=False)
    assert _read_raw(out)[1] == ["=1+1"]


def test_queue_export_csv_neutralises_formulas(tmp_path):
    q = queue.open("inj", tmp_path / "q.db")
    q.add("=SUM(1+1)", {"note": "x"})
    item = q.next()
    q.fail_business(item, "=cmd|' /C calc'!A0")
    safe = _read_raw(q.export_csv(tmp_path / "s.csv"))
    raw = _read_raw(q.export_csv(tmp_path / "r.csv", escape_formulas=False))
    flat_safe, flat_raw = [c for r in safe for c in r], [c for r in raw for c in r]
    assert any(c == "=cmd|' /C calc'!A0" for c in flat_raw) and "=SUM(1+1)" in flat_raw
    assert not any(c.startswith("=") for c in flat_safe) and any(c == "'=cmd|' /C calc'!A0" for c in flat_safe)
    assert "'=SUM(1+1)" in flat_safe


# --- attachment names ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("../../evil.exe", "evil.exe"), ("C:\\Windows\\x.dll", "x.dll"), ("NUL", "_NUL"), ("con.txt", "_con.txt"), ("COM1", "_COM1"),
    ("lpt9.log", "_lpt9.log"), ("report. ", "report"), ("...", "attachment"), ("", "attachment"),     ("ok name.pdf", "ok name.pdf"), ("CONSOLE.txt", "CONSOLE.txt"),
])
def test_safe_filename(name, expected):
    assert mail.safe_filename(name) == expected


def test_safe_filename_colon_never_survives():
    assert ":" not in mail.safe_filename("a:b.txt") and ":" not in mail.safe_filename("file.txt:hidden")


def test_outlook_script_applies_the_same_rules():
    s = mail.OUTLOOK_READ_SCRIPT
    assert "TrimEnd('.', ' ')" in s and "CON|PRN|AUX|NUL" in s and "$name = 'attachment'" in s


# --- windows_ui -----------------------------------------------------------------------------------------------------------
def test_escape_keys_and_literal_send_keys_escape_metacharacters():
    assert windows_ui.escape_keys("a+b^c%d~e(f)g{h}i[j]") == "a{+}b{^}c{%}d{~}e{(}f{)}g{{}h{}}i{[}j{]}"
    assert windows_ui.escape_keys("plain text 123") == "plain text 123"


def test_send_keys_literal_escapes_before_sending(monkeypatch):
    sent = []

    class FakeSession:
        def call(self, cmd, **kw):
            sent.append((cmd, kw))
            return {}

    monkeypatch.setattr(windows_ui, "_sess", lambda s=None: FakeSession())
    windows_ui.send_keys("%{F4}", literal=True)
    windows_ui.send_keys("%{F4}")
    assert sent[0][1]["keys"] == "{%}{{}F4{}}" and sent[1][1]["keys"] == "%{F4}"


def test_ui_host_regex_has_a_match_timeout():
    s = windows_ui.HOST_SCRIPT
    assert "[TimeSpan]::FromSeconds(2)" in s and "[regex]::IsMatch" not in s
