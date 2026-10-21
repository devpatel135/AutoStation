from __future__ import annotations

import datetime as dt
import hashlib
import zipfile

import pytest

from wrapper_core.activities import excel
from wrapper_core.retry import DataError


def test_roundtrip_all_types(tmp_path):
    rows = [
        {"name": "Acme <&> \"Co\"", "qty": 3, "price": 1.25, "paid": True, "due": dt.date(2026, 10, 31),
         "at": dt.datetime(2026, 10, 1, 13, 45, 30), "note": None, "unicode": "Zażółć gęślą jaźń ✓", "lead": "  spaced  "},
        {"name": "Bob", "qty": 0, "price": -2.5, "paid": False, "due": None, "at": None, "note": "x", "unicode": "", "lead": "1"},
    ]
    p = tmp_path / "t.xlsx"
    excel.write_xlsx(p, rows)
    back = excel.read_xlsx(p)
    assert back[0]["name"] == 'Acme <&> "Co"' and back[0]["qty"] == 3 and back[0]["price"] == 1.25
    assert back[0]["paid"] is True and back[1]["paid"] is False
    assert back[0]["due"] == dt.date(2026, 10, 31) and back[0]["at"] == dt.datetime(2026, 10, 1, 13, 45, 30)
    assert back[0]["unicode"].startswith("Zażółć") and back[0]["lead"] == "  spaced  "
    assert back[1]["due"] is None and back[1]["note"] == "x" and back[1]["qty"] == 0 and isinstance(back[1]["qty"], int)


def test_written_package_has_the_parts_excel_needs(tmp_path):
    p = tmp_path / "t.xlsx"
    excel.write_xlsx(p, [{"a": 1}])
    with zipfile.ZipFile(p) as z:
        names = z.namelist()
        assert names[0] == "[Content_Types].xml"
        for part in ("_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml", "xl/sharedStrings.xml", "xl/worksheets/sheet1.xml"):
            assert part in names
        ct = z.read("[Content_Types].xml").decode()
        assert "spreadsheetml.sheet.main+xml" in ct and "worksheet+xml" in ct and "sharedStrings+xml" in ct
        assert z.testzip() is None


def test_output_is_reproducible(tmp_path):
    rows = [{"a": 1, "b": "x"}]
    a, b = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    excel.write_xlsx(a, rows)
    excel.write_xlsx(b, rows)
    assert hashlib.sha256(a.read_bytes()).hexdigest() == hashlib.sha256(b.read_bytes()).hexdigest()


def test_header_false_and_columns_and_lists(tmp_path):
    p = tmp_path / "t.xlsx"
    excel.write_xlsx(p, [[1, "a"], [2, "b"]], columns=["n", "s"])
    assert excel.read_xlsx(p) == [{"n": 1, "s": "a"}, {"n": 2, "s": "b"}]
    raw = excel.read_xlsx(p, header=False)
    assert raw[0] == {"A": "n", "B": "s"} and raw[2] == {"A": 2, "B": "b"}


def test_sheets_by_name_and_index_and_errors(tmp_path):
    p = tmp_path / "t.xlsx"
    excel.write_workbook(p, {"Summary": [{"k": 1}], "Detail": [{"k": 2}, {"k": 3}]})
    assert excel.sheet_names(p) == ["Summary", "Detail"]
    assert len(excel.read_xlsx(p, sheet="Detail")) == 2 and len(excel.read_xlsx(p, sheet=0)) == 1
    with pytest.raises(DataError):
        excel.read_xlsx(p, sheet="Nope")
    with pytest.raises(DataError):
        excel.read_xlsx(p, sheet=7)
    for bad in ("", "x" * 32, "a/b", "a[b]", "q?"):
        with pytest.raises(DataError):
            excel.write_xlsx(tmp_path / "bad.xlsx", [{"a": 1}], sheet=bad)
    with pytest.raises(DataError):
        excel.write_workbook(tmp_path / "dup.xlsx", {"A": [], "a": []})


def test_append_creates_then_appends(tmp_path):
    p = tmp_path / "log.xlsx"
    excel.append_xlsx(p, [{"id": 1}])
    excel.append_xlsx(p, [{"id": 2}, {"id": 3}])
    assert [r["id"] for r in excel.read_xlsx(p)] == [1, 2, 3]


