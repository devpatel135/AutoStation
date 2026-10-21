from __future__ import annotations

import zlib

import pytest

from wrapper_core.activities import pdf
from wrapper_core.activities._common import NotAvailable
from wrapper_core.retry import DataError


# --- a tiny PDF writer (just enough structure to exercise the extractor) -----------------------------------
def stream_obj(num: int, data: bytes, filters=None) -> bytes:
    f = ""
    if filters:
        f = " /Filter " + (filters[0] if len(filters) == 1 else "[" + " ".join(filters) + "]")
    return f"{num} 0 obj\n<< /Length {len(data)}{f} >>\nstream\n".encode() + data + b"\nendstream\nendobj\n"


def encode(data: bytes, mode: str):
    if mode == "plain":
        return data, None
    if mode == "flate":
        return zlib.compress(data), ["/FlateDecode"]
    if mode == "ahex_flate":
        return zlib.compress(data).hex().encode() + b">", ["/ASCIIHexDecode", "/FlateDecode"]
    raise ValueError(mode)


def build_pdf(page_contents, mode="plain", font="helv", kids_order=None, objstm=False, resources_on_pages_node=False, encrypt=False,
              cmap_text: "bytes | None" = None) -> bytes:
    """page_contents: list of content-stream bytes (page 1..n). Objects: 1 catalog, 2 pages, 3 font, 4 cmap, then content/page pairs."""
    n = len(page_contents)
    first_page = 10
    page_nums = [first_page + 2 * i for i in range(n)]
    content_nums = [p + 1 for p in page_nums]
    font_dict = {"helv": "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
                 "type0": "<< /Type /Font /Subtype /Type0 /BaseFont /Custom /ToUnicode 4 0 R >>"}[font]
    res = "<< /Font << /F1 3 0 R >> >>"
    kids = " ".join(f"{p} 0 R" for p in (kids_order and [page_nums[i] for i in kids_order] or page_nums))
    plain_objs = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {n}" + (f" /Resources {res}" if resources_on_pages_node else "") + " >>",
        3: font_dict,
    }
    for pn, cn in zip(page_nums, content_nums):
        plain_objs[pn] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {cn} 0 R"
                          + ("" if resources_on_pages_node else f" /Resources {res}") + " >>")
    out = [b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n"]
    if objstm:
        nums = sorted(plain_objs)
        bodies = [plain_objs[k].encode() for k in nums]
        offs, pos = [], 0
        for b in bodies:
            offs.append(pos)
            pos += len(b) + 1
        header = " ".join(f"{k} {o}" for k, o in zip(nums, offs)).encode() + b"\n"
        data = zlib.compress(header + b"\n".join(bodies))
        out.append(f"90 0 obj\n<< /Type /ObjStm /N {len(nums)} /First {len(header)} /Length {len(data)} /Filter /FlateDecode >>\nstream\n".encode()
                   + data + b"\nendstream\nendobj\n")
    else:
        for k in sorted(plain_objs):
            out.append(f"{k} 0 obj\n{plain_objs[k]}\nendobj\n".encode())
    if font == "type0":
        out.append(stream_obj(4, *_enc(cmap_text or b"", mode)))
    for cn, content in zip(content_nums, page_contents):
        out.append(stream_obj(cn, *_enc(content, mode)))
    trailer = "trailer\n<< /Root 1 0 R /Size 100" + (" /Encrypt 99 0 R" if encrypt else "") + " >>\nstartxref\n0\n%%EOF\n"
    out.append(trailer.encode())
    return b"".join(out)


def _enc(data: bytes, mode: str):
    d, f = encode(data, mode)
    return d, f


def write(tmp_path, data: bytes, name="t.pdf"):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


T = b"BT /F1 12 Tf 72 720 Td (Line one) Tj 0 -14 Td (Line two) Tj ET"


@pytest.mark.parametrize("mode", ["plain", "flate", "ahex_flate"])
def test_basic_text_in_all_stream_encodings(tmp_path, mode):
    assert pdf.extract_text(write(tmp_path, build_pdf([T], mode=mode))) == "Line one\nLine two"


def test_objstm_compressed_objects(tmp_path):
    assert pdf.extract_text(write(tmp_path, build_pdf([T], mode="flate", objstm=True))) == "Line one\nLine two"


def test_string_escapes_nesting_and_octal(tmp_path):
    c = rb"BT /F1 12 Tf 72 720 Td (Paren \(nested\) and \\ slash \101\102 tab\tend) Tj 0 -14 Td (a (b) c) Tj 0 -14 Td <48656C6C6F> Tj ET"
    got = pdf.extract_text(write(tmp_path, build_pdf([c])))
    assert got.splitlines() == ["Paren (nested) and \\ slash AB tab end", "a (b) c", "Hello"]


def test_tj_arrays_kerning_and_word_gaps(tmp_path):
    c = b"BT /F1 12 Tf 72 720 Td [(Hel) -20 (lo) -400 (World)] TJ ET"
    assert pdf.extract_text(write(tmp_path, build_pdf([c]))) == "Hello World"


def test_td_tm_and_quote_operators_make_lines_and_spaces(tmp_path):
    c = (b"BT /F1 12 Tf 1 0 0 1 72 700 Tm (Name:) Tj 1 0 0 1 150 700 Tm (Acme) Tj "      # same y: a space, not a newline
         b"1 0 0 1 72 680 Tm (Total:) Tj 50 0 Td (99) Tj T* (next) Tj 14 TL (quoted) ' ET")
    got = pdf.extract_text(write(tmp_path, build_pdf([c])))
    assert got.splitlines()[0] == "Name: Acme" and got.splitlines()[1] == "Total: 99"
    assert "next" in got and got.splitlines()[-1] == "quoted"


def test_pages_follow_the_page_tree_not_object_numbers(tmp_path):
    p1 = b"BT /F1 12 Tf 72 720 Td (FIRST) Tj ET"
    p2 = b"BT /F1 12 Tf 72 720 Td (SECOND) Tj ET"
    data = build_pdf([p1, p2], kids_order=[1, 0])                               # Kids lists page 2 first
    assert pdf.extract_pages(write(tmp_path, data)) == ["SECOND", "FIRST"]
    assert pdf.extract_text(write(tmp_path, data, "b.pdf")) == "SECOND\n\nFIRST"
    assert pdf.extract_text(write(tmp_path, data, "c.pdf"), max_pages=1) == "SECOND"


def test_resources_inherited_from_the_pages_node(tmp_path):
    cmap = b"1 begincodespacerange <0000> <FFFF> endcodespacerange 2 beginbfchar <0001> <0048> <0002> <0069> endbfchar"
    c = b"BT /F1 12 Tf 72 720 Td <00010002> Tj ET"
    data = build_pdf([c], font="type0", resources_on_pages_node=True, cmap_text=cmap)
    assert pdf.extract_text(write(tmp_path, data)) == "Hi"


def test_composite_font_with_tounicode_bfchar_and_bfrange(tmp_path):
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            b"2 beginbfchar\n<0001> <0048>\n<0002> <20AC>\nendbfchar\n"
            b"2 beginbfrange\n<0010> <0013> <0061>\n<0020> <0022> [<0058> <0059> <005A>]\nendbfrange\nendcmap")
    c = b"BT /F1 12 Tf 72 720 Td <0001001000110012> Tj <000200200021> Tj ET"
    data = build_pdf([c], mode="flate", font="type0", cmap_text=cmap)
    assert pdf.extract_text(write(tmp_path, data)) == "Habc€XY"


