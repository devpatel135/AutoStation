"""Deterministic routing rules (the LLM never decides where money goes) and anomaly flagging."""
from __future__ import annotations

import statistics

import wrapper_core as wrapper

AP_REVIEW_THRESHOLD = 10_000.0


def route(fields: dict) -> str:
    """Return the destination queue or raise DataError so the item is skipped and flagged."""
    t = fields["doc_type"]
    if t == "tax_form":
        return "TAX"
    if t == "other":
        return "MANUAL"
    if not fields.get("vendor") or not fields.get("invoice_no") or fields.get("amount") is None:
        missing = [k for k in ("vendor", "invoice_no", "amount") if not fields.get(k) and fields.get(k) != 0]
        raise wrapper.DataError("missing required field: " + ", ".join(missing))
    return "AP_REVIEW" if fields["amount"] >= AP_REVIEW_THRESHOLD else "AP"


def deterministic_outliers(amounts: dict[str, float]) -> list[str]:
    if len(amounts) < 3:
        return []
    vals = list(amounts.values())
    mean, sd = statistics.mean(vals), statistics.pstdev(vals)
    return [k for k, v in amounts.items() if sd and (v - mean) / sd > 1.5]


def flag_anomalies(amounts: dict[str, float]) -> dict:
    """Pattern 2 (last-mile intelligence): LLM summary + outliers over *redacted* data, deterministic fallback."""
    det = deterministic_outliers(amounts)
    table = "\n".join(f"{k}: {v:.2f}" for k, v in amounts.items())
    out = wrapper.llm_call("Summarise these invoice amounts in one sentence and list ids that look unusually large.",
                           context=table, schema={"summary": "string", "outliers": ["string"]},
                           caller="automation-invoice-router/anomalies", template="invoice-anomalies@1")
    if out:
        # only accept outlier ids that actually exist; union with the deterministic check
        known = [o for o in out["outliers"] if o in amounts]
        return {"summary": out["summary"], "outliers": sorted(set(known) | set(det)), "source": "llm"}
    return {"summary": f"{len(amounts)} invoice(s) routed; {len(det)} unusually large.", "outliers": det, "source": "deterministic"}
