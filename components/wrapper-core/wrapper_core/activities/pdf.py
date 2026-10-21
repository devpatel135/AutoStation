"""PDF text extraction (UiPath: Read PDF Text).

A best-effort, dependency-free extractor for **text-based** PDFs: it understands the common structure of PDFs produced by Word,
LibreOffice, reportlab, browsers and most reporting tools (FlateDecode / ASCIIHex / ASCII85 streams, compressed object streams,
Tj/TJ/'/\" operators, ToUnicode CMaps for composite fonts, page order from the page tree).

What it deliberately does NOT do (be honest about it in your design):
  * scanned/image-only PDFs have no text to extract: OCR is needed (see ``images.ocr_image``);
  * layout (columns, tables) is approximated by line breaks - do not rely on it for table extraction;
  * encrypted PDFs raise DataError; text inside Form XObjects and fonts without a ToUnicode map (custom glyph encodings) can come
    out empty or garbled.
For harder documents install ``pdfminer.six``: ``extract_text(path, engine="pdfminer")`` (or ``engine="auto"``, the default, which
falls back to it when the built-in extractor finds nothing).
"""
from __future__ import annotations

import base64
import re
import zlib
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..retry import DataError
from ._common import NotAvailable, emit

_OBJ = re.compile(rb"(?<![\d.])(\d+)\s+(\d+)\s+obj\b")
_STREAM = re.compile(rb">>\s*stream(\r\n|\n|\r)")
_WS = b" \t\r\n\f\x00"
_DELIM = b"()<>[]{}/%"


class _CMap:
    def __init__(self):
        self.map: Dict[int, str] = {}
        self.width = 1


# --- object parsing ---------------------------------------------------------------------------------------
def _filters(dic: bytes) -> List[str]:
    i = dic.find(b"/Filter")
    if i < 0:
        return []
    seg = dic[i + 7:]
    m = re.match(rb"\s*\[([^\]]*)\]", seg)
    seg = m.group(1) if m else seg[:60]
    return [n.decode() for n in re.findall(rb"/([A-Za-z0-9]+)", seg) if n.decode() in
            ("FlateDecode", "Fl", "ASCIIHexDecode", "AHx", "ASCII85Decode", "A85", "LZWDecode", "LZW", "DCTDecode", "DCT",
             "CCITTFaxDecode", "CCF", "JBIG2Decode", "JPXDecode", "RunLengthDecode", "RL", "Crypt")]


def _decode_stream(dic: bytes, raw: bytes) -> Optional[bytes]:
    data = raw
    for f in _filters(dic):
        if f in ("FlateDecode", "Fl"):
            try:
                data = zlib.decompressobj().decompress(data)
            except zlib.error:
                try:  # tolerate a stray leading byte / truncated stream
                    d = zlib.decompressobj()
                    data = d.decompress(data[1:]) if data[:1] in (b"\r", b"\n") else d.decompress(data)
                except zlib.error:
                    return None
        elif f in ("ASCIIHexDecode", "AHx"):
            h = re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0])
            data = bytes.fromhex((h + b"0" if len(h) % 2 else h).decode())
        elif f in ("ASCII85Decode", "A85"):
            try:
                body = data.strip()
                body = body[2:] if body.startswith(b"<~") else body
                data = base64.a85decode(body.split(b"~>")[0], adobe=False)
            except ValueError:
                return None
        else:
            return None  # image / LZW / crypt filters: no text here
    return data


def _parse_objects(data: bytes) -> Dict[int, Tuple[bytes, Optional[bytes]]]:
    objs: Dict[int, Tuple[bytes, Optional[bytes]]] = {}
    for m in _OBJ.finditer(data):
        num = int(m.group(1))
        body = data[m.end(): (data.find(b"endobj", m.end()) if data.find(b"endobj", m.end()) >= 0 else len(data))]
        sm = _STREAM.search(body)
        if sm:
            dic = body[: sm.start() + 2]
            s = body[sm.end():]
            e = s.rfind(b"endstream")
            stream = (s[:e] if e >= 0 else s).rstrip(b"\r\n")
            objs[num] = (dic, stream)
        else:
            objs[num] = (body, None)
    # expand compressed object streams (PDF 1.5+)
    for num, (dic, stream) in list(objs.items()):
        if stream is not None and re.search(rb"/Type\s*/ObjStm", dic):
            dec = _decode_stream(dic, stream)
            n = re.search(rb"/N\s+(\d+)", dic)
            first = re.search(rb"/First\s+(\d+)", dic)
            if not dec or not n or not first:
                continue
            nums = [int(x) for x in re.findall(rb"\d+", dec[: int(first.group(1))])]
            pairs = list(zip(nums[0::2], nums[1::2]))[: int(n.group(1))]
            base = int(first.group(1))
            for i, (onum, off) in enumerate(pairs):
                end = pairs[i + 1][1] if i + 1 < len(pairs) else len(dec) - base
                objs.setdefault(onum, (dec[base + off: base + end], None))
    return objs


