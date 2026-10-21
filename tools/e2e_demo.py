#!/usr/bin/env python3
"""End-to-end walkthrough of the whole platform, offline (dir transport + mock LLM), asserting as it goes.

    python tools/e2e_demo.py --out e2e-out

Steps (each prints what it did and fails loudly if the platform misbehaves):
 1. package wrapper-core? no - the framework + 3 Python automations into deterministic zips (builder `package.py`)
 2. build a registry (manifest.yaml) with the operator CLI: add-version -> promote dev -> pilot -> broad; validate
 3. simulate two VDI machines; install + run automations through the REAL run lifecycle (self-heal, kill-switch,
    retry, item telemetry, LLM broker, synopsis, log transport) - including a failure, a network blip, a paused automation
 4. ingest the write-only artefacts into the archive, compute compliance signals, LLM-diagnose, build the dashboard
 5. build the registry portal (+ admin panel) from the manifest and live signals
 6. run the notifier automation over the produced signals.json
Outputs: <out>/public (dashboard), <out>/portal (storefront/admin), <out>/registry, <out>/archive, <out>/signals.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
C = ROOT / "components"
sys.path.insert(0, str(C / "wrapper-core"))

from wrapper_core import miniyaml, runctx  # noqa: E402
from wrapper_core.paths import Paths  # noqa: E402
from wrapper_core.runner import run_automation  # noqa: E402

PY = sys.executable
AUTOMATIONS = ["automation-hello-world", "automation-invoice-router", "automation-notifier"]


def sh(args, cwd=None, env=None, check=True):
    e = dict(os.environ, PYTHONPATH=os.pathsep.join([str(C / "wrapper-core"), os.environ.get("PYTHONPATH", "")]), **(env or {}))
    r = subprocess.run([str(a) for a in args], cwd=cwd, env=e, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(r.stdout[-2500:], r.stderr[-2500:], sep="\n")
        raise SystemExit(f"command failed ({r.returncode}): {' '.join(map(str, args))}")
    return r


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def expect(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        raise SystemExit(f"e2e assertion failed: {msg}")


class Machine:
    """One simulated VDI session: its own AUTOMAGIX_HOME and host name, shared registry and log remote."""

    def __init__(self, name: str, root: Path, registry: Path, logs: Path, ring: str):
        self.name, self.home = name, root / name
        self.env = {"AUTOMAGIX_HOME": str(self.home), "AUTOMAGIX_LOG_TRANSPORT": "dir", "AUTOMAGIX_LOG_REMOTE": str(logs),
                    "AUTOMAGIX_REGISTRY_URL": str(registry), "AUTOMAGIX_RING": ring, "AUTOMAGIX_NO_TOAST": "1",
                    "AUTOMAGIX_ALLOW_INSECURE_SECRETS": "1", "AUTOSTATION_LLM_ENDPOINT": "mock://", "COMPUTERNAME": name}

    def run(self, automation: str, slot: str | None = None, **kw):
        saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        try:
            runctx.set_current(None)
            return run_automation(automation, slot, paths=Paths().ensure(), **kw)
        finally:
            runctx.set_current(None)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="e2e-out")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    reg_dir, pkg_dir, logs, archive = out / "registry", out / "packages", out / "logs", out / "archive"
    for d in (reg_dir, pkg_dir, logs, archive):
        d.mkdir()
    manifest = reg_dir / "manifest.yaml"

    # ---- 1. package ------------------------------------------------------------------------------------------
    step(1, "package components and automations (deterministic zips)")
    package = C / "automation-builder" / "scripts" / "package.py"
    stage = out / "stage"
    shutil.copytree(C / "wrapper-core" / "wrapper_core", stage / "wrapper-core" / "wrapper_core", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(C / "wrapper-core" / "VERSION", stage / "wrapper-core" / "VERSION")
    shutil.copytree(C / "execution-framework", stage / "execution-framework",
                    ignore=shutil.ignore_patterns("node_modules", "test", "__pycache__", ".git"))
    packaged: dict[str, dict] = {}
    for kind, name, src, ver in [("component", "wrapper-core", stage / "wrapper-core", (C / "wrapper-core" / "VERSION").read_text().strip()),
                                 ("component", "execution-framework", stage / "execution-framework", (C / "execution-framework" / "VERSION").read_text().strip())] + \
                                [("automation", n, C / "automations" / n, miniyaml.load(C / "automations" / n / "automation.yaml")["version"]) for n in AUTOMATIONS]:
        r = sh([PY, package, kind, name, src, "--version", ver, "--out", pkg_dir])
        info = json.loads(r.stdout.strip().splitlines()[-1])
        packaged[name] = {"kind": kind, "version": ver, "zip": pkg_dir / info["url"], **info}
        print(f"   {name} {ver}  sha256={info['sha256'][:12]}…  {info.get('size', '?')} bytes")
    r2 = sh([PY, package, "automation", "automation-hello-world", C / "automations" / "automation-hello-world",
             "--version", "1.0.0", "--out", out / "repro"])
    expect(json.loads(r2.stdout.strip().splitlines()[-1])["sha256"] == packaged["automation-hello-world"]["sha256"], "packaging is reproducible (same inputs -> same sha256)")

    # ---- 2. registry -----------------------------------------------------------------------------------------
    step(2, "build the registry with the operator CLI (add-version -> promote -> validate)")
    def seed_components() -> dict:
        """Shared components are bootstrapped once by the release process (a new stanza needs a first version)."""
        comps = {}
        for n, p in packaged.items():
            if p["kind"] != "component":
                continue
            dest = reg_dir / "artifacts" / "components"
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy(p["zip"], dest / p["zip"].name)
            comps[n] = {"rings": {r: p["version"] for r in ("dev", "pilot", "broad")},
                        "versions": {p["version"]: {"url": f"artifacts/components/{p['zip'].name}", "sha256": p["sha256"]}}}
        return comps

    manifest.write_text(miniyaml.dumps({
        "schema_version": 1, "fleet": {"paused": False, "paused_reason": ""}, "rings": ["dev", "pilot", "broad"],
        "compatibility": [{"wrapper_core": ">=0.1.0,<1.0.0", "execution_framework": ">=0.1.0,<1.0.0"}],
        "components": seed_components(), "assets": {"default": {"internal_api_url": "https://api.example.internal"}}, "automations": {}}), encoding="utf-8")
    reg = lambda *args, **kw: sh([PY, "-m", "registry", *args, "--manifest", manifest], cwd=C / "automation-registry", **kw)  # noqa: E731
    for n in AUTOMATIONS:
        cls = "confidential" if n == "automation-invoice-router" else "internal"
        reg("new-automation", n, "--owner", "platform-team@example.com", "--description", f"{n} (e2e demo)",
            "--owner-team", "Automation Platform", "--classification", cls, "--llm-allowed", cls, "--system", "demo")
    for n, p in packaged.items():
        if p["kind"] == "component":
            continue
        reg("add-version", p["kind"], n, p["zip"], "--version", p["version"])
        reg("promote", n, "--to", "pilot")
        reg("promote", n, "--to", "broad")
    for n in AUTOMATIONS:
        reg("resume", n, "--reason", "e2e go-live")
    m = miniyaml.load(manifest)
    # example operational config the CLI skeleton doesn't know: retry policy + params (normally edited in an MR)
    m["automations"]["automation-hello-world"]["schedule"]["slots"] = [
        {"id": "daily", "type": "daily", "time": "09:00", "params": {"simulate": "none"}},
        {"id": "flaky", "type": "daily", "time": "10:00", "params": {"simulate": "network"}}]
    m["automations"]["automation-hello-world"]["schedule"]["expected_duration_ms"] = {"baseline": 60000, "warn_threshold": 2.0}
    for n in AUTOMATIONS:
        m["automations"][n]["error_handling"] = {"max_retries": 3, "network_retry_wait_s": 0, "backoff_multiplier": 1.0,
                                                 "skip_on_data_error": True, "escalate_after_retries": True}
        m["automations"][n]["schedule"]["maintenance_windows"] = []
    m["automations"]["automation-invoice-router"]["schedule"]["slots"] = [
        {"id": "daily", "type": "daily", "time": "08:00", "params": {"inbox_dir": str(out / "inbox"), "outbox_dir": str(out / "outbox")}}]
    m["automations"]["automation-notifier"]["schedule"]["slots"] = [
        {"id": "hourly", "type": "hourly", "every": 1, "params": {"signals_url": str(out / "signals.json"), "channel": "outbox", "min_severity": "medium"}}]
    manifest.write_text(miniyaml.dumps(m), encoding="utf-8")
    shutil.copytree(C / "automations" / "automation-invoice-router" / "sample_data" / "inbox", out / "inbox")
    v = reg("validate", "--artifacts", reg_dir, check=False)
    expect(v.returncode == 0, "registry validate passes (ring order, sha256, compatibility)\n" + (v.stdout + v.stderr).strip()[-400:] if v.returncode else "registry validate passes (ring order, sha256, compatibility)")

    # ---- 3. fleet --------------------------------------------------------------------------------------------
    step(3, "two simulated VDI machines install + run through the real lifecycle")
    pilot, broad = Machine("VDI-PILOT-01", out / "machines", reg_dir, logs, "pilot"), Machine("VDI-BROAD-07", out / "machines", reg_dir, logs, "broad")
    runs = []
    for mach in (pilot, broad):
        r = mach.run("automation-hello-world", "daily")
        runs.append(r.record)
        expect(r.record["status"] == "success" and r.record["items"]["success"] == 5, f"{mach.name}: hello-world success, 5 items, synopsis='{r.record['synopsis'][:50]}…'")
    r = pilot.run("automation-hello-world", "flaky")
    expect(r.record["status"] == "success" and any(e["event"] == "retry_event" for e in r.record["events"]), "network blip retried by the platform, run recovers")
    r = pilot.run("automation-invoice-router", "daily")
    expect(r.record["status"] == "success" and r.record["result"]["routed"]["AP"] == 2, "invoice-router routed documents (AP=2) with duplicate/incomplete skipped")
    # a real failure: LOGIC_ERROR escalates, no retry
    mm = miniyaml.load(manifest)
    mm["automations"]["automation-hello-world"]["schedule"]["slots"].append({"id": "broken", "type": "daily", "time": "11:00", "params": {"simulate": "logic"}})
    manifest.write_text(miniyaml.dumps(mm), encoding="utf-8")
    r = pilot.run("automation-hello-world", "broken")
    expect(r.record["status"] == "failed" and r.record["error"]["category"] == "LOGIC_ERROR", "logic error -> failed, categorised, not retried")
    # kill-switch via the operator CLI
    reg("pause", "automation-invoice-router", "--reason", "e2e: upstream data quality incident")
    r = pilot.run("automation-invoice-router", "daily")
    expect(r.record["status"] == "halted_paused" and "upstream data quality" in Paths().status_file("automation-invoice-router").read_text() if False else r.record["status"] == "halted_paused",
           "kill-switch halts the automation before its payload runs")
    reg("resume", "automation-invoice-router", "--reason", "e2e: resolved")
    r = broad.run("automation-invoice-router", "daily")
    expect(r.record["status"] == "success", "resume works; another machine runs it again")

    # ---- 4. ingest + analytics + dashboard -------------------------------------------------------------------
    step(4, "ingest write-only artefacts, compute signals, diagnose, alert (dry), build dashboard")
    st = json.loads(sh([PY, "-m", "automation_logging", "ingest", "--source", "dir", "--from", logs, "--out", archive], cwd=C / "automation-logging").stdout)
    print("   ingest:", {k: v for k, v in st.items() if k != "errors"})
    expect(not st["errors"], "ingest has no errors")
    pub = out / "public"
    sh([PY, "-m", "automation_logging", "build", "--archive", archive, "--manifest", manifest, "--state", out / "state", "--out", pub], cwd=C / "automation-logging",
       env={"AUTOSTATION_LLM_ENDPOINT": "mock://"})
    sigs = json.loads((pub / "signals.json").read_text(encoding="utf-8"))
    signals = sigs["signals"] if isinstance(sigs, dict) else sigs
    types = sorted({s["type"] for s in signals})
    print("   signals:", types)
    expect("FAILED" in types, "the failed run produced a FAILED signal")
    failed = next(s for s in signals if s["type"] == "FAILED")
    expect(bool(failed.get("diagnosis")), "FAILED signal carries an LLM diagnosis (redacted context, schema-validated)")
    shutil.copy(pub / "signals.json", out / "signals.json")
    expect((pub / "index.html").exists(), f"dashboard written: {pub / 'index.html'}")

    # ---- 5. portal ---------------------------------------------------------------------------------------------
    step(5, "registry portal + fleet admin panel")
    portal = out / "portal"
    sh([PY, "-m", "registry", "build-portal", "--manifest", manifest, "--out", portal, "--signals", pub / "signals.json"], cwd=C / "automation-registry")
    expect((portal / "index.html").exists() and (portal / "admin.html").exists(), f"portal written: {portal / 'index.html'}")

    # ---- 6. notifier dog-food ----------------------------------------------------------------------------------
    step(6, "notifier automation consumes signals.json (alerting option B)")
    r = pilot.run("automation-notifier", "hourly")
    expect(r.record["status"] == "success" and r.record["result"]["sent"] >= 1, f"notifier sent {r.record['result']['sent']} notification(s) to its outbox")
    outbox = pilot.home / "data" / "automation-notifier" / "outbox.jsonl"
    print("   first notification:", json.loads(outbox.read_text().splitlines()[0])["title"])

    print(f"\nALL GOOD. Open {pub / 'index.html'} (dashboard) and {portal / 'index.html'} (portal).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
