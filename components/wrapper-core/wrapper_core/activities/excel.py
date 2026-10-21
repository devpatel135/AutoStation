"""Excel / CSV activities (UiPath: Read Range, Write Range, Append Range, Filter Data Table, Join, Remove Duplicate Rows).

Pure standard library: a minimal ``.xlsx`` reader and writer (zipfile + ElementTree), CSV with encoding/delimiter sniffing,
and small table helpers over ``list[dict]``. No Excel installation, no COM, no macros. Honest limits: formulas are read as
their cached values (the file must have been saved by Excel/LibreOffice), styles/charts/pivots are neither read nor
preserved by ``append_xlsx`` (it rewrites the sheet data), and ``.xls``/``.xlsb`` are not supported.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import os
import re
import tempfile
import zipfile
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _esc

from ..retry import DataError
from ._common import emit

Row = Dict[str, Any]

# --- helpers --------------------------------------------------------------------------------------------
_BUILTIN_DATE_IDS = set(range(14, 23)) | {27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 45, 46, 47, 50, 51, 52, 53, 54, 55, 56, 57, 58}
_EPOCH_1900 = _dt.datetime(1899, 12, 30)
_EPOCH_1904 = _dt.datetime(1904, 1, 1)
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Hostile-workbook limits (an .xlsx is a zip of XML: zip bombs and XML entity bombs are the classic ways to exhaust a worker).
MAX_MEMBERS = 10_000
MAX_PART_BYTES = 256 * 1024 * 1024        # one XML part, uncompressed
MAX_TOTAL_BYTES = 500 * 1024 * 1024       # all parts read from one workbook
MAX_ROWS, MAX_COLS = 1_048_576, 16_384    # Excel's own sheet limits
MAX_GRID_CELLS = 50_000_000               # dense grid we are willing to materialise
_DOCTYPE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.I)


def _ln(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def col_letter(idx: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA"""
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def _is_date_format(code: str) -> bool:
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.|_.|\*.', "", code)  # drop quoted text, [Red]/[$-409], escapes, padding
    return bool(re.search(r"[dmyhs]", code, re.I)) and not re.search(r"^(0|#|General)", code.strip())


def _from_serial(value: float, epoch: _dt.datetime):
    if value >= 61 or epoch is _EPOCH_1904:
        base = epoch
    else:
        base = _dt.datetime(1899, 12, 31)
    dt = base + _dt.timedelta(days=value)
    if abs(value - int(value)) < 1e-9:
        return dt.date()
    # float serials carry sub-second noise: round to the nearest second
    return dt.replace(microsecond=0) + _dt.timedelta(seconds=1 if dt.microsecond >= 500000 else 0)


def _to_serial(value) -> float:
    if isinstance(value, _dt.datetime):
        delta = value.replace(tzinfo=None) - _EPOCH_1900
        return delta.days + delta.seconds / 86400 + delta.microseconds / 86400e6
    d = _dt.datetime(value.year, value.month, value.day)
    return float((d - _EPOCH_1900).days)