def _balanced(text: bytes, start: int) -> bytes:
    """text[start:] begins with '<<': return the balanced '<<...>>' slice."""
    depth, i = 0, start
    while i < len(text) - 1:
        two = text[i:i + 2]
        if two == b"<<":
            depth += 1
            i += 2
        elif two == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                return text[start:i]
        else:
            i += 1
    return text[start:]


def _ref(dic: bytes, key: bytes) -> Optional[int]:
    m = re.search(rb"/" + key + rb"\s+(\d+)\s+\d+\s+R", dic)
    return int(m.group(1)) if m else None


def _sub_dict(dic: bytes, key: bytes, objs) -> bytes:
    """The (possibly indirect) dictionary stored under /key in `dic`, else b''."""
    m = re.search(rb"/" + key + rb"\b\s*", dic)
    if not m:
        return b""
    rest = dic[m.end():]
    if rest.startswith(b"<<"):
        return _balanced(rest, 0)
    r = re.match(rb"(\d+)\s+\d+\s+R", rest)
    return objs.get(int(r.group(1)), (b"", None))[0] if r else b""


# --- CMaps ------------------------------------------------------------------------------------------------
def _hex(s: bytes) -> bytes:
    h = re.sub(rb"\s", b"", s)
    return bytes.fromhex((h + b"0" if len(h) % 2 else h).decode())


def _u16(b: bytes) -> str:
    try:
        return b.decode("utf-16-be")
    except UnicodeDecodeError:
        return ""


def _parse_cmap(text: bytes) -> _CMap:
    cm = _CMap()
    cs = re.search(rb"begincodespacerange\s*<([0-9A-Fa-f\s]+)>", text)
    if cs:
        cm.width = max(1, len(_hex(cs.group(1))))
    for blk in re.findall(rb"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]*)>", blk):
            s = _hex(src)
            cm.width = max(cm.width, len(s)) if not cs else cm.width
            cm.map[int.from_bytes(s, "big")] = _u16(_hex(dst))
    for blk in re.findall(rb"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, dst in re.findall(rb"<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]+)>\s*(<[0-9A-Fa-f\s]*>|\[[^\]]*\])", blk):
            a, b = int.from_bytes(_hex(lo), "big"), int.from_bytes(_hex(hi), "big")
            if b - a > 70000:
                continue
            if dst.startswith(b"["):
                for k, d in enumerate(re.findall(rb"<([0-9A-Fa-f\s]*)>", dst)):
                    if a + k <= b:
                        cm.map[a + k] = _u16(_hex(d))
            else:
                base = _hex(dst[1:-1])
                for k in range(b - a + 1):
                    code = int.from_bytes(base, "big") + k if base else 0
                    cm.map[a + k] = _u16(code.to_bytes(len(base), "big")) if base else ""
    return cm


# --- content streams ----------------------------------------------------------------------------------------
_ESC = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12, ord("("): 40, ord(")"): 41, ord("\\"): 92}


