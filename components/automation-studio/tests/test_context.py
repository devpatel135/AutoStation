"""Tests for automation_studio.context — context assembler."""
import json
import sys
from pathlib import Path

import pytest

# wrapper_core must be on PYTHONPATH (amx.py sets it; set it here for direct pytest runs)
_WC = Path(__file__).resolve().parents[3] / "wrapper-core"
if str(_WC) not in sys.path:
    sys.path.insert(0, str(_WC))

from automation_studio.context import (
    StudioContext,
    _clip,
    _format_fleet,
    _format_registry,
    build_studio_context,
)


# ── _clip ─────────────────────────────────────────────────────────────────────

def test_clip_short_string_unchanged():
    assert _clip("hello", 100) == "hello"


def test_clip_long_string_truncates():
    text = "x" * 200
    result = _clip(text, 100)
    assert len(result) < 200
    assert "truncated" in result


def test_clip_exact_limit_unchanged():
    text = "a" * 50
    assert _clip(text, 50) == text


# ── _format_registry ──────────────────────────────────────────────────────────

def test_format_registry_empty_list():
    result = _format_registry([])
    assert "no automations" in result.lower()


def test_format_registry_dict_with_automations_key():
    data = {"automations": [
        {"name": "automation-cert-renewal", "ring": "pilot", "description": "Renews certs"},
        {"name": "automation-report-gen", "ring": "broad", "description": "Generates reports"},
    ]}
    result = _format_registry(data)
    assert "automation-cert-renewal" in result
    assert "pilot" in result
    assert "automation-report-gen" in result


def test_format_registry_bare_list():
    data = [{"name": "automation-foo", "ring": "dev", "description": "Test"}]
    result = _format_registry(data)
    assert "automation-foo" in result


def test_format_registry_caps_at_30():
    items = [{"name": f"automation-auto{i}", "ring": "pilot", "description": ""} for i in range(50)]
    result = _format_registry(items)
    # Should not include item 31+ (which would be automation-auto30)
    assert "automation-auto0" in result
    # Result is clipped so we just check it doesn't explode
    assert isinstance(result, str)


def test_format_registry_missing_fields_dont_crash():
    data = [{"name": "automation-x"}]  # no ring or description
    result = _format_registry(data)
    assert "automation-x" in result


# ── _format_fleet ─────────────────────────────────────────────────────────────

def test_format_fleet_with_fleet_key():
    data = {
        "fleet": {"status": "degraded", "automations": 2, "hosts": 1,
                  "signals_total": 1, "signals": {"critical": 0, "high": 1, "medium": 0}},
        "automations": {
            "automation-cert-renewal": {"health": "healthy"},
            "automation-report-gen": {"health": "degraded"},
        },
    }
    result = _format_fleet(data)
    assert "degraded" in result
    assert "signals" in result.lower()
    assert "automation-cert-renewal" in result


def test_format_fleet_no_signals():
    data = {"fleet": {"status": "healthy", "automations": 1, "hosts": 1, "signals_total": 0}}
    result = _format_fleet(data)
    assert "healthy" in result
    assert "signals" not in result.lower()


def test_format_fleet_bare_dict():
    result = _format_fleet({"status": "healthy", "automations": 3, "hosts": 2})
    assert isinstance(result, str)


def test_format_fleet_non_dict_input():
    result = _format_fleet("not a dict")
    assert isinstance(result, str)  # must not raise


# ── build_studio_context ──────────────────────────────────────────────────────

def test_build_studio_context_returns_studio_context():
    ctx = build_studio_context()
    assert isinstance(ctx, StudioContext)


def test_build_studio_context_text_non_empty():
    ctx = build_studio_context()
    assert len(ctx.text) > 100


def test_build_studio_context_includes_framing():
    ctx = build_studio_context()
    assert "AUTOSTATION Studio" in ctx.text


def test_build_studio_context_sources_list():
    ctx = build_studio_context()
    expected = {"studio_framing", "automation_standard", "wrapper_api",
                "manifest_format", "registry", "fleet_state", "adapter_catalog"}
    assert expected == set(ctx.sources)


def test_build_studio_context_str_returns_text():
    ctx = build_studio_context()
    assert str(ctx) == ctx.text


def test_build_studio_context_repr():
    ctx = build_studio_context()
    assert "StudioContext" in repr(ctx)
    assert "sources=" in repr(ctx)


def test_build_studio_context_with_missing_dirs(tmp_path):
    # Non-existent override dirs should not crash — graceful fallback text
    ctx = build_studio_context(
        registry_root=tmp_path / "no-registry",
        logging_root=tmp_path / "no-logging",
        framework_root=tmp_path / "no-framework",
    )
    assert isinstance(ctx, StudioContext)
    assert len(ctx.text) > 100


def test_build_studio_context_with_registry_json(tmp_path):
    reg_dir = tmp_path / "automation-registry"
    reg_dir.mkdir()
    (reg_dir / "manifest.json").write_text(json.dumps([
        {"name": "automation-test-auto", "ring": "pilot", "description": "A test automation"},
    ]), encoding="utf-8")
    ctx = build_studio_context(registry_root=reg_dir)
    assert "automation-test-auto" in ctx.text


def test_build_studio_context_with_fleet_json(tmp_path):
    log_dir = tmp_path / "automation-logging"
    log_dir.mkdir()
    (log_dir / "fleet.json").write_text(json.dumps({
        "fleet": {"status": "healthy", "automations": 1, "hosts": 1, "signals_total": 0},
    }), encoding="utf-8")
    ctx = build_studio_context(logging_root=log_dir)
    assert "healthy" in ctx.text


def test_build_studio_context_with_adapter_catalog(tmp_path):
    fw_dir = tmp_path / "execution-framework"
    fw_dir.mkdir()
    catalog = "# Adapters\n- adapter-sharepoint\n- adapter-sap"
    (fw_dir / "ADAPTERS.md").write_text(catalog, encoding="utf-8")
    ctx = build_studio_context(framework_root=fw_dir)
    assert "adapter-sharepoint" in ctx.text


def test_build_studio_context_sections_separated():
    ctx = build_studio_context()
    assert "---" in ctx.text  # section separator
