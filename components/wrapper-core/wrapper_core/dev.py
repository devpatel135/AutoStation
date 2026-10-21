"""Developer harness CLI: ``python -m wrapper_core dev ./my-automation --param date_range=yesterday``.

Runs the payload through the real lifecycle offline (see ``wrapper_core.testing``) and prints the run log.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path


def run_dev(payload_dir: str, params: dict[str, str], keep: bool = False) -> int:
    src = Path(payload_dir).resolve()
    if not (src / "automation.yaml").exists():
        print(f"error: {src} has no automation.yaml")
        return 2
    from .testing import run_payload
    res = run_payload(src, params, keep=keep)
    rec = res.record
    print(json.dumps({k: v for k, v in rec.items() if k != "events"}, indent=2, default=str))
    print(f"\n{len(rec.get('events', []))} event(s). Raw artefacts: {res.logs_dir}")
    if keep:
        print(f"Kept sandbox: {res.root}")
    else:
        shutil.rmtree(res.root, ignore_errors=True)
    return res.exit_code