def _tokens(s: bytes) -> Iterator[Tuple[str, Any]]:
    i, n = 0, len(s)
    stack: List[List[Any]] = []

    def emit_tok(kind, val):
        if stack:
            stack[-1].append((kind, val))
            return None
        return (kind, val)

    while i < n:
        c = s[i:i + 1]
        if c in (b" ", b"\t", b"\r", b"\n", b"\f", b"\x00"):
            i += 1
        elif c == b"%":
            while i < n and s[i:i + 1] not in (b"\r", b"\n"):
                i += 1
        elif c == b"(":
            depth, i, buf = 1, i + 1, bytearray()
            while i < n and depth:
                ch = s[i]
                if ch == 92 and i + 1 < n:
                    nx = s[i + 1]
                    if nx in _ESC:
                        buf.append(_ESC[nx])
                        i += 2
                    elif 48 <= nx <= 55:
                        j = i + 1
                        while j < n and j < i + 4 and 48 <= s[j] <= 55:
                            j += 1
                        buf.append(int(s[i + 1:j], 8) & 0xFF)
                        i = j
                    elif nx in (10, 13):
                        i += 2 if not (nx == 13 and s[i + 2:i + 3] == b"\n") else 3
                    else:
                        buf.append(nx)
                        i += 2
                    continue
                if ch == 40:
                    depth += 1
                elif ch == 41:
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                buf.append(ch)
                i += 1
            t = emit_tok("str", bytes(buf))
            if t:
                yield t
        elif c == b"<":
            if s[i:i + 2] == b"<<":
                depth, i = 1, i + 2  # skip inline dictionaries (inline image params, marked content)
                while i < n and depth:
                    if s[i:i + 2] == b"<<":
                        depth, i = depth + 1, i + 2
                    elif s[i:i + 2] == b">>":
                        depth, i = depth - 1, i + 2
                    else:
                        i += 1
            else:
                j = s.find(b">", i)
                j = n if j < 0 else j
                try:
                    val = _hex(s[i + 1:j])
                except ValueError:
                    val = b""
                i = j + 1
                t = emit_tok("str", val)
                if t:
                    yield t
        elif c == b"[":
            stack.append([])
            i += 1
        elif c == b"]":
            i += 1
            if stack:
                items = stack.pop()
                t = emit_tok("arr", items)
                if t:
                    yield t
        elif c == b"/":
            j = i + 1
            while j < n and s[j:j + 1] not in _WS and s[j:j + 1] not in _DELIM:
                j += 1
            t = emit_tok("name", s[i + 1:j].decode("latin-1"))
            i = j
            if t:
                yield t
        elif c in (b")", b">", b"{", b"}"):
            i += 1
        else:
            j = i
            while j < n and s[j:j + 1] not in _WS and s[j:j + 1] not in _DELIM:
                j += 1
            word = s[i:j].decode("latin-1")
            i = max(j, i + 1)
            if re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)", word):
                t = emit_tok("num", float(word))
            else:
                t = ("op", word) if not stack else None
            if t:
                yield t


def _decode_text(b: bytes, cmap: Optional[_CMap]) -> str:
    if cmap and cmap.map:
        w = cmap.width
        return "".join(cmap.map.get(int.from_bytes(b[i:i + w], "big"), "") for i in range(0, len(b), w))
    return b.decode("cp1252", "replace")


