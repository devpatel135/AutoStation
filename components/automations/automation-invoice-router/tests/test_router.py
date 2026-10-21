from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent.parent / "wrapper-core"))
sys.path.insert(0, str(ROOT))

from wrapper_core import mockllm  # noqa: E402
from wrapper_core.testing import run_payload  # noqa: E402
from router.extract import regex_extract  # noqa: E402

FAST = {"max_retries": 2, "network_retry_wait_s": 0, "backoff_multiplier": 1.0, "skip_on_data_error": True}


@pytest.fixture
def dirs(tmp_path):
    inbox = tmp_path / "inbox"
    shutil.copytree(ROOT / "sample_data" / "inbox", inbox)
    return {"inbox_dir": str(inbox), "outbox_dir": str(tmp_path / "outbox")}, tmp_path


def run(dirs, **kw):
    params, _ = dirs
    return run_payload(ROOT, params, error_handling=FAST, entry_fields={"data_classification": "confidential", "llm_allowed_classification": "confidential"}, **kw)


def queues(outbox: Path) -> dict:
    return {q.name: sorted(f.name for f in q.iterdir()) for q in outbox.iterdir()} if outbox.exists() else {}


def test_regex_fallback_extracts_fields():
    f = regex_extract((ROOT / "sample_data/inbox/inv-1002.txt").read_text())
    assert f == {"doc_type": "invoice", "vendor": "Contoso Logistics", "invoice_no": "CT-77", "amount": 18400.5, "due_date": "2026-11-15"}


def test_routes_everything_with_llm_disabled(dirs):
    r = run(dirs, llm=None)
    assert r.record["status"] == "success"
    out = queues(Path(dirs[0]["outbox_dir"]))
    assert out == {"AP": ["inv-1001.txt", "inv-1005.txt"], "AP_REVIEW": ["inv-1002.txt"], "MANUAL": ["memo.txt"], "TAX": ["w9-acme.txt"]}
    assert r.record["items"] == {"total": 7, "success": 5, "skipped": 2, "failed": 0}
    reasons = {e["data"]["item_id"]: e["data"]["reason"] for e in r.events("item_processed") if e["data"]["outcome"] == "skipped"}
    assert reasons["inv-1003-duplicate.txt"] == "duplicate" and reasons["inv-1004-incomplete.txt"].startswith("missing required field")
    # skipped documents stay in the inbox for a human
    assert sorted(p.name for p in Path(dirs[0]["inbox_dir"]).iterdir()) == ["inv-1003-duplicate.txt", "inv-1004-incomplete.txt"]
    assert r.record["result"]["routed"] == {"AP": 2, "AP_REVIEW": 1, "MANUAL": 1, "TAX": 1}


def test_llm_extraction_is_used_but_ungrounded_fields_are_rejected(dirs):
    def fake(caller, instr, data, schema):
        if caller != "automation-invoice-router/extract":
            return None
        if "NW-1001" in data:  # hallucinated vendor not present in the document
            return {"doc_type": "invoice", "vendor": "Totally Made Up Corp", "invoice_no": "NW-1001", "amount": 1250.0, "due_date": None}
        return {"doc_type": "invoice" if "INVOICE" in data else ("tax_form" if "W-9" in data else "other"), "vendor": None, "invoice_no": None, "amount": None, "due_date": None}

    mockllm.register(fake)
    r = run(dirs)
    ung = r.events("llm_ungrounded_fields")
    assert ung and ung[0]["data"]["fields"] == ["vendor"]
    assert r.events("llm_decision")                       # audit trail for each extraction
    routed = {e["data"]["item_id"]: e["data"] for e in r.events("routed")}
    assert routed["inv-1001.txt"]["vendor"] == "Northwind Supplies Ltd"   # the real vendor, not the hallucination
    assert routed["inv-1001.txt"]["source"] == "llm"


def test_money_never_routed_on_llm_say_so(dirs):
    mockllm.register(lambda c, i, d, s: {"doc_type": "invoice", "vendor": None, "invoice_no": None, "amount": 99999.0, "due_date": None}
                     if c == "automation-invoice-router/extract" and "CT-77" in d else None)
    r = run(dirs)
    routed = {e["data"]["item_id"]: e["data"] for e in r.events("routed")}
    assert routed["inv-1002.txt"]["amount"] == 18400.5   # ungrounded 99999 discarded


def test_sensitive_fields_not_sent_to_llm_and_llm_gated_by_classification(dirs):
    seen = []
    mockllm.register(lambda c, i, d, s: seen.append((c, d)) or None)
    run(dirs)
    anomaly_ctx = [d for c, d in seen if c.endswith("anomalies")]
    assert anomaly_ctx and all("Northwind" not in d for d in anomaly_ctx)      # amounts only, no vendor names
    seen.clear()
    params, tmp = dirs
    r = run_payload(ROOT, params, error_handling=FAST, entry_fields={"data_classification": "restricted", "llm_allowed_classification": "internal"})
    assert not [c for c, _ in seen]                       # gate: restricted data never reaches the LLM
    assert r.record["status"] == "success" and r.events("routed")


def test_dry_run_moves_nothing(dirs):
    r = run(dirs, llm=None, dry_run=True)
    assert r.record["status"] == "success"
    assert not queues(Path(dirs[0]["outbox_dir"]))
    assert len(list(Path(dirs[0]["inbox_dir"]).iterdir())) == 7


def test_second_run_is_idempotent(dirs):
    run(dirs, llm=None)
    r2 = run_payload(ROOT, dirs[0], error_handling=FAST, llm=None, tmp_root=dirs[1] / "sandbox")  # fresh home: no processed.json
    assert r2.record["status"] == "success"