# --- xlsx reader ----------------------------------------------------------------------------------------
class _Book:
    def __init__(self, path: "str | os.PathLike"):
        try:
            self.z = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise DataError(f"not a readable .xlsx file: {path}") from exc
        infos = self.z.infolist()
        if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_TOTAL_BYTES * 8:
            self.z.close()
            raise DataError(f"{path}: workbook has too many parts or an implausible uncompressed size")
        self._read_total = 0
        names = set(self.z.namelist())
        if "xl/workbook.xml" not in names:
            raise DataError(f"{path} is not an OOXML workbook (xl/workbook.xml missing)")
        self.date1904 = False
        self.sheets: List[Tuple[str, str]] = []  # (name, part)
        self._load_workbook()
        self.shared = self._load_shared()
        self.date_xfs = self._load_styles()

    def close(self):
        self.z.close()

    def _xml(self, part: str):
        """Parse one workbook part with a streaming size cap (declared sizes are not trusted) and DOCTYPE/ENTITY rejected
        (no entity expansion is ever needed for OOXML, so any DTD is treated as an attack)."""
        try:
            with self.z.open(part) as f:
                data = f.read(MAX_PART_BYTES + 1)
        except KeyError:
            raise DataError(f"workbook part {part} is missing") from None
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError) as exc:
            raise DataError(f"workbook part {part} is unreadable: {type(exc).__name__}") from exc
        if len(data) > MAX_PART_BYTES:
            raise DataError(f"workbook part {part} exceeds {MAX_PART_BYTES} bytes uncompressed")
        self._read_total += len(data)
        if self._read_total > MAX_TOTAL_BYTES:
            raise DataError(f"workbook expands beyond {MAX_TOTAL_BYTES} bytes (zip bomb?)")
        if data.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00")) or data[1:2] == b"\x00":
            raise DataError(f"workbook part {part} uses an unsupported (UTF-16/32) encoding")
        if _DOCTYPE.search(data):
            raise DataError(f"workbook part {part} contains a DOCTYPE/ENTITY declaration (refused: XML entity bomb / XXE)")
        try:
            return ET.fromstring(data)
        except ET.ParseError as exc:
            raise DataError(f"workbook part {part} is not valid XML: {exc}") from exc

    def _load_workbook(self):
        wb = self._xml("xl/workbook.xml")
        rels = {}
        if "xl/_rels/workbook.xml.rels" in self.z.namelist():
            for r in self._xml("xl/_rels/workbook.xml.rels"):
                rels[r.get("Id")] = r.get("Target")
        for el in wb.iter():
            ln = _ln(el.tag)
            if ln == "workbookPr" and el.get("date1904") in ("1", "true"):
                self.date1904 = True
            if ln == "sheet":
                rid = next((v for k, v in el.attrib.items() if _ln(k) == "id"), None)
                target = rels.get(rid, "")
                part = target.lstrip("/") if target.startswith("/") else "xl/" + target
                self.sheets.append((el.get("name", ""), part))

    def _load_shared(self) -> List[str]:
        if "xl/sharedStrings.xml" not in self.z.namelist():
            return []
        out = []
        for si in self._xml("xl/sharedStrings.xml"):
            if _ln(si.tag) != "si":
                continue
            parts = []
            for child in si:  # <t> directly, or <r><t> runs; <rPh> (phonetic) is skipped
                ln = _ln(child.tag)
                if ln == "t":
                    parts.append(child.text or "")
                elif ln == "r":
                    parts.extend((n.text or "") for n in child if _ln(n.tag) == "t")
            out.append("".join(parts))
        return out

    def _load_styles(self) -> set:
        if "xl/styles.xml" not in self.z.namelist():
            return set()
        root = self._xml("xl/styles.xml")
        custom = {}
        xfs: List[int] = []
        for el in root.iter():
            ln = _ln(el.tag)
            if ln == "numFmt":
                custom[int(el.get("numFmtId"))] = el.get("formatCode", "")
            elif ln == "cellXfs":
                xfs = [int(x.get("numFmtId", "0")) for x in el if _ln(x.tag) == "xf"]
        date_xfs = set()
        for i, fid in enumerate(xfs):
            if fid in custom:
                if _is_date_format(custom[fid]):
                    date_xfs.add(i)
            elif fid in _BUILTIN_DATE_IDS:
                date_xfs.add(i)
        return date_xfs

    def sheet_part(self, sheet) -> Tuple[str, str]:
        if not self.sheets:
            raise DataError("workbook has no sheets")
        if sheet is None:
            return self.sheets[0]
        if isinstance(sheet, int):
            try:
                return self.sheets[sheet]
            except IndexError:
                raise DataError(f"sheet index {sheet} out of range (workbook has {len(self.sheets)})") from None
        for name, part in self.sheets:
            if name == sheet:
                return name, part
        raise DataError(f"sheet {sheet!r} not found; available: {[n for n, _ in self.sheets]}")

    def grid(self, sheet) -> Tuple[str, List[List[Any]]]:
        name, part = self.sheet_part(sheet)
        epoch = _EPOCH_1904 if self.date1904 else _EPOCH_1900
        rows: Dict[int, Dict[int, Any]] = {}
        root = self._xml(part)
        for row_el in root.iter():
            if _ln(row_el.tag) != "row":
                continue
            rnum = int(row_el.get("r") or (max(rows) + 1 if rows else 1))
            if not 1 <= rnum <= MAX_ROWS:
                raise DataError(f"row number {rnum} is outside the sheet limits")
            cells: Dict[int, Any] = {}
            auto_col = 0
            for c in row_el:
                if _ln(c.tag) != "c":
                    continue
                ref = c.get("r")
                if ref:
                    m = re.match(r"([A-Za-z]+)", ref)
                    ci = col_index(m.group(1)) if m else auto_col
                else:
                    ci = auto_col
                if ci >= MAX_COLS:
                    raise DataError(f"column index {ci} is outside the sheet limits")
                auto_col = ci + 1
                cells[ci] = self._cell_value(c, epoch)
            if cells:
                rows[rnum] = cells
        if not rows:
            return name, []
        width = max(max(c) for c in rows.values()) + 1
        if width * max(rows) > MAX_GRID_CELLS:
            raise DataError("sheet is too sparse/large to load (would need more than 50 million cells)")
        grid = []
        for rnum in range(1, max(rows) + 1):
            r = rows.get(rnum, {})
            grid.append([r.get(i) for i in range(width)])
        return name, grid

    def _cell_value(self, c, epoch):
        t = c.get("t", "n")
        v = None
        inline = None
        for ch in c:
            ln = _ln(ch.tag)
            if ln == "v":
                v = ch.text
            elif ln == "is":
                inline = "".join((n.text or "") for n in ch.iter() if _ln(n.tag) == "t")
        if t == "inlineStr":
            return inline if inline is not None else (v or "")
        if v is None:
            return None
        if t == "s":
            try:
                return self.shared[int(v)]
            except (ValueError, IndexError):
                raise DataError("corrupt shared string reference") from None
        if t == "b":
            return v in ("1", "true")
        if t in ("str", "e"):
            return v
        if t == "d":  # ISO 8601 date cell
            try:
                return _dt.datetime.fromisoformat(v)
            except ValueError:
                return v
        try:
            num = float(v)
        except ValueError:
            return v
        if int(c.get("s", "0")) in self.date_xfs:
            return _from_serial(num, epoch)
        return int(num) if num == int(num) and abs(num) < 1e15 and "." not in v and "E" not in v.upper() else num


