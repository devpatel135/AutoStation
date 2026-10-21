"""automation-hello-world: the platform's test automation.

Exercises every wrapper-core feature on purpose so a ring promotion can be validated end to end:
item telemetry, retry classification, kill-switch/maintenance behaviour (via the registry), LLM seam, results.

Behaviour is chosen with the ``simulate`` schedule parameter (set per slot in the registry manifest):

    none     (default)  process N items successfully
    data                one item has bad data   -> skipped, run continues  => status "success" with a skip
    network             first attempts raise ConnectionError -> retried by the platform, then succeeds
    logic               raises KeyError -> not retried, escalated         => status "failed"
    flaky_llm           asks the LLM to classify items; falls back deterministically if it returns None
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import wrapper_core as wrapper
from wrapper_core import DataError

COUNTER = Path("attempt.counter")  # survives the platform's process-level retries within one run


def _attempt_number() -> int:
    n = int(COUNTER.read_text()) + 1 if COUNTER.exists() else 1
    COUNTER.write_text(str(n))
    return n


def process(item: dict) -> str:
    if item["id"] == "ITEM-3" and wrapper.param("simulate") == "data":
        raise DataError("missing required field: vendor_id")
    time.sleep(0.01)
    return item["id"].lower()


def classify(item: dict) -> str:
    """Pattern 1: LLM at a decision point, with a deterministic fallback (LLM may be disabled/down)."""
    out = wrapper.llm_call(
        "Classify the item as one of: small, large. Return JSON.",
        context=f"item id {item['id']} size {item['size']}",
        schema={"label": "small|large"},
        caller="automation-hello-world/classify",
        template="hello-classify@1",
        decision_log=True,
    )
    return out["label"] if out else ("large" if item["size"] > 5 else "small")


@wrapper.entrypoint
def main() -> None:
    simulate = wrapper.param("simulate", "none")
    count = int(wrapper.param("items", "5"))
    wrapper.log_event("start", {"simulate": simulate, "items": count, "dry_run": wrapper.is_dry_run(),
                                "internal_api_url": wrapper.assets().get("internal_api_url")})

    if simulate == "network" and _attempt_number() <= 2:
        raise ConnectionError("simulated VPN drop")
    if simulate == "logic":
        {}["missing"]  # noqa: B018 - deliberate KeyError

    items = [{"id": f"ITEM-{i}", "size": i * 2} for i in range(1, count + 1)]
    labels = {}
    for item in items:
        wrapper.run_item(item["id"], process, item)
        if simulate == "flaky_llm":
            labels[item["id"]] = classify(item)

    wrapper.set_result(processed=len(items), labels=labels or None)
    wrapper.log_event("done", {"count": len(items)})


if __name__ == "__main__":
    raise SystemExit(main())
