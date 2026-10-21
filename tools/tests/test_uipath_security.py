"""Hardening of the UiPath inventory: files and archives are attacker-supplied, so DTD/entity declarations are rejected in
every encoding and archives are capped while streaming."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UIPATH = ROOT / "tools" / "uipath"
sys.path.insert(0, str(UIPATH))

import inventory as inv  # noqa: E402
import xlsxmini  # noqa: E402
import xmlsafe  # noqa: E402
from test_uipath_inventory import HEADER, make_project  # noqa: E402

BOMB = '<?xml version="1.0" encoding="{enc}"?><!DOCTYPE a [<!ENTITY x "y">]><Activity>&x;</Activity>'


def _parse(tmp_path: Path, raw: bytes) -> dict:
    proj = make_project(tmp_path / "p", "P", "")
    f = proj / "Evil.xaml"
    f.write_bytes(raw)
    return inv.parse_workflow(proj, f, {})


CASES = [
    ("utf-8", BOMB.format(enc="utf-8").encode("utf-8")),
    ("utf-8-bom", b"\xef\xbb\xbf" + BOMB.format(enc="utf-8").encode("utf-8")),
    ("utf-16-bom", BOMB.format(enc="UTF-16").encode("utf-16")),
    ("utf-16-le-nobom", BOMB.format(enc="UTF-16").encode("utf-16-le")),
    ("utf-16-be-nobom", BOMB.format(enc="UTF-16").encode("utf-16-be")),
    ("utf-32", BOMB.format(enc="UTF-32").encode("utf-32")),
    ("padded-comment", ('<?xml version="1.0"?><!-- ' + "x" * 60_000 + " -->" + '<!DOCTYPE a [<!ENTITY x "y">]><Activity>&x;</Activity>').encode()),
    ("padded-whitespace", ('<?xml version="1.0"?>' + " " * 70_000 + '<!DOCTYPE a [<!ENTITY x "y">]><Activity>&x;</Activity>').encode()),
    ("padded-utf16-comment", ('<?xml version="1.0" encoding="UTF-16"?><!-- ' + "x" * 40_000 + ' --><!DOCTYPE a [<!ENTITY x "y">]><Activity/>').encode("utf-16")),
    ("mixed-case", b'<?xml version="1.0"?><!DocType a [<!eNtItY x "y">]><Activity/>'),
]


@pytest.mark.parametrize("label,raw", CASES, ids=[c[0] for c in CASES])
def test_dtd_and_entities_are_rejected_in_every_encoding_and_padding(tmp_path, label, raw):
    wf = _parse(tmp_path, raw)
    assert wf["parse_error"] == "DTD/entity declarations are not allowed", label
    assert wf["activities"] == {}


def test_a_normal_workflow_in_utf16_still_parses(tmp_path):
    doc = ('<?xml version="1.0" encoding="UTF-16"?>' + HEADER + '<ui:LogMessage Message="hi" /></Activity>').encode("utf-16")
    wf = _parse(tmp_path, doc)
    assert wf["parse_error"] is None and wf["log_messages"] == 1


def test_contains_dtd_helper():
    assert xmlsafe.contains_dtd(b"<!DOCTYPE x>") and xmlsafe.contains_dtd("<!ENTITY a 'b'>".encode("utf-16-le"))
    assert not xmlsafe.contains_dtd(b"<Activity><Sequence/></Activity>")


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def test_archive_member_count_cap(tmp_path):
    arc = _zip(tmp_path / "many.nupkg", {f"f{i}.txt": b"x" for i in range(6)})
    with pytest.raises(xmlsafe.ArchiveLimitError):
        xmlsafe.extract_zip(arc, tmp_path / "out", max_members=5)
    assert xmlsafe.extract_zip(arc, tmp_path / "ok", max_members=6) == 6


def test_archive_declared_total_size_cap(tmp_path):
    arc = _zip(tmp_path / "big.nupkg", {"a.bin": b"\0" * 2_000_000, "b.bin": b"\0" * 2_000_000})   # compresses to a few KB
    with pytest.raises(xmlsafe.ArchiveLimitError):
        xmlsafe.extract_zip(arc, tmp_path / "out", max_total=3_000_000)


def test_archive_streaming_per_member_cap_stops_the_write(tmp_path):
    arc = _zip(tmp_path / "one.nupkg", {"a.bin": b"\0" * 2_000_000})
    with pytest.raises(xmlsafe.ArchiveLimitError):
        xmlsafe.extract_zip(arc, tmp_path / "out", max_member=1_000_000)
    assert (tmp_path / "out" / "a.bin").stat().st_size <= 1_000_000 + xmlsafe._CHUNK       # never wrote the whole bomb


def test_scan_rejects_a_bomb_nupkg_and_skips_a_nested_one(tmp_path, monkeypatch):
    monkeypatch.setattr(xmlsafe, "MAX_MEMBERS", 3)
    bomb = _zip(tmp_path / "Bomb.nupkg", {f"lib/{i}.xaml": b"<a/>" for i in range(10)})
    with pytest.raises(ValueError):
        inv.scan(bomb)
    folder = tmp_path / "folder"
    make_project(folder / "Good", "Good", "")
    _zip(folder / "Nested.nupkg", {f"lib/{i}.xaml": b"<a/>" for i in range(10)})
    assert [p["name"] for p in inv.scan(folder)["projects"]] == ["Good"]               # the bomb is skipped, the scan completes


def test_zip_slip_is_still_refused(tmp_path):
    evil = _zip(tmp_path / "evil.nupkg", {"../../escape.txt": b"x"})
    with pytest.raises(ValueError):
        xmlsafe.extract_zip(evil, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_xlsx_with_a_dtd_is_rejected(tmp_path):
    wb = _zip(tmp_path / "evil.xlsx", {"xl/workbook.xml": b'<!DOCTYPE x [<!ENTITY a "b">]><workbook><sheets/></workbook>'})
    with pytest.raises(xmlsafe.DtdNotAllowed):
        xlsxmini.read_xlsx(wb)
    good = tmp_path / "good.xlsx"
    xlsxmini.write_xlsx(good, {"Settings": [["Name", "Value"], ["a", "b"]]})
    assert xlsxmini.read_xlsx(good)["Settings"][1] == ["a", "b"]