def _rows_from_grid(grid: List[List[Any]], header: bool) -> List[Row]:
    if not grid:
        return []
    if not header:
        width = max(len(r) for r in grid)
        keys = [col_letter(i) for i in range(width)]
        return [{k: (r[i] if i < len(r) else None) for i, k in enumerate(keys)} for r in grid if any(x is not None for x in r)]
    start = next((i for i, r in enumerate(grid) if any(x not in (None, "") for x in r)), None)
    if start is None:
        return []
    keys: List[str] = []
    for i, h in enumerate(grid[start]):
        k = str(h).strip() if h not in (None, "") else f"col_{i + 1}"
        base, n = k, 2
        while k in keys:
            k = f"{base}_{n}"
            n += 1
        keys.append(k)
    out = []
    for r in grid[start + 1:]:
        if all(x in (None, "") for x in r):
            continue
        out.append({k: (r[i] if i < len(r) else None) for i, k in enumerate(keys)})
    return out


def sheet_names(path: "str | os.PathLike") -> List[str]:
    b = _Book(path)
    try:
        return [n for n, _ in b.sheets]
    finally:
        b.close()


def read_xlsx(path: "str | os.PathLike", sheet: "str | int | None" = None, header: bool = True) -> List[Row]:
    """Read a worksheet as a list of dicts. ``header=False`` keys rows by column letter ("A", "B", ...). Dates come back as date/datetime."""
    b = _Book(path)
    try:
        name, grid = b.grid(sheet)
    finally:
        b.close()
    rows = _rows_from_grid(grid, header)
    emit("excel.read_xlsx", sheet=name, rows=len(rows))
    return rows


