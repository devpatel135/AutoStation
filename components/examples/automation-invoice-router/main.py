"""automation-invoice-router: pulls documents from an inbox, understands them, routes them to AP / AP review / tax / manual.

Headless (no browser): the inbox is a shared path. Deterministic routing, with the LLM used only for document
understanding (Pattern 1) and an end-of-run anomaly summary (Pattern 2), both with deterministic fallbacks.

Parameters (registry schedule slot ``params``): ``inbox_dir``, ``outbox_dir`` (default: under the data dir), ``limit``.
Honours ``--dry-run`` (nothing is moved, no state written).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wrapper_core as wrapper  # noqa: E402

from router.extract import extract  # noqa: E402
from router.routing import flag_anomalies, route  # noqa: E402

NAME = "automation-invoice-router"


def data_dir() -> Path:
    base = Path(os.environ.get("AUTOMAGIX_HOME", Path.home() / ".automagix")) / "data" / NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def load_seen(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def handle(doc: Path, outbox: Path, seen: set[str], amounts: dict[str, float], dry_run: bool) -> str:
    text = doc.read_text(encoding="utf-8", errors="replace")
    fields = extract(doc.name, text)
    queue = route(fields)                                   # DataError -> item skipped by run_item, run continues
    key = f"{fields.get('vendor')}|{fields.get('invoice_no')}".lower() if queue.startswith("AP") else doc.name
    if key in seen:
        raise wrapper.DataError("duplicate")                # run_item logs it as skipped (reason: duplicate)
    wrapper.log_event("routed", {"item_id": doc.name, "queue": queue, "source": fields["source"],
                                 "vendor": fields.get("vendor"), "amount": fields.get("amount")},
                      sensitive_keys=["vendor", "amount"])
    if not dry_run:
        dest = outbox / queue
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(doc), dest / doc.name)
        seen.add(key)
    if fields.get("amount") is not None:
        amounts[doc.name] = float(fields["amount"])
    return queue


@wrapper.entrypoint
def main() -> None:
    dry = wrapper.is_dry_run()
    inbox = Path(wrapper.param("inbox_dir", str(data_dir() / "inbox")))
    outbox = Path(wrapper.param("outbox_dir", str(data_dir() / "outbox")))
    limit = int(wrapper.param("limit", "500"))
    state_file = data_dir() / "processed.json"
    seen = load_seen(state_file)
    docs = sorted(p for p in inbox.glob("*.txt"))[:limit] if inbox.exists() else []
    wrapper.log_event("start", {"inbox": str(inbox), "documents": len(docs), "dry_run": dry})

    amounts: dict[str, float] = {}
    routed: dict[str, int] = {}
    for doc in docs:
        # snapshot of `seen` semantics: duplicates are skipped (not failed) and the run keeps going
        queue = wrapper.run_item(doc.name, handle, doc, outbox, seen, amounts, dry)
        if queue:
            routed[queue] = routed.get(queue, 0) + 1

    anomalies = flag_anomalies(amounts) if amounts else {"summary": "no invoices", "outliers": [], "source": "none"}
    wrapper.log_event("anomalies", anomalies)
    if not dry:
        state_file.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    wrapper.set_result(routed=routed, anomalies=anomalies["outliers"], processed=sum(routed.values()))


if __name__ == "__main__":
    raise SystemExit(main())