def test_unmapped_codes_do_not_crash(tmp_path):
    cmap = b"1 begincodespacerange <0000> <FFFF> endcodespacerange 1 beginbfchar <0001> <0041> endbfchar"
    data = build_pdf([b"BT /F1 12 Tf <00010099> Tj ET"], font="type0", cmap_text=cmap)
    assert pdf.extract_text(write(tmp_path, data)) == "A"


def test_encrypted_and_non_pdf_and_empty(tmp_path):
    with pytest.raises(DataError, match="encrypted"):
        pdf.extract_text(write(tmp_path, build_pdf([T], encrypt=True)))
    with pytest.raises(DataError):
        pdf.extract_text(write(tmp_path, b"hello, not a pdf", "x.pdf"))
    scanned = build_pdf([b"q 612 0 0 792 0 0 cm /Im0 Do Q"])                     # image-only page: no text operators
    assert pdf.extract_text(write(tmp_path, scanned, "scan.pdf")) == ""


def test_engine_selection(tmp_path):
    with pytest.raises(ValueError):
        pdf.extract_text(write(tmp_path, build_pdf([T])), engine="magic")
    try:
        import pdfminer  # noqa: F401
        pytest.skip("pdfminer.six is installed here")
    except ImportError:
        pass
    with pytest.raises(NotAvailable, match="pdfminer"):
        pdf.extract_text(write(tmp_path, build_pdf([T])), engine="pdfminer")
    assert pdf.extract_text(write(tmp_path, build_pdf([T]), "y.pdf"), engine="builtin") == "Line one\nLine two"
    assert pdf.extract_text(write(tmp_path, build_pdf([b"q Q"]), "z.pdf"), engine="auto") == ""     # empty + no pdfminer: empty, no error


def test_garbage_streams_are_skipped_not_fatal(tmp_path):
    good = build_pdf([T])
    broken = good.replace(b"/Length", b"/Filter /FlateDecode /Length", 1)           # first stream claims Flate but is not
    assert isinstance(pdf.extract_pages(write(tmp_path, broken)), list)