# --- xlsx writer ----------------------------------------------------------------------------------------
_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")


def _check_sheet_name(name: str) -> str:
    if not name or len(name) > 31 or _SHEET_BAD.search(name) or name.startswith("'") or name.endswith("'"):
        raise DataError(f"invalid sheet name {name!r} (1-31 chars, none of []:*?/\\)")
    return name


def _columns(rows: Sequence, columns: Optional[Sequence[str]]) -> List[str]:
    if columns:
        return list(columns)
    cols: List[str] = []
    for r in rows:
        if isinstance(r, dict):
            for k in r:
                if k not in cols:
                    cols.append(k)
    return cols


def _clean(s: str) -> str:
    return _esc(_INVALID_XML.sub("", s))


def _sheet_xml(rows: Sequence, columns: Optional[Sequence[str]], sst: Dict[str, int], sst_list: List[str]) -> str:
    cols = _columns(rows, columns)
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']

    def string_cell(ref, text, style=0):
        idx = sst.get(text)
        if idx is None:
            idx = sst[text] = len(sst_list)
            sst_list.append(text)
        s = f' s="{style}"' if style else ""
        return f'<c r="{ref}" t="s"{s}><v>{idx}</v></c>'

    def cell(ref, v):
        if v is None or v == "":
            return ""
        if isinstance(v, bool):
            return f'<c r="{ref}" t="b"><v>{int(v)}</v></c>'
        if isinstance(v, _dt.datetime):
            return f'<c r="{ref}" s="1"><v>{_to_serial(v)!r}</v></c>'
        if isinstance(v, _dt.date):
            return f'<c r="{ref}" s="2"><v>{int(_to_serial(v))}</v></c>'
        if isinstance(v, (int, float)):
            if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
                return string_cell(ref, str(v))
            return f'<c r="{ref}"><v>{v!r}</v></c>'
        return string_cell(ref, str(v))

    r = 1
    if cols:
        out.append(f'<row r="{r}">' + "".join(string_cell(f"{col_letter(i)}{r}", str(c), 3) for i, c in enumerate(cols)) + "</row>")
        r += 1
    for row in rows:
        values = [row.get(c) for c in cols] if isinstance(row, dict) else list(row)
        out.append(f'<row r="{r}">' + "".join(cell(f"{col_letter(i)}{r}", v) for i, v in enumerate(values)) + "</row>")
        r += 1
    out.append("</sheetData></worksheet>")
    return "".join(out)


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd\\ hh:mm:ss"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def write_workbook(path: "str | os.PathLike", sheets: Dict[str, Sequence], columns: Optional[Dict[str, Sequence[str]]] = None) -> str:
    """Write several sheets at once: ``{"Summary": rows, "Detail": rows}``. Atomic (temp file + rename)."""
    if not sheets:
        raise DataError("no sheets to write")
    names = [_check_sheet_name(n) for n in sheets]
    if len({n.lower() for n in names}) != len(names):
        raise DataError("duplicate sheet names")
    sst: Dict[str, int] = {}
    sst_list: List[str] = []
    parts = {n: _sheet_xml(list(rows), (columns or {}).get(n), sst, sst_list) for n, rows in sheets.items()}
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    for i in range(1, len(names) + 1))
          + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
          '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          + "".join(f'<sheet name="{_clean(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, 1)) + "</sheets></workbook>")
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                         for i in range(1, len(names) + 1))
               + f'<Relationship Id="rId{len(names) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
               f'<Relationship Id="rId{len(names) + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>')
    sst_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(sst_list)}" uniqueCount="{len(sst_list)}">'
               + "".join(f'<si><t xml:space="preserve">{_clean(s)}</t></si>' for s in sst_list) + "</sst>")
    dest = os.fspath(path)
    d = os.path.dirname(os.path.abspath(dest))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".part")
    os.close(fd)
    try:
        info = lambda n: zipfile.ZipInfo(n, date_time=(2020, 1, 1, 0, 0, 0))  # noqa: E731 - reproducible output
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in [("[Content_Types].xml", ct), ("_rels/.rels", root_rels), ("xl/workbook.xml", wb),
                               ("xl/_rels/workbook.xml.rels", wb_rels), ("xl/styles.xml", _STYLES), ("xl/sharedStrings.xml", sst_xml)]:
                z.writestr(info(name), data)
            for i, n in enumerate(names, 1):
                z.writestr(info(f"xl/worksheets/sheet{i}.xml"), parts[n])
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    emit("excel.write_xlsx", sheets=len(names), rows=sum(len(list(r)) for r in sheets.values()))
    return dest


