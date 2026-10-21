"""Test/dev harness: run a payload folder through the REAL run lifecycle, fully offline.

    from wrapper_core.testing import run_payload
    out = run_payload("path/to/automation", params={"mode": "demo"})
    assert out.record["status"] == "success"

Creates a throwaway AUTOMAGIX_HOME + dir registry + dir log transport + ``mock://`` LLM, copies the payload in,
runs ``run_automation`` (broker, retry, synopsis, logging) and returns the ``RunOutcome`` (``.record`` is the run log,
``.logs_dir`` the raw artefacts). The process environment is restored afterwards.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import miniyaml, runctx
from .paths import Paths
from .runner import RunOutcome, run_automation

_ENV_KEYS = ("AUTOMAGIX_HOME", "AUTOMAGIX_LOG_TRANSPORT", "AUTOMAGIX_LOG_REMOTE", "AUTOMAGIX_REGISTRY_URL", "AUTOMAGIX_RING",
             "AUTOMAGIX_NO_TOAST", "AUTOMAGIX_ALLOW_INSECURE_SECRETS", "AUTOSTATION_LLM_ENDPOINT", "AUTOSTATION_LLM_KEY",
             "AUTOMAGIX_LLM_ALLOWED_CLASSIFICATION", "AUTOMAGIX_DATA_CLASSIFICATION", "AUTOMAGIX_NO_REMEDIATION")


@dataclass
class PayloadRun:
    outcome: RunOutcome
    root: Path

    @property
    def record(self) -> dict:
        return self.outcome.record or {}

    @property
    def exit_code(self) -> int:
        return self.outcome.exit_code

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def home(self) -> Path:
        return self.root / "home"

    def events(self, name: str | None = None) -> list[dict]:
        return [e for e in self.record.get("events", []) if name is None or e["event"] == name]


def run_payload(payload_dir: str | Path, params: dict | None = None, *, env: dict | None = None, tmp_root: Path | None = None,
                llm: str | None = "mock://", ring: str = "dev", error_handling: dict | None = None,
                entry_fields: dict | None = None, slot_type: str = "daily", dry_run: bool = False, keep: bool = False) -> PayloadRun:
    src = Path(payload_dir).resolve()
    desc = miniyaml.load(src / "automation.yaml")
    name = desc["name"]
    root = Path(tmp_root) if tmp_root else Path(tempfile.mkdtemp(prefix="amx-test-"))
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    saved_extra = {k: os.environ.get(k) for k in (env or {})}
    try:
        os.environ.update({
            "AUTOMAGIX_HOME": str(root / "home"), "AUTOMAGIX_LOG_TRANSPORT": "dir", "AUTOMAGIX_LOG_REMOTE": str(root / "logs"),
            "AUTOMAGIX_RING": ring, "AUTOMAGIX_NO_TOAST": "1", "AUTOMAGIX_REGISTRY_URL": str(root / "registry"),
            "AUTOMAGIX_ALLOW_INSECURE_SECRETS": "1",
            "AUTOMAGIX_NO_REMEDIATION": "1"})  # a failing fixture must stay a failing fixture: no auto-fix, no second payload run (opt in via env=)
        if llm:
            os.environ["AUTOSTATION_LLM_ENDPOINT"] = llm
        else:
            os.environ.pop("AUTOSTATION_LLM_ENDPOINT", None)
        os.environ.update(env or {})
        paths = Paths().ensure()
        dest = paths.automation_dir(name)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "tests", ".pytest_cache", "node_modules"))
        (dest / "VERSION").write_text("dev\n", encoding="utf-8")
        slot = {"id": "dev", "type": slot_type, "params": dict(params or {})}
        if slot_type in ("daily", "weekly"):
            slot["time"] = "00:00"
        entry = {"description": "test harness", "owner": "dev@example.com", "status": "active", "rings": {ring: "dev"},
                 "versions": {}, "schedule": {"timezone": "UTC", "slots": [slot]}}
        if error_handling is not None:
            entry["error_handling"] = error_handling
        entry.update(entry_fields or {})
        reg = root / "registry"
        reg.mkdir(parents=True, exist_ok=True)
        (reg / "manifest.yaml").write_text(miniyaml.dumps({"schema_version": 1, "automations": {name: entry}}), encoding="utf-8")
        runctx.set_current(None)
        out = run_automation(name, "dev", paths=paths, dry_run=dry_run)
        return PayloadRun(out, root)
    finally:
        runctx.set_current(None)
        for k, v in {**saved, **saved_extra}.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if not keep and tmp_root is None:
            pass  # caller inspects root; temp dirs are left for the OS to reap (they are small)


# --- fixture-driven tests (``python -m wrapper_core test``) --------------------------------------------------------
MAX_FIXTURE_BYTES = 1_000_000
_ENV_RX = None


def _env_ok(key: str) -> bool:
    import re
    global _ENV_RX
    if _ENV_RX is None:
        _ENV_RX = re.compile(r"^(AMX_|AUTOMATION_|TEST_)[A-Z0-9_]{1,60}$")
    return bool(_ENV_RX.match(key))


def load_fixtures(path: "str | Path") -> list:
    """Read a fixture file (JSON, or the YAML subset) into a list of case dicts. See :func:`run_fixtures` for the schema."""
    import json
    p = Path(path)
    if not p.is_file() or p.stat().st_size > MAX_FIXTURE_BYTES:
        raise ValueError(f"fixtures file missing or larger than {MAX_FIXTURE_BYTES} bytes")
    text = p.read_text(encoding="utf-8")
    data = json.loads(text) if p.suffix.lower() == ".json" else miniyaml.loads(text)
    cases = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(cases, list) or not cases or len(cases) > 200 or not all(isinstance(c, dict) for c in cases):
        raise ValueError("fixtures must be a list of 1-200 case mappings (or {cases: [...]})")
    return cases


def check_expectations(run: "PayloadRun", expect: dict) -> list:
    """Compare a finished run with an ``expect`` block; returns a list of human-readable failures (empty = pass).

    Keys: ``status`` (str or list of str), ``exit_code``, ``result`` (mapping: every key must equal), ``result_keys`` (list),
    ``items`` (mapping of success/skipped/failed/total -> exact count), ``events`` (list of ``{event, min?, max?, data?}`` where ``data`` is a
    subset match), ``error_contains`` (substring of the run error message), ``max_duration_ms``.
    """
    rec, fails = run.record, []
    if "status" in expect:
        want = expect["status"] if isinstance(expect["status"], list) else [expect["status"]]
        if rec.get("status") not in want:
            fails.append(f"status {rec.get('status')!r}, expected {'|'.join(map(str, want))}")
    if "exit_code" in expect and run.exit_code != expect["exit_code"]:
        fails.append(f"exit_code {run.exit_code}, expected {expect['exit_code']}")
    result = rec.get("result") or {}
    for k, v in (expect.get("result") or {}).items():
        if result.get(k) != v:
            fails.append(f"result[{k!r}] is {result.get(k)!r}, expected {v!r}")
    for k in expect.get("result_keys") or []:
        if k not in result:
            fails.append(f"result has no key {k!r}")
    for k, v in (expect.get("items") or {}).items():
        if (rec.get("items") or {}).get(k) != v:
            fails.append(f"items[{k!r}] is {(rec.get('items') or {}).get(k)!r}, expected {v!r}")
    for spec in expect.get("events") or []:
        name = spec.get("event")
        hits = [e for e in rec.get("events", []) if e.get("event") == name
                and all((e.get("data") or {}).get(dk) == dv for dk, dv in (spec.get("data") or {}).items())]
        lo, hi = spec.get("min", 1), spec.get("max")
        if len(hits) < lo or (hi is not None and len(hits) > hi):
            fails.append(f"event {name!r} seen {len(hits)}x, expected min {lo}" + (f" max {hi}" if hi is not None else ""))
    if "error_contains" in expect and expect["error_contains"] not in str((rec.get("error") or {}).get("message", "")):
        fails.append(f"error message does not contain {expect['error_contains']!r}")
    if "max_duration_ms" in expect and (rec.get("duration_ms") or 0) > expect["max_duration_ms"]:
        fails.append(f"duration {rec.get('duration_ms')} ms exceeds {expect['max_duration_ms']} ms")
    return fails


def assert_run(run: "PayloadRun", **expect) -> None:
    """pytest helper: ``assert_run(run_payload(dir), status="success", items={"failed": 0})``."""
    fails = check_expectations(run, expect)
    assert not fails, "; ".join(fails)


def run_fixtures(payload_dir: "str | Path", cases: list) -> list:
    """Run each case in its own sandbox and return ``[{"name", "ok", "failures", "status"}]``.

    Case schema: ``name``, ``params`` (scalar values), ``env`` (only ``AMX_*`` / ``AUTOMATION_*`` / ``TEST_*`` variables), ``dry_run``, ``llm``
    (``mock://`` default; ``null`` for none), ``ring``, ``error_handling`` (manifest block), ``expect`` (see :func:`check_expectations`).
    """
    out = []
    for i, c in enumerate(cases, 1):
        name = str(c.get("name") or f"case-{i}")[:80]
        try:
            params = {str(k): str(v) for k, v in (c.get("params") or {}).items() if not isinstance(v, (dict, list))}
            env = {str(k): str(v) for k, v in (c.get("env") or {}).items()}
            bad = [k for k in env if not _env_ok(k)]
            if bad:
                raise ValueError(f"env variable(s) not allowed in a test case: {', '.join(bad[:3])} (use AMX_*, AUTOMATION_* or TEST_*)")
            llm = c.get("llm", "mock://")
            run = run_payload(payload_dir, params, env=env, llm=llm if llm else None, ring=str(c.get("ring", "dev")),
                              error_handling=c.get("error_handling"), dry_run=bool(c.get("dry_run", False)))
            fails = check_expectations(run, c.get("expect") or {"status": "success"})
            out.append({"name": name, "ok": not fails, "failures": fails, "status": run.record.get("status")})
        except Exception as exc:  # noqa: BLE001 - a broken case is a failed case, not a crashed harness
            out.append({"name": name, "ok": False, "failures": [f"{type(exc).__name__}: {str(exc)[:200]}"], "status": None})
    return out


def main(argv: "list | None" = None) -> int:
    """``python -m wrapper_core test <payload_dir> [--fixtures FILE] [--case NAME] [--json]``  (exit 1 if any case fails)"""
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="wrapper_core test")
    ap.add_argument("payload_dir")
    ap.add_argument("--fixtures", default=None, help="JSON or YAML file of cases (default: one run that must succeed)")
    ap.add_argument("--case", default=None, help="run only the case with this name")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)
    src = Path(a.payload_dir)
    if not (src / "automation.yaml").is_file():
        print(f"error: {src} has no automation.yaml")
        return 2
    try:
        cases = load_fixtures(a.fixtures) if a.fixtures else [{"name": "default", "expect": {"status": "success"}}]
    except (ValueError, miniyaml.YamlError) as exc:
        print(f"error: {exc}")
        return 2
    if a.case:
        cases = [c for c in cases if c.get("name") == a.case]
        if not cases:
            print(f"error: no case named {a.case!r}")
            return 2
    results = run_fixtures(src, cases)
    if a.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['name']}  (status {r['status']})")
            for f in r["failures"]:
                print(f"      - {f}")
        print(f"{sum(1 for r in results if r['ok'])}/{len(results)} case(s) passed")
    return 0 if all(r["ok"] for r in results) else 1