def test_invalid_files_raise_data_error(tmp_path):
    junk = tmp_path / "junk.xlsx"
    junk.write_bytes(b"not a zip")
    with pytest.raises(DataError):
        excel.read_xlsx(junk)
    z = tmp_path / "empty.xlsx"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("hello.txt", "x")
    with pytest.raises(DataError):
        excel.read_xlsx(z)
    with pytest.raises(DataError):
        excel.read_xlsx(tmp_path / "missing.xlsx")


def test_invalid_xml_control_chars_are_stripped(tmp_path):
    p = tmp_path / "t.xlsx"
    excel.write_xlsx(p, [{"a": "bad\x00\x0bchars ok"}])
    assert excel.read_xlsx(p)[0]["a"] == "badchars ok"


def _foreign_xlsx(path, date1904=False):
    """An xlsx shaped like Excel/LibreOffice output: shared strings with rich text + phonetic runs, inline strings, formulas with cached
    values, a custom date format, sparse cells/rows, explicit cell refs, a boolean, an error value, strict-ish prefixed namespaces."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    wb = (f'<?xml version="1.0"?><x:workbook xmlns:x="{main}" xmlns:r="{rel}">'
          f'<x:workbookPr date1904="{"1" if date1904 else "0"}"/><x:sheets><x:sheet name="Data" sheetId="1" r:id="rId1"/></x:sheets></x:workbook>')
    rels = (f'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    sst = (f'<?xml version="1.0"?><x:sst xmlns:x="{main}">'
           '<x:si><x:t>Name</x:t></x:si><x:si><x:t>When</x:t></x:si><x:si><x:t>Total</x:t></x:si>'
           '<x:si><x:r><x:t>Rich </x:t></x:r><x:r><x:t>Text</x:t></x:r><x:rPh><x:t>PHONETIC</x:t></x:rPh></x:si></x:sst>')
    styles = (f'<?xml version="1.0"?><x:styleSheet xmlns:x="{main}"><x:numFmts count="1"><x:numFmt numFmtId="170" formatCode="[$-409]d\\-mmm\\-yy;@"/></x:numFmts>'
              '<x:cellXfs count="3"><x:xf numFmtId="0"/><x:xf numFmtId="170"/><x:xf numFmtId="14"/></x:cellXfs></x:styleSheet>')
    serial = 46296  # 2026-10-01 in the 1900 system
    sheet = (f'<?xml version="1.0"?><x:worksheet xmlns:x="{main}"><x:sheetData>'
             '<x:row r="1"><x:c r="A1" t="s"><x:v>0</x:v></x:c><x:c r="C1" t="s"><x:v>1</x:v></x:c><x:c r="E1" t="s"><x:v>2</x:v></x:c></x:row>'
             f'<x:row r="2"><x:c r="A2" t="s"><x:v>3</x:v></x:c><x:c r="C2" s="1"><x:v>{serial}</x:v></x:c><x:c r="E2"><x:f>SUM(1,2)</x:f><x:v>3</x:v></x:c></x:row>'
             '<x:row r="4"><x:c r="A4" t="inlineStr"><x:is><x:t>inline</x:t></x:is></x:c><x:c r="C4" t="b"><x:v>1</x:v></x:c><x:c r="E4" t="e"><x:v>#DIV/0!</x:v></x:c></x:row>'
             f'<x:row r="5"><x:c r="A5" t="str"><x:v>fstr</x:v></x:c><x:c r="C5" s="2"><x:v>{serial + 0.5}</x:v></x:c><x:c r="E5"><x:v>1.5</x:v></x:c></x:row>'
             '</x:sheetData></x:worksheet>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/styles.xml", styles)
        z.writestr("xl/worksheets/sheet1.xml", sheet)


def test_reads_foreign_workbook_features(tmp_path):
    p = tmp_path / "foreign.xlsx"
    _foreign_xlsx(p)
    rows = excel.read_xlsx(p)
    assert [list(r)[:5] for r in rows][0] == ["Name", "col_2", "When", "col_4", "Total"]  # sparse columns get placeholder headers
    r0, r1, r2 = rows
    assert r0["Name"] == "Rich Text"                      # rich-text runs joined, phonetic run ignored
    assert r0["When"] == dt.date(2026, 10, 1)             # custom date format recognised
    assert r0["Total"] == 3 and isinstance(r0["Total"], int)  # formula -> cached value
    assert r1["Name"] == "inline" and r1["When"] is True and r1["Total"] == "#DIV/0!"
    assert r2["Name"] == "fstr" and r2["Total"] == 1.5
    assert r2["When"] == dt.datetime(2026, 10, 1, 12, 0)  # builtin numFmt 14 + fractional day => datetime (row 3 was entirely empty and skipped)


def test_date1904_workbook(tmp_path):
    p = tmp_path / "mac.xlsx"
    _foreign_xlsx(p, date1904=True)
    rows = excel.read_xlsx(p)
    assert rows[0]["When"] == dt.date(2026, 10, 1) + dt.timedelta(days=1462)


def test_csv_sniffing_and_roundtrip(tmp_path):
    semi = tmp_path / "semi.csv"
    semi.write_bytes("name;amount\nÅsa;1,5\nBob;2\n".encode("cp1252"))
    rows = excel.read_csv(semi)
    assert rows[0] == {"name": "Åsa", "amount": "1,5"}
    bom = tmp_path / "bom.csv"
    bom.write_bytes(b"\xef\xbb\xbfid,name\n1,\xc3\x85sa\n")
    assert excel.read_csv(bom) == [{"id": "1", "name": "Åsa"}]
    u16 = tmp_path / "u16.csv"
    u16.write_bytes("id\tname\n1\tx\n".encode("utf-16"))
    assert excel.read_csv(u16) == [{"id": "1", "name": "x"}]
    tab = tmp_path / "tab.csv"
    tab.write_text("a\tb\n1\t2\n", encoding="utf-8")
    assert excel.read_csv(tab, header=False)[0] == {"A": "a", "B": "b"}
    out = tmp_path / "out.csv"
    excel.write_csv(out, [{"d": dt.date(2026, 1, 2), "s": 'q"uote,comma', "n": None}], delimiter=";")
    assert excel.read_csv(out)[0] == {"d": "2026-01-02", "s": 'q"uote,comma', "n": None}


def test_table_helpers():
    rows = [{"v": "a", "n": 1}, {"v": "a", "n": 2}, {"v": "b", "n": 3}, {"v": "a", "n": 2}]
    assert excel.filter_rows(rows, v="a", n=2) == [rows[1], rows[3]]
    assert excel.filter_rows(rows, lambda r: r["n"] > 1, v="a") == [rows[1], rows[3]]
    g = excel.group_by(rows, "v")
    assert len(g["a"]) == 3 and len(g["b"]) == 1
    assert set(excel.group_by(rows, ["v", "n"])) == {("a", 1), ("a", 2), ("b", 3)}
    agg = {r["v"]: r for r in excel.aggregate(rows, "v", total=("n", "sum"), cnt=("n", "count"), top=("n", "max"), mean=("n", "avg"))}
    assert agg["a"] == {"v": "a", "total": 5, "cnt": 3, "top": 2, "mean": 5 / 3}
    assert excel.dedupe(rows) == rows[:3]
    assert excel.dedupe(rows, key="v") == [rows[0], rows[2]]
    assert excel.dedupe(rows, key="v", keep="last") == [rows[2], rows[3]]
    left = [{"id": 1, "x": "l1"}, {"id": 2, "x": "l2"}]
    right = [{"id": 1, "x": "r1", "y": "Y"}, {"id": 1, "x": "r1b", "y": "Y2"}]
    inner = excel.join(left, right, on="id")
    assert inner == [{"id": 1, "x": "l1", "x_right": "r1", "y": "Y"}, {"id": 1, "x": "l1", "x_right": "r1b", "y": "Y2"}]
    lj = excel.join(left, right, on="id", how="left")
    assert {"id": 2, "x": "l2"} in lj and len(lj) == 3
    with pytest.raises(ValueError):
        excel.join(left, right, on="id", how="outer")
    with pytest.raises(ValueError):
        excel.aggregate(rows, "v", t=("n", "median"))


def test_big_sheet_roundtrip_is_reasonably_fast(tmp_path):
    rows = [{"id": i, "txt": f"row {i}", "amt": i * 1.5} for i in range(5000)]
    p = tmp_path / "big.xlsx"
    excel.write_xlsx(p, rows)
    back = excel.read_xlsx(p)
    assert len(back) == 5000 and back[4999] == rows[4999]