def write_xlsx(path: "str | os.PathLike", rows: Sequence, sheet: str = "Sheet1", columns: Optional[Sequence[str]] = None) -> str:
    """Write one sheet from ``list[dict]`` (header from the keys, or from `columns`) or ``list[list]`` (with `columns` for a header)."""
    return write_workbook(path, {sheet: rows}, {sheet: columns} if columns else None)


def append_xlsx(path: "str | os.PathLike", rows: Sequence[Row], sheet: str = "Sheet1") -> str:
    """Append rows to a sheet (creating the file if needed). Rewrites the sheet data: formatting is NOT preserved."""
    if not os.path.exists(path):
        return write_xlsx(path, rows, sheet)
    b = _Book(path)
    try:
        book = {}
        for name, _ in b.sheets:
            book[name] = _rows_from_grid(b.grid(name)[1], True)
    finally:
        b.close()
    book.setdefault(sheet, [])
    book[sheet] = book[sheet] + list(rows)
    return write_workbook(path, book)


# --- csv ------------------------------------------------------------------------------------------------
def _decode(raw: bytes, encoding: Optional[str]) -> Tuple[str, str]:
    if encoding:
        return raw.decode(encoding), encoding
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "cp1252"


def read_csv(path: "str | os.PathLike", delimiter: Optional[str] = None, encoding: Optional[str] = None, header: bool = True) -> List[Row]:
    """Read a CSV with BOM/utf-8/utf-16/cp1252 sniffing and delimiter sniffing (, ; tab |)."""
    with open(path, "rb") as f:
        text, enc = _decode(f.read(), encoding)
    if delimiter is None:
        sample = text[:8192]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            first = sample.splitlines()[0] if sample.splitlines() else ""
            delimiter = max(",;\t|", key=first.count) if first else ","
    grid = [row for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)]
    grid = [[(c if c != "" else None) for c in r] for r in grid]
    rows = _rows_from_grid(grid, header)
    emit("excel.read_csv", rows=len(rows), encoding=enc)
    return rows


_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any, escape_formulas: bool = True) -> Any:
    """CSV/formula-injection guard (OWASP): a text cell that Excel/LibreOffice would evaluate as a formula (leading
    ``= + - @``, tab or CR) is prefixed with an apostrophe so it is shown as text. Numbers and dates are untouched."""
    if escape_formulas and isinstance(value, str) and value.startswith(_FORMULA_LEAD):
        return "'" + value
    return value


def write_csv(path: "str | os.PathLike", rows: Sequence[Row], columns: Optional[Sequence[str]] = None, delimiter: str = ",",
              encoding: str = "utf-8", escape_formulas: bool = True) -> str:
    """Write ``rows`` as CSV. Text cells that start with ``= + - @ \\t \\r`` are neutralised (``'`` prefix) so opening the file
    in Excel cannot execute them; pass ``escape_formulas=False`` for machine-to-machine files that must round-trip verbatim."""
    cols = _columns(rows, columns)
    dest = os.fspath(path)
    d = os.path.dirname(os.path.abspath(dest))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".part")
    os.close(fd)
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            w = csv.writer(f, delimiter=delimiter)
            w.writerow([csv_safe(c, escape_formulas) for c in cols])
            for r in rows:
                w.writerow(["" if r.get(c) is None else csv_safe(r[c].isoformat() if hasattr(r[c], "isoformat") else r[c], escape_formulas)
                            for c in cols])
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    emit("excel.write_csv", rows=len(rows))
    return dest


