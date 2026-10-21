"""Document understanding: LLM extraction (Pattern 1) with a deterministic fallback and a grounding check.

The LLM is only trusted for fields it can show are *in the document*: a vendor/invoice number/amount that does not
appear verbatim in the text is discarded (hallucination guard), so a wrong model answer can never route money.
"""
from __future__ import annotations

import re

import wrapper_core as wrapper

SCHEMA = {"doc_type": "invoice|tax_form|other", "vendor": "string?", "invoice_no": "string?", "amount": "number?", "due_date": "string?"}
PROMPT = ("Classify the document as invoice, tax_form or other and extract vendor, invoice_no, amount (number, no "
          "currency symbol) and due_date (YYYY-MM-DD). Use null for anything not present in the document. Return JSON.")

_MONEY = re.compile(r"(?:total due|amount due|total)\s*:?\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)


def regex_extract(text: str) -> dict:
    low = text.lower()
    if re.search(r"form\s+w-?[0-9]|taxpayer identification", low):
        doc_type = "tax_form"
    elif re.search(r"\binvoice\b", low):
        doc_type = "invoice"
    else:
        doc_type = "other"

    def field(label: str):
        m = re.search(rf"^{label}\s*:\s*(.+)$", text, re.I | re.M)
        return m.group(1).strip() if m else None

    money = _MONEY.search(text)
    return {"doc_type": doc_type, "vendor": field("vendor") or field("name"), "invoice_no": field(r"invoice\s*no\.?"),
            "amount": float(money.group(1).replace(",", "")) if money else None, "due_date": field("due date")}


def _grounded(value, text: str) -> bool:
    if value is None:
        return True
    needle = f"{value:,.2f}" if isinstance(value, float) else str(value)
    return needle in text or str(value) in text or (isinstance(value, float) and f"{value:.2f}" in text.replace(",", ""))


def extract(doc_id: str, text: str) -> dict:
    """Return the extracted fields plus ``source`` (llm|regex) - never raises on LLM problems."""
    base = regex_extract(text)
    out = wrapper.llm_call(PROMPT, context=text, schema=SCHEMA, caller="automation-invoice-router/extract",
                           template="invoice-extract@1", classification="confidential", decision_log=True)
    if not out:
        return {**base, "source": "regex"}
    merged = dict(base)
    rejected = []
    for k, v in out.items():
        if k == "doc_type":
            merged[k] = v
        elif v is not None and _grounded(v, text):
            merged[k] = v
        elif v is not None:
            rejected.append(k)
    if rejected:
        wrapper.log_event("llm_ungrounded_fields", {"item_id": doc_id, "fields": rejected})
    return {**merged, "source": "llm"}
