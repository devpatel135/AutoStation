from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "site"))

import check_site as cs  # noqa: E402
import site_common as sc  # noqa: E402

PAGE = ("<!doctype html><html><head><title>t</title></head><body>"
        '<button class="tab" onclick="show(\'arch\')">A</button><button class="tab" onclick="show(\'flow\')">F</button>'
        "<script>function show(t){return t}</script></body></html>")


def csp_of(html: str) -> str:
    return re.search(r'Content-Security-Policy" content="([^"]*)"', html).group(1)


def test_standalone_page_gets_hashes_instead_of_unsafe_inline():
    out = sc.harden_html(PAGE)
    script_src = re.search(r"script-src([^;]*)", csp_of(out)).group(1)
    assert "'unsafe-inline'" not in script_src and script_src.count("'sha256-") == 2     # the page script + the wiring script
    assert "onclick" not in out and out.count("data-show=") == 2
    assert sc.STANDALONE_WIRE_JS in out
    assert sc.harden_html(out) == out                                                    # idempotent


def test_unknown_inline_handlers_are_refused_not_silently_broken():
    with pytest.raises(ValueError, match="inline event handlers"):
        sc.harden_html(PAGE.replace("<script>", '<img src=x onerror="alert(1)"><script>'))
    with pytest.raises(ValueError):
        sc.harden_html('<html><head></head><body><a onmouseover="x()">y</a></body></html>')


def write_page(tmp_path, body: str, csp_scripts: str = "'self'") -> Path:
    html = ('<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src ' + csp_scripts +
            '"></head><body>' + body + "</body></html>")
    p = tmp_path / "p.html"
    p.write_text(html, encoding="utf-8")
    return p


def test_check_site_flags_unsafe_inline_scripts(tmp_path):
    info = cs.parse(write_page(tmp_path, "<p>x</p>", "'unsafe-inline'"))
    assert any("unsafe-inline" in m for m in cs._check_inline_scripts("p.html", info))


def test_check_site_flags_inline_handlers_and_unhashed_scripts(tmp_path):
    info = cs.parse(write_page(tmp_path, '<button onclick="x()">b</button>'))
    assert any("inline event handler" in m for m in cs._check_inline_scripts("p.html", info))
    info = cs.parse(write_page(tmp_path, "<script>steal()</script>"))
    assert any("not covered" in m for m in cs._check_inline_scripts("p.html", info))


def test_check_site_accepts_a_hashed_script(tmp_path):
    code = "var a=1;"
    info = cs.parse(write_page(tmp_path, f"<script>{code}</script>", sc.script_hashes(code).strip()))
    assert cs._check_inline_scripts("p.html", info) == []