def _page_text(content: bytes, fonts: Dict[str, _CMap]) -> str:
    out: List[str] = []
    stack: List[Tuple[str, Any]] = []
    cmap: Optional[_CMap] = None
    last_y: Optional[float] = None

    def nl():
        if out and not out[-1].endswith("\n"):
            out.append("\n")

    def sp():
        if out and not out[-1].endswith((" ", "\n")):
            out.append(" ")

    for kind, val in _tokens(content):
        if kind != "op":
            stack.append((kind, val))
            if len(stack) > 12:
                del stack[0]
            continue
        op = val
        if op == "Tf" and len(stack) >= 2 and stack[-2][0] == "name":
            cmap = fonts.get(stack[-2][1])
        elif op == "Tj" and stack and stack[-1][0] == "str":
            out.append(_decode_text(stack[-1][1], cmap))
        elif op == "TJ" and stack and stack[-1][0] == "arr":
            for k, v in stack[-1][1]:
                if k == "str":
                    out.append(_decode_text(v, cmap))
                elif k == "num" and v < -250:
                    sp()
        elif op in ("'", '"') and stack and stack[-1][0] == "str":
            nl()
            out.append(_decode_text(stack[-1][1], cmap))
        elif op in ("Td", "TD") and len(stack) >= 2 and stack[-1][0] == "num" and stack[-2][0] == "num":
            tx, ty = stack[-2][1], stack[-1][1]
            if abs(ty) > 0.01:
                nl()
            elif tx > 0.01:
                sp()
        elif op == "Tm" and len(stack) >= 6 and stack[-1][0] == "num":
            y = stack[-1][1]
            if last_y is not None and abs(y - last_y) > 1:
                nl()
            else:
                sp()
            last_y = y
        elif op == "T*":
            nl()
        elif op in ("ET", "BT"):
            nl()
        stack.clear()
    text = "".join(out)
    text = re.sub(r"[ \t]+", " ", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


# --- document level -----------------------------------------------------------------------------------------
def _page_order(objs) -> List[int]:
    kids: List[int] = []

    def walk(num: int, seen: set):
        if num in seen or num not in objs:
            return
        seen.add(num)
        dic = objs[num][0]
        if re.search(rb"/Type\s*/Pages\b", dic):
            m = re.search(rb"/Kids\s*\[([^\]]*)\]", dic)
            for k in re.findall(rb"(\d+)\s+\d+\s+R", m.group(1) if m else b""):
                walk(int(k), seen)
        elif re.search(rb"/Type\s*/Page\b", dic):
            kids.append(num)

    roots = [n for n, (d, _) in objs.items() if re.search(rb"/Type\s*/Pages\b", d) and b"/Parent" not in d]
    seen: set = set()
    for r in sorted(roots):
        walk(r, seen)
    if not kids:
        kids = sorted(n for n, (d, _) in objs.items() if re.search(rb"/Type\s*/Page\b", d))
    return kids


def _inherited(objs, num: int, key: bytes) -> bytes:
    seen = set()
    while num in objs and num not in seen:
        seen.add(num)
        d = objs[num][0]
        sub = _sub_dict(d, key, objs)
        if sub:
            return sub
        p = _ref(d, b"Parent")
        if p is None:
            break
        num = p
    return b""


def extract_pages(path: str) -> List[str]:
    """Text of each page, in page order, using the built-in extractor."""
    with open(path, "rb") as f:
        data = f.read()
    if not data.lstrip().startswith(b"%PDF"):
        raise DataError("not a PDF file")
    objs = _parse_objects(data)
    if re.search(rb"/Encrypt\s*(\d+\s+\d+\s+R|<<)", data):
        raise DataError("encrypted PDFs are not supported")
    cmaps: Dict[int, _CMap] = {}
    for num, (dic, _) in objs.items():
        if b"/ToUnicode" in dic:
            ref = _ref(dic, b"ToUnicode")
            if ref and ref in objs and objs[ref][1] is not None:
                dec = _decode_stream(objs[ref][0], objs[ref][1])
                if dec:
                    cmaps[num] = _parse_cmap(dec)
    pages: List[str] = []
    for pnum in _page_order(objs):
        pdic = objs[pnum][0]
        fonts: Dict[str, _CMap] = {}
        res = _inherited(objs, pnum, b"Resources")
        fdic = _sub_dict(res, b"Font", objs) if res else b""
        for name, ref in re.findall(rb"/([^\s/<>\[\]()]+)\s+(\d+)\s+\d+\s+R", fdic):
            cm = cmaps.get(int(ref))
            if cm:
                fonts[name.decode("latin-1")] = cm
        m = re.search(rb"/Contents\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", pdic)
        chunks: List[bytes] = []
        for ref in re.findall(rb"(\d+)\s+\d+\s+R", m.group(1) if m else b""):
            o = objs.get(int(ref))
            if o and o[1] is not None:
                dec = _decode_stream(o[0], o[1])
                if dec:
                    chunks.append(dec)
        pages.append(_page_text(b"\n".join(chunks), fonts))
    return pages


def extract_text(path: str, engine: str = "auto", max_pages: Optional[int] = None) -> str:
    """Text of a PDF (pages separated by a blank line). ``engine``: ``builtin``, ``pdfminer`` or ``auto`` (builtin, then
    pdfminer.six if the result is empty and it is installed). An empty string usually means a scanned PDF: use OCR."""
    if engine not in ("auto", "builtin", "pdfminer"):
        raise ValueError("engine must be auto|builtin|pdfminer")
    text = ""
    if engine in ("auto", "builtin"):
        pages = extract_pages(str(path))
        if max_pages:
            pages = pages[:max_pages]
        text = "\n\n".join(pages).strip()
    if engine == "pdfminer" or (engine == "auto" and not text):
        try:
            from pdfminer.high_level import extract_text as _pm  # type: ignore
        except ImportError:
            if engine == "pdfminer":
                raise NotAvailable("pdfminer.six is not installed (pip install pdfminer.six)") from None
        else:
            text = (_pm(str(path), maxpages=max_pages or 0) or "").strip()
    emit("pdf.extract_text", chars=len(text), empty=not text)
    return text
