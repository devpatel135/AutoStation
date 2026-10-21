from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent.parent / "wrapper-core"))

from wrapper_core.testing import run_payload  # noqa: E402

FAST = {"max_retries": 3, "network_retry_wait_s": 0, "backoff_multiplier": 1.0, "skip_on_data_error": True}


def run(params=None, **kw):
    return run_payload(ROOT, params or {}, error_handling=FAST, **kw)


def test_default_run_succeeds():
    r = run()
    assert r.record["status"] == "success" and r.exit_code == 0
    assert r.record["items"]["success"] == 5 and r.record["result"]["processed"] == 5
    assert r.record["synopsis"]


def test_data_error_skips_one_item_and_run_continues():
    r = run({"simulate": "data"})
    assert r.record["status"] == "success"
    assert r.record["items"] == {"total": 5, "success": 4, "skipped": 1, "failed": 0}


def test_network_error_is_retried_by_the_platform():
    r = run({"simulate": "network"})
    assert r.record["status"] == "success"
    assert len([e for e in r.events("retry_event")]) == 2


def test_logic_error_fails_without_retry():
    r = run({"simulate": "logic"})
    assert r.record["status"] == "failed" and r.record["error"]["category"] == "LOGIC_ERROR"
    assert all(e["data"]["decision"] != "retry" for e in r.events("retry_event"))


def test_llm_pattern_1_with_and_without_llm():
    with_llm = run({"simulate": "flaky_llm", "items": "3"})
    assert with_llm.record["status"] == "success" and set(with_llm.record["result"]["labels"]) == {"ITEM-1", "ITEM-2", "ITEM-3"}
    assert with_llm.events("llm_decision")
    without = run({"simulate": "flaky_llm", "items": "3"}, llm=None)  # LLM unavailable -> deterministic fallback
    assert without.record["status"] == "success"
    assert without.record["result"]["labels"] == {"ITEM-1": "small", "ITEM-2": "small", "ITEM-3": "large"}
