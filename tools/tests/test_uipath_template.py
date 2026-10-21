"""The transactional (REFramework-equivalent) template, tested against a FAKE in-memory queue so its exception mapping is
verified independently of the activities library, plus the `--template` option of new_automation.py."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
WRAPPER_CORE = ROOT / "components" / "wrapper-core"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(WRAPPER_CORE))

import new_automation as na  # noqa: E402
import wrapper_core as wrapper  # noqa: E402
from wrapper_core import runctx  # noqa: E402


class NeedsReview(Exception):
    pass


class FakeQueue:
    """Exactly the documented queue API: add/next/succeed/fail_business/fail_system/needs_review/counts."""

    def __init__(self):
        self.items: dict = {}
        self.output: dict = {}

    def add(self, reference, payload=None, priority=2, defer_until=None, deadline=None, allow_duplicate=False) -> bool:
        if reference in self.items and not allow_duplicate:
            return False
        self.items[reference] = SimpleNamespace(reference=reference, payload=payload or {}, retry_no=0, status="new", reason="")
        return True

    def next(self, worker=None):
        for it in self.items.values():
            if it.status == "new":
                it.status = "in_progress"
                return it
        return None

    def succeed(self, item, output=None):
        item.status = "successful"
        self.output[item.reference] = output

    def fail_business(self, item, reason):
        item.status, item.reason = "failed_business", reason

    def fail_system(self, item, reason, max_retries=3):
        item.reason = reason
        if item.retry_no < max_retries:
            item.retry_no += 1
            item.status = "new"
            return "retried"
        item.status = "failed_system"
        return "failed_system"

    def needs_review(self, item, reason):
        item.status, item.reason = "needs_review", reason

    def counts(self):
        out: dict = {}
        for it in self.items.values():
            out[it.status] = out.get(it.status, 0) + 1
        return out


@pytest.fixture
def txn(tmp_path, monkeypatch):
    """Render the template, import its main.py with the fake queue module, return (module, queue)."""
    monkeypatch.setenv("AUTOMAGIX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AUTOMAGIX_ERROR_POLICY", json.dumps({"max_retries": 1, "network_retry_wait_s": 0, "backoff_multiplier": 1.0}))
    for k in ("AUTOSTATION_LLM_ENDPOINT", "AUTOMAGIX_DRY_RUN"):
        monkeypatch.delenv(k, raising=False)
    runctx.set_current(None)
    fq = FakeQueue()
    pkg = types.ModuleType("wrapper_core.activities")
    pkg.__path__ = []  # behave like a package
    qmod = types.ModuleType("wrapper_core.activities.queue")
    qmod.open = lambda name, path=None: fq
    qmod.NeedsReview = NeedsReview
    pkg.queue = qmod
    monkeypatch.setitem(sys.modules, "wrapper_core.activities", pkg)
    monkeypatch.setitem(sys.modules, "wrapper_core.activities.queue", qmod)
    out = tmp_path / "proj"
    na.render_tree(TOOLS / "templates" / "automation-transactional", out,
                   {"name": "automation-txn-test", "description": "t", "owner": "a@b.co", "owner_team": "T", "classification": "internal", "queue_name": "txn-test"})
    spec = importlib.util.spec_from_file_location("txn_main", out / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod, fq
    runctx.set_current(None)


CFG = {"mode": "both", "queue": "q", "max_retries": 2, "max_consecutive_system_exceptions": 3, "max_items": None}


def events(name):
    return [e for e in runctx.current().read_events() if e["event"] == name]


def test_dispatch_then_perform_maps_exceptions_like_the_reframework(txn):
    mod, q = txn
    assert mod.dispatch(q, CFG) == 4
    assert mod.dispatch(q, CFG) == 0                                   # re-running the dispatcher adds nothing
    stats = mod.perform(q, CFG)
    assert stats == {"succeeded": 2, "business_exceptions": 1, "system_exceptions": 0, "needs_review": 1}
    st = {r: i.status for r, i in q.items.items()}
    assert st == {"T-1": "successful", "T-2": "successful", "T-3": "failed_business", "T-4": "needs_review"}
    outcomes = {e["data"]["item_id"]: e["data"]["outcome"] for e in events("item_processed")}
    assert outcomes == {"T-1": "success", "T-2": "success", "T-3": "skipped", "T-4": "skipped"}      # business/review are not failures
    assert q.output["T-1"] == {"approved": True, "amount": 120}


def test_system_exception_is_requeued_until_retries_are_exhausted(txn, monkeypatch):
    mod, q = txn
    calls = []

    def boom(item):
        calls.append(item.retry_no)
        raise RuntimeError("target exploded")                          # UNKNOWN category, no LLM -> escalates -> fail_system

    monkeypatch.setattr(mod, "process_transaction", boom)
    q.add("X-1", {"id": "X-1"})
    stats = mod.perform(q, {**CFG, "max_retries": 2, "max_consecutive_system_exceptions": 99})
    assert calls == [0, 1, 2] and q.items["X-1"].status == "failed_system"
    assert stats["system_exceptions"] == 3 and stats["succeeded"] == 0
    assert all(e["data"]["outcome"] == "failed" for e in events("item_processed"))


def test_logic_errors_are_never_retried(txn, monkeypatch):
    mod, q = txn
    monkeypatch.setattr(mod, "process_transaction", lambda item: {}["missing"])
    q.add("L-1", {})
    mod.perform(q, CFG)
    assert q.items["L-1"].retry_no == 0 and q.items["L-1"].status == "failed_system"


def test_too_many_consecutive_system_exceptions_stop_the_run(txn, monkeypatch):
    mod, q = txn
    monkeypatch.setattr(mod, "process_transaction", lambda item: {}["missing"])
    for i in range(5):
        q.add(f"C-{i}", {})
    with pytest.raises(wrapper.Escalation, match="consecutive system exceptions"):
        mod.perform(q, {**CFG, "max_consecutive_system_exceptions": 3})
    assert sum(1 for i in q.items.values() if i.status == "failed_system") == 3 and sum(1 for i in q.items.values() if i.status == "new") == 2


def test_business_exceptions_reset_the_consecutive_counter(txn, monkeypatch):
    mod, q = txn
    seq = iter(["sys", "sys", "biz", "sys", "sys", "ok"])

    def handler(item):
        what = next(seq)
        if what == "sys":
            raise KeyError("k")
        if what == "biz":
            raise wrapper.DataError("bad data")
        return {"ok": True}

    monkeypatch.setattr(mod, "process_transaction", handler)
    for i in range(6):
        q.add(f"R-{i}", {})
    stats = mod.perform(q, {**CFG, "max_consecutive_system_exceptions": 3})        # would have stopped at 4 in a row without the reset
    assert stats["system_exceptions"] == 4 and stats["business_exceptions"] == 1 and stats["succeeded"] == 1


def test_max_items_and_mode_validation(txn, monkeypatch):
    mod, q = txn
    mod.dispatch(q, CFG)
    assert sum(mod.perform(q, {**CFG, "max_items": 2}).values()) == 2
    monkeypatch.setenv("AUTOMATION_PARAM_MODE", "sideways")
    with pytest.raises(wrapper.LogicError):
        mod.init()
    monkeypatch.setenv("AUTOMATION_PARAM_MODE", "dispatch")
    monkeypatch.setenv("AUTOMATION_PARAM_MAX_RETRIES", "7")
    cfg = mod.init()
    assert cfg["mode"] == "dispatch" and cfg["max_retries"] == 7 and cfg["queue"] == "txn-test"


def test_main_modes_and_dry_run(txn, monkeypatch):
    mod, q = txn
    monkeypatch.setenv("AUTOMAGIX_RUN_DIR", str(Path(runctx.current().run_dir)))
    monkeypatch.setenv("AUTOMATION_PARAM_MODE", "dispatch")
    assert mod.main() == 0
    assert len(q.items) == 4 and all(i.status == "new" for i in q.items.values())
    assert runctx.current().read_result()["dispatched"] == 4
    monkeypatch.setenv("AUTOMATION_PARAM_MODE", "perform")
    assert mod.main() == 0
    assert runctx.current().read_result()["succeeded"] == 2
    fresh = FakeQueue()
    sys.modules["wrapper_core.activities.queue"].open = lambda name, path=None: fresh
    monkeypatch.setenv("AUTOMAGIX_DRY_RUN", "1")
    monkeypatch.setenv("AUTOMATION_PARAM_MODE", "both")
    assert mod.main() == 0
    assert fresh.items == {}                                            # dry run: nothing queued, nothing processed


# --- new_automation.py --template ------------------------------------------------------------------------------------
def run_na(*args, cwd=None):
    return subprocess.run([sys.executable, str(TOOLS / "new_automation.py"), *map(str, args)], capture_output=True, text=True, cwd=cwd)


def test_template_option_generates_both_flavours(tmp_path):
    t = run_na("automation-txn-demo", "--description", "d", "--owner", "a@b.co", "--template", "transactional", "--out", tmp_path / "t", "--no-verify")
    assert t.returncode == 0, t.stdout + t.stderr
    assert "queue.open" in (tmp_path / "t" / "main.py").read_text(encoding="utf-8")
    assert 'QUEUE_NAME = "txn-demo"' in (tmp_path / "t" / "main.py").read_text(encoding="utf-8")
    p = run_na("automation-plain-demo", "--description", "d", "--owner", "a@b.co", "--out", tmp_path / "p", "--no-verify")        # default stays python
    assert p.returncode == 0 and "queue.open" not in (tmp_path / "p" / "main.py").read_text(encoding="utf-8")
    alias = run_na("automation-alias-demo", "--description", "d", "--owner", "a@b.co", "--runtime", "python", "--out", tmp_path / "a", "--no-verify")
    assert alias.returncode == 0


def test_missing_template_is_a_clear_error(tmp_path):
    r = run_na("automation-node-demo", "--description", "d", "--owner", "a@b.co", "--template", "node", "--out", tmp_path / "n", "--no-verify")
    if (TOOLS / "templates" / "automation-node").is_dir() and any((TOOLS / "templates" / "automation-node").rglob("*.tmpl")):
        assert r.returncode == 0
    else:
        assert r.returncode == 2 and "no template" in r.stderr