# --- table helpers (UiPath data-table activities) ----------------------------------------------------------
def filter_rows(rows: Iterable[Row], predicate: Optional[Callable[[Row], bool]] = None, **equals: Any) -> List[Row]:
    """Keep rows where ``predicate(row)`` is true and every ``column=value`` keyword matches."""
    out = []
    for r in rows:
        if predicate is not None and not predicate(r):
            continue
        if all(r.get(k) == v for k, v in equals.items()):
            out.append(r)
    return out


def group_by(rows: Iterable[Row], key: "str | Sequence[str]") -> Dict[Any, List[Row]]:
    """Group rows by one column or a tuple of columns."""
    keys = [key] if isinstance(key, str) else list(key)
    out: Dict[Any, List[Row]] = {}
    for r in rows:
        k = r.get(keys[0]) if len(keys) == 1 else tuple(r.get(x) for x in keys)
        out.setdefault(k, []).append(r)
    return out


def aggregate(rows: Iterable[Row], key: "str | Sequence[str]", **aggs: Tuple[str, str]) -> List[Row]:
    """``aggregate(rows, "vendor", total=("amount", "sum"), n=("amount", "count"))`` with sum/count/min/max/avg."""
    keys = [key] if isinstance(key, str) else list(key)
    fns = {"sum": sum, "count": len, "min": min, "max": max, "avg": lambda v: sum(v) / len(v) if v else None}
    out = []
    for k, grp in group_by(rows, key).items():
        rec = dict(zip(keys, [k] if len(keys) == 1 else list(k)))
        for name, (col, fn) in aggs.items():
            if fn not in fns:
                raise ValueError(f"unknown aggregate {fn!r}")
            vals = [r.get(col) for r in grp if r.get(col) is not None] if fn != "count" else [r for r in grp]
            rec[name] = fns[fn](vals) if vals or fn in ("sum", "count") else None
        out.append(rec)
    return out


def join(left: Sequence[Row], right: Sequence[Row], on: "str | Sequence[str]", how: str = "inner", suffix: str = "_right") -> List[Row]:
    """Join two row lists on column(s). ``how``: inner | left. Colliding non-key columns from the right get `suffix`."""
    if how not in ("inner", "left"):
        raise ValueError("how must be 'inner' or 'left'")
    keys = [on] if isinstance(on, str) else list(on)
    index: Dict[Any, List[Row]] = {}
    for r in right:
        index.setdefault(tuple(r.get(k) for k in keys), []).append(r)
    out = []
    for lrow in left:
        matches = index.get(tuple(lrow.get(k) for k in keys), [])
        if not matches and how == "left":
            out.append(dict(lrow))
        for m in matches:
            rec = dict(lrow)
            for k, v in m.items():
                if k in keys:
                    continue
                rec[k + suffix if k in rec else k] = v
            out.append(rec)
    return out


def dedupe(rows: Iterable[Row], key: "str | Sequence[str] | None" = None, keep: str = "first") -> List[Row]:
    """Remove duplicate rows by `key` column(s) (default: the whole row). ``keep``: first | last."""
    if keep not in ("first", "last"):
        raise ValueError("keep must be 'first' or 'last'")
    keys = None if key is None else ([key] if isinstance(key, str) else list(key))
    seq = list(rows)
    seen: Dict[Any, int] = {}
    for i, r in enumerate(seq):
        k = tuple(r.get(x) for x in keys) if keys else tuple(sorted((a, repr(b)) for a, b in r.items()))
        if keep == "last" or k not in seen:
            seen[k] = i
    return [seq[i] for i in sorted(seen.values())]
