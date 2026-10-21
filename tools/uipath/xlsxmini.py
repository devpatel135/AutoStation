"""A tiny, dependency-free xlsx/csv/json table reader (and deterministic xlsx writer for fixtures).

Why not openpyxl: the migration kit must run on a locked-down laptop with only Python. This reads the subset that matters for
Config.xlsx files and output comparison: sheets, shared/inline strings, numbers, booleans. Formulas are returned as their
cached values; dates stay as Excel serial numbers (callers that care normalise them).
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

_COL = re.compile(r"^([A-Z]+)(\d+)$")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _col_index(ref: str) -> int:
    m = _COL.match(ref)
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter() if _local(t.tag) == "t")


def _num(s: str):
    try:
        f = float(s)
    except ValueError:
        return s
    return int(f) if f == int(f) and abs(f) < 1e15 else f


def read_xlsx(src) -> dict:
    """Return ``{sheet_name: [[cell, ...], ...]}`` (insertion order = workbook order). ``src`` is a path or bytes."""
    data = src if isinstance(src, (bytes, bytearray)) else Path(src).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = [_text(si) for si in root if _local(si.tag) == "si"]
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
                rels[r.get("Id")] = r.get("Target")
        out: dict = {}
        sheets = [s for el in wb.iter() if _local(el.tag) == "sheets" for s in el if _local(s.tag) == "sheet"]
        for i, sh in enumerate(sheets, 1):
            rid = next((v for k, v in sh.attrib.items() if _local(k) == "id"), None)
            target = rels.get(rid, f"worksheets/sheet{i}.xml")
            target = target.lstrip("/")
            path = target if target.startswith("xl/") else "xl/" + target
            if path not in names:
                continue
            rows: list[list] = []
            for row in ET.fromstring(z.read(path)).iter():
                if _local(row.tag) != "row":
                    continue
                cells: list = []
                for c in row:
                    if _local(c.tag) != "c":
                        continue
                    idx = _col_index(c.get("r", ""))
                    while len(cells) < idx:
                        cells.append("")
                    t = c.get("t")
                    v = next((x for x in c if _local(x.tag) == "v"), None)
                    if t == "inlineStr":
                        val = _text(c)
                    elif t == "s" and v is not None and v.text is not None:
                        k = int(v.text)
                        val = shared[k] if k < len(shared) else ""
                    elif t in ("str", "e") and v is not None:
                        val = v.text or ""
                    elif t == "b" and v is not None:
                        val = (v.text or "0") == "1"
                    elif v is not None and v.text is not None:
                        val = _num(v.text)
                    else:
                        val = ""
                    cells.append(val)
                rows.append(cells)
            while rows and all(c == "" for c in rows[-1]):
                rows.pop()
            out[sh.get("name", f"Sheet{i}")] = rows
        return out


def _col_letters(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_xlsx(path, sheets: dict) -> None:
    """Deterministic minimal xlsx writer (inline strings, fixed zip timestamps) used for sample fixtures and tests."""
    names = list(sheets)
    parts: dict[str, str] = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                  for i in range(1, len(names) + 1)) + "</Types>",
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
        + "".join(f'<sheet name="{_esc(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, 1)) + "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                  for i in range(1, len(names) + 1)) + "</Relationships>",
    }
    for i, n in enumerate(names, 1):
        rows_xml = []
        for r, row in enumerate(sheets[n], 1):
            cells = []
            for c, v in enumerate(row):
                ref = f"{_col_letters(c)}{r}"
                if v is None or v == "":
                    continue
                if isinstance(v, bool):
                    cells.append(f'<c r="{ref}" t="b"><v>{int(v)}</v></c>')
                elif isinstance(v, (int, float)):
                    cells.append(f'<c r="{ref}"><v>{v}</v></c>')
                else:
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_esc(str(v))}</t></is></c>')
            rows_xml.append(f'<row r="{r}">{"".join(cells)}</row>')
        parts[f"xl/worksheets/sheet{i}.xml"] = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                                                f'<sheetData>{"".join(rows_xml)}</sheetData></worksheet>')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(parts):
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, parts[name])


def read_table(path, sheet: str | None = None) -> list[dict]:
    """Rows as dicts keyed by the header row. Supports .xlsx, .csv/.tsv, .json (list of objects or {"rows"/"items": [...]})."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        book = read_xlsx(p)
        if not book:
            return []
        rows = book[sheet] if sheet else next(iter(book.values()))
        return _rows_to_dicts(rows)
    if ext in (".csv", ".tsv", ".txt"):
        text = p.read_text(encoding="utf-8-sig")
        delim = "\t" if ext == ".tsv" else ","
        return [dict(r) for r in csv.DictReader(io.StringIO(text), delimiter=delim)]
    if ext == ".json":
        doc = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(doc, dict):
            for k in ("rows", "items", "value", "data", "results"):
                if isinstance(doc.get(k), list):
                    doc = doc[k]
                    break
        if not isinstance(doc, list):
            raise ValueError(f"{p.name}: expected a JSON list of objects")
        return [dict(r) for r in doc if isinstance(r, dict)]
    raise ValueError(f"unsupported table format: {p.name}")


def _rows_to_dicts(rows: list[list]) -> list[dict]:
    if not rows:
        return []
    header = [str(h).strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if all(c == "" for c in r):
            continue
        out.append({h: (r[i] if i < len(r) else "") for i, h in enumerate(header) if h})
    return out
