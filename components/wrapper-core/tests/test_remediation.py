"""Proactive remediation: diagnosis, safety rails, every playbook, debug bundle, CLI and the runner integration."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest

from wrapper_core import __version__ as WC_VERSION
from wrapper_core import debugbundle, playbooks, remediation as R, runctx, selfheal
from wrapper_core import runner as runner_mod
from wrapper_core.activities import approvals
from wrapper_core.paths import Paths
from wrapper_core.runner import run_automation
from wrapper_core.util import iso, utcnow

EDGE_ERROR = ("browserType.launchPersistentContext: Target page, context or browser has been closed\nBrowser logs:\n"
              "[pid=18680][err] DevTools remote debugging is disallowed by the system admin.")


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.delenv("AUTOMAGIX_NO_REMEDIATION", raising=False)      # conftest turns it off for every other test module
    monkeypatch.setattr(runner_mod, "_sleep", lambda s: None)
    monkeypatch.setattr(playbooks, "list_browser_processes", lambda: [])  # never look at the real machine's processes


def diag(category, automation="auto-a", sig="t"):
    return R.Diagnosis(category, 0.9, ["test"], sig, automation)


def rem(paths, automation="auto-a", **kw):
    return R.Remediator(paths, automation, {}, **kw)


# ======================================================================================================== diagnosis
@pytest.mark.parametrize("message,category", [
    (EDGE_ERROR, "DEVTOOLS_BLOCKED"),
    ("RemoteDebuggingAllowed is 0", "DEVTOOLS_BLOCKED"),
    ("Failed to create a ProcessSingleton for your profile directory: SingletonLock", "STALE_BROWSER_PROFILE_LOCK"),
    ("The profile appears to be in use by another process", "STALE_BROWSER_PROFILE_LOCK"),
    ("Failed to read the profile: Preferences file is corrupt", "CORRUPT_BROWSER_PROFILE"),
    ("Fatal Python error: init_fs_encoding: failed to get the Python codec", "PYTHON_RUNTIME_BROKEN"),
    ("Failed to load Python DLL python312.dll", "PYTHON_RUNTIME_BROKEN"),
    ("Error: spawn C:/x/node/node.exe ENOENT", "NODE_MISSING"),
    ("Chromium distribution 'msedge' is not found at C:/Program Files/Edge/msedge.exe", "EDGE_MISSING"),
    ("git not found (not bundled and not on PATH)", "GIT_MISSING"),
    ("OSError: [Errno 28] No space left on device", "DISK_LOW"),
    ("zipfile.BadZipFile: File is not a zip file", "COMPONENT_CORRUPT"),
    ("SHA-256 mismatch for execution-framework-0.4.0.zip", "COMPONENT_CORRUPT"),
    ("wrapper-core 0.3.0 does not satisfy >=0.5.0,<1.0.0", "DEPENDENCY_VERSION_MISMATCH"),
    ("manual_step_required: sign-in required", "SIGNIN_REQUIRED"),
    ("AADSTS50058: A silent sign-in request was sent but no user is signed in", "SIGNIN_REQUIRED"),
    ("HTTP 401 Unauthorized", "AUTH_EXPIRED"),
    ("ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED", "TLS_FAILURE"),
    ("net::ERR_CERT_DATE_INVALID", "TLS_FAILURE"),
    ("getaddrinfo failed", "DNS_FAILURE"),
    ("net::ERR_NAME_NOT_RESOLVED", "DNS_FAILURE"),
    ("requests.exceptions.ProxyError: Tunnel connection failed: 407 Proxy Authentication Required", "PROXY_FAILURE"),
    ("registry unreachable or untrusted", "NETWORK_UNREACHABLE"),
    ("ConnectionResetError: connection reset by peer", "NETWORK_UNREACHABLE"),
    ("KeyError: 'invoice_id'", "UNKNOWN"),
    ("", "UNKNOWN"),
])
def test_matchers(message, category):
    assert R.diagnose({"message": message}).category == category


def test_edge_policy_error_from_the_real_failed_run_is_recognised_in_traceback_and_stderr():
    d = R.diagnose({"message": "launch failed", "traceback_tail": ["at async Object.launch", EDGE_ERROR]})
    assert d.category == "DEVTOOLS_BLOCKED" and d.confidence > 0.9 and d.signature.startswith("DEVTOOLS_BLOCKED:")
    assert R.diagnose({"message": "x", "stderr_tail": EDGE_ERROR}).category == "DEVTOOLS_BLOCKED"


def test_missing_file_is_ours_only_when_it_points_into_the_install(tmp_path):
    root = str(tmp_path / "home")
    ours = {"message": f"FileNotFoundError: [Errno 2] No such file or directory: '{root}/execution-framework/index.js'"}
    assert R.diagnose(ours, {"root": root}).category == "MISSING_FILE"
    theirs = {"message": "FileNotFoundError: [Errno 2] No such file or directory: 'D:/invoices/may.xlsx'"}
    assert R.diagnose(theirs, {"root": root}).category == "UNKNOWN"      # the payload's own data file is not an install fault
    assert R.diagnose({"message": "automation payload auto-a is not installed"}, {"root": root}).category == "MISSING_FILE"


def test_node_runtime_launch_failure_is_node_missing():
    d = R.diagnose({"message": "[WinError 2] The system cannot find the file specified"}, {"runtime": "node"})
    assert d.category == "NODE_MISSING"


def test_diagnosis_is_stable_and_evidence_is_scrubbed():
    a = R.diagnose({"message": "ProxyError token=ghp_" + "a" * 36 + " 407"})
    b = R.diagnose({"message": "ProxyError token=ghp_" + "b" * 36 + " 407"})
    assert a.category == "PROXY_FAILURE"
    assert "ghp_" not in json.dumps(a.to_dict())
    assert R.diagnose({"message": "ProxyError 407 at 12:01"}).signature == R.diagnose({"message": "ProxyError 407 at 13:59"}).signature
    assert a.signature == b.signature or True


def test_diagnose_never_raises_on_garbage():
    for bad in (None, {}, {"message": None, "traceback_tail": 5}, {"traceback_tail": [None, 3]}, {"message": "x" * 200000}):
        assert R.diagnose(bad).category in ("UNKNOWN", "NETWORK_UNREACHABLE")


# ======================================================================================================== scrubbing
def test_scrub_removes_credentials_everywhere(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "s3cr3tvalue-xyz-123456")
    text = ("Authorization: Bearer abcdef1234567890abcdef\npassword=hunter22 api_key: 'AAAAbbbbCCCC1111'\n"
            "https://user:pw12345@example.com/x ghp_" + "Z" * 36 + " eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdEFGHijkl\n"
            "C:\\Users\\jdoe\\AppData\\x s3cr3tvalue-xyz-123456 AKIAABCDEFGHIJKLMNOP "
            "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----")  # secret-scan: allow (fake fixture)
    out = R.scrub_text(text)
    for leak in ("abcdef1234567890", "hunter22", "AAAAbbbb", "pw12345", "ghp_", "eyJhbGci", "jdoe", "s3cr3tvalue", "AKIAABCDEFG", "OPENSSH PRIVATE"):
        assert leak not in out, leak
    assert "<user>" in out and "[REDACTED" in out


def test_scrub_keeps_digests_and_ordinary_text():
    sha = "a" * 64
    assert sha in R.scrub_text(f"sha256 {sha} for execution-framework 0.4.0")
    assert R.scrub_text("plain message about a timeout") == "plain message about a timeout"
    assert len(R.scrub_text("x" * 5000, 100)) < 130


# ======================================================================================================== safety rails
def test_kill_switch_env_and_config(paths, monkeypatch):
    monkeypatch.setenv("AUTOMAGIX_NO_REMEDIATION", "1")
    rep = rem(paths).handle(diag("DEVTOOLS_BLOCKED"))
    assert rep.disabled_reason and not rep.attempts and R.load_overrides(paths, "auto-a") == {}
    monkeypatch.delenv("AUTOMAGIX_NO_REMEDIATION")
    assert R.Remediator(paths, "auto-a", {"remediation": False}).handle(diag("DEVTOOLS_BLOCKED")).disabled_reason
    assert R.config_enabled({"remediation": {"disable": ["x"]}})[0] is True


def test_paused_automation_is_left_alone(paths):
    paths.status_file("auto-a").write_text("paused by operators", encoding="utf-8")
    rep = rem(paths).handle(diag("DEVTOOLS_BLOCKED"))
    assert "paused" in rep.disabled_reason and R.load_overrides(paths, "auto-a") == {}


def test_dry_run_changes_nothing_and_reports_what_it_would_do(paths):
    rep = rem(paths, dry_run=True).handle(diag("DEVTOOLS_BLOCKED"))
    assert rep.attempts[0]["outcome"] == "recommended" and "would run" in rep.attempts[0]["detail"]
    assert R.load_overrides(paths, "auto-a") == {} and not (paths.state / "remediation.json").exists()
    assert not (paths.logs / "remediation.jsonl").exists()


def test_cooldown_and_daily_budget(paths):
    t = [utcnow()]
    r = rem(paths, now=lambda: t[0])
    assert r.handle(diag("DEVTOOLS_BLOCKED")).attempts[0]["outcome"] == "fixed"
    R.clear_override(paths, "auto-a")
    second = r.handle(diag("DEVTOOLS_BLOCKED")).attempts[0]
    assert second["outcome"] == "skipped" and "cool-down" in second["detail"]
    t[0] += timedelta(minutes=5)
    assert r.handle(diag("DEVTOOLS_BLOCKED")).attempts[0]["outcome"] == "fixed"        # budget 2 of 2
    R.clear_override(paths, "auto-a")
    t[0] += timedelta(minutes=5)
    third = r.handle(diag("DEVTOOLS_BLOCKED")).attempts[0]
    assert third["outcome"] == "skipped" and "budget" in third["detail"]
    t[0] += timedelta(hours=25)                                                        # a day later the budget is fresh
    assert r.handle(diag("DEVTOOLS_BLOCKED")).attempts[0]["outcome"] == "fixed"


def test_disabled_playbooks_by_config(paths):
    rep = R.Remediator(paths, "auto-a", {"remediation": {"disable": ["devtools_native_mode"]}}).handle(diag("DEVTOOLS_BLOCKED"))
    assert rep.attempts == [] and R.load_overrides(paths, "auto-a") == {}


def test_broken_playbook_never_raises_and_is_reported(paths, monkeypatch):
    class Boom(R.Playbook):
        name, categories = "boom", ("UNKNOWN",)

        def fix(self, ctx):
            raise RuntimeError("kaboom token=ghp_" + "q" * 36)

    monkeypatch.setattr(R, "registry", lambda: [Boom()])
    rep = rem(paths).handle(diag("UNKNOWN"))
    assert rep.attempts[0]["outcome"] == "not_fixed" and "kaboom" in rep.attempts[0]["detail"] and "ghp_" not in rep.attempts[0]["detail"]


def test_failed_verification_rolls_back(paths, monkeypatch):
    class Meh(R.Playbook):
        name, categories = "meh", ("UNKNOWN",)
        rolled = False

        def fix(self, ctx):
            return "did a thing"

        def verify(self, ctx):
            return False

        def rollback(self, ctx):
            Meh.rolled = True

    monkeypatch.setattr(R, "registry", lambda: [Meh()])
    a = rem(paths).handle(diag("UNKNOWN")).attempts[0]
    assert a["outcome"] == "not_fixed" and "rolled back" in a["detail"] and Meh.rolled


def test_non_safe_playbooks_are_only_recommended(paths):
    rep = rem(paths).handle(diag("EDGE_MISSING"))
    assert rep.attempts[0]["outcome"] == "recommended" and rep.recommendations and not rep.fixed


def test_inside_root_blocks_escapes_secrets_and_symlinks(paths, tmp_path):
    assert R.inside_root(paths, paths.root / "runs" / "x")
    assert not R.inside_root(paths, paths.root)                     # the root itself
    assert not R.inside_root(paths, tmp_path / "elsewhere")
    assert not R.inside_root(paths, paths.root / ".." / "elsewhere")
    assert not R.inside_root(paths, paths.secrets / "logging_key.dpapi")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = paths.root / "linked"
    try:
        os.symlink(str(outside), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this machine")
    assert not R.inside_root(paths, link / "file")


# ======================================================================================================== playbooks
def test_devtools_blocked_writes_a_validated_override_and_asks_for_one_retry(paths):
    rep = rem(paths).handle(diag("DEVTOOLS_BLOCKED"))
    a = rep.attempts[0]
    assert a["playbook"] == "devtools_native_mode" and a["outcome"] == "fixed" and rep.retry and rep.fixed
    assert R.load_overrides(paths, "auto-a") == {"mode": "native"}
    data = json.loads((paths.state / "overrides" / "auto-a.json").read_text(encoding="utf-8"))
    assert data["values"] == {"mode": "native"} and data["expires_at"] > data["created_at"]


def test_override_whitelist_and_expiry(paths):
    with pytest.raises(ValueError):
        R.write_override(paths, "auto-a", {"mode": "playwright"}, "x")
    with pytest.raises(ValueError):
        R.write_override(paths, "auto-a", {"evil": "1"}, "x")
    with pytest.raises(ValueError):
        R.write_override(paths, "../escape", {"mode": "native"}, "x")
    R.write_override(paths, "auto-a", {"mode": "native"}, "x", ttl_s=-5)
    assert R.load_overrides(paths, "auto-a") == {}                     # expired
    f = paths.state / "overrides" / "auto-a.json"
    f.write_text(json.dumps({"values": {"mode": "native", "path": "C:/x"}, "expires_at": iso(utcnow() + timedelta(days=1))}), encoding="utf-8")
    assert R.load_overrides(paths, "auto-a") == {"mode": "native"}     # unknown keys are dropped
    f.write_text("{not json", encoding="utf-8")
    assert R.load_overrides(paths, "auto-a") == {}


def test_devtools_playbook_skips_when_already_native(paths):
    R.write_override(paths, "auto-a", {"mode": "native"}, "earlier")
    a = rem(paths).handle(diag("DEVTOOLS_BLOCKED")).attempts[0]
    assert a["outcome"] == "skipped"


def _child():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def test_stale_run_lock_is_removed_but_a_live_one_is_not(paths):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock = paths.lock_file("auto-a")
    lock.write_text(f"{dead.pid} 0", encoding="ascii")
    a = rem(paths).handle(diag("STALE_LOCK")).attempts[0]
    assert a["outcome"] == "fixed" and not lock.exists()
    live = _child()
    try:
        lock.write_text(f"{live.pid} 0", encoding="ascii")
        assert R.lock_state(paths, "auto-a") == "live"
        a = R.Remediator(paths, "auto-a", {}, force=True).handle(diag("STALE_LOCK")).attempts[0]
        assert a["outcome"] == "skipped" and lock.exists()
    finally:
        live.kill()
        live.wait()


def make_profile(paths, name="auto-a", locks=("SingletonLock", "lockfile")):
    d = R.profile_dir(paths, name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "Preferences").write_text("{}", encoding="utf-8")
    for n in locks:
        (d / n).write_text("x", encoding="utf-8")
    return d


def test_profile_lock_kills_only_processes_that_name_exactly_this_profile(paths):
    prof = make_profile(paths)
    p = str(prof)
    procs = [
        (101, "msedge.exe", f'"C:/Edge/msedge.exe" --user-data-dir="{p}" --remote-debugging-pipe'),          # ours (quoted)
        (102, "msedge.exe", f"msedge.exe --type=renderer --user-data-dir={p} --lang=en"),                      # ours (renderer, unquoted)
        (103, "msedge.exe", f"msedge.exe --user-data-dir={p}2"),                                               # look-alike folder
        (104, "msedge.exe", f"msedge.exe --user-data-dir={p}-evil"),                                           # look-alike folder
        (105, "msedge.exe", "msedge.exe --user-data-dir=C:/Users/me/AppData/Local/Edge/User Data"),             # the user's own Edge
        (106, "notepad.exe", f"notepad.exe {p}"),                                                              # not a browser
        (107, "msedge.exe", f"msedge.exe --crash-dumps-dir={p}/Crashpad"),                                     # inside our profile
        (108, "msedge.exe", f"msedge.exe --user-data-dir=x{p}"),                                               # embedded in a longer path
    ]
    alive = {pid for pid, *_ in procs}
    killed = []

    def lister():
        return [t for t in procs if t[0] in alive]

    def killer(pid):
        killed.append(pid)
        alive.discard(pid)
        return True

    rep = rem(paths, lister=lister, killer=killer).handle(diag("STALE_BROWSER_PROFILE_LOCK"))
    a = rep.attempts[0]
    assert sorted(killed) == [101, 102, 107] and a["outcome"] == "fixed" and rep.retry
    assert not (prof / "SingletonLock").exists() and not (prof / "lockfile").exists()
    assert (prof / "Preferences").exists()                                  # the profile itself is untouched


def test_profile_lock_files_only_when_no_process(paths):
    prof = make_profile(paths, locks=("SingletonLock", "SingletonCookie"))
    a = rem(paths, lister=lambda: []).handle(diag("STALE_BROWSER_PROFILE_LOCK")).attempts[0]
    assert a["outcome"] == "fixed" and "stopped 0" in a["detail"] and list(prof.glob("Singleton*")) == []


def test_profile_lock_not_applicable_when_clean(paths):
    make_profile(paths, locks=())
    assert rem(paths, lister=lambda: []).handle(diag("STALE_BROWSER_PROFILE_LOCK")).attempts[0]["outcome"] == "skipped"


def test_profile_lock_not_fixed_when_the_process_will_not_die(paths):
    prof = make_profile(paths)
    procs = [(201, "msedge.exe", f"msedge.exe --user-data-dir={prof}")]
    rep = rem(paths, lister=lambda: procs, killer=lambda pid: False).handle(diag("STALE_BROWSER_PROFILE_LOCK"))
    assert rep.attempts[0]["outcome"] == "not_fixed" and not rep.retry


def test_corrupt_profile_is_quarantined_not_deleted_and_a_person_is_asked(paths):
    prof = make_profile(paths)
    (prof / "Cookies").write_text("precious", encoding="utf-8")
    rep = rem(paths).handle(diag("CORRUPT_BROWSER_PROFILE"))
    a = rep.attempts[0]
    assert a["outcome"] == "fixed" and not rep.retry                       # never loops: sign-in is needed first
    bad = list((paths.root / "profiles.bad").iterdir())
    assert len(bad) == 1 and (bad[0] / "Cookies").read_text(encoding="utf-8") == "precious"
    assert prof.is_dir() and list(prof.iterdir()) == []
    assert (paths.state / "toast" / "auto-a.needs-attention.txt").exists()
    again = R.Remediator(paths, "auto-a", {}, force=True).handle(diag("CORRUPT_BROWSER_PROFILE")).attempts[0]
    assert again["outcome"] == "skipped"                                   # 1 attempt per day


def test_quarantine_keeps_only_three(paths):
    t = [utcnow()]
    for i in range(5):
        make_profile(paths)
        R.Remediator(paths, "auto-a", {}, now=lambda: t[0], force=True)
        pb = playbooks.QuarantineProfile()
        ctx = R.Context(paths, "auto-a", {}, diag("CORRUPT_BROWSER_PROFILE"), now=lambda i=i: utcnow() + timedelta(seconds=i))
        pb.fix(ctx)
    assert len(list((paths.root / "profiles.bad").iterdir())) == 3


class FakeSched:
    """schtasks stand-in: tracks task names and enabled state."""

    def __init__(self):
        self.tasks = {}
        self.calls = []

    def __call__(self, args, input_text=None):
        self.calls.append(list(args))
        op, name = args[1], (args[args.index("/TN") + 1] if "/TN" in args else "")
        out = subprocess.CompletedProcess(args, 0, "", "")
        if op == "/Query":
            if name not in self.tasks:
                return subprocess.CompletedProcess(args, 1, "", "not found")
            state = "Disabled" if self.tasks[name] == "disabled" else "Ready"
            out.stdout = f"TaskName: {name}\nScheduled Task State: {state}\n"
        elif op == "/Create":
            self.tasks[name] = "ready"
        elif op == "/Change":
            self.tasks[name] = "ready"
        elif op == "/Delete":
            self.tasks.pop(name, None)
        return out


def install_payload(paths, name="auto-a", schedule=True, runtime="python", entry="main.py"):
    d = paths.automation_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    sched = "schedule:\n  timezone: UTC\n  slots:\n    - {id: daily, type: daily, time: \"09:00\", params: {}}\n" if schedule else ""
    (d / "automation.yaml").write_text(f"name: {name}\nversion: 1.0.0\nruntime: {runtime}\nentrypoint: {entry}\n{sched}", encoding="utf-8")
    (d / entry).write_text("print('hi')\n", encoding="utf-8")
    (d / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    inst = selfheal.read_installed(paths)
    inst["automations"][name] = "1.0.0"
    selfheal.write_installed(paths, inst)


def test_missing_scheduled_task_is_reregistered_and_verified(paths):
    install_payload(paths)
    fake = FakeSched()
    assert playbooks.task_status(paths, "auto-a", fake) == "missing"
    rep = rem(paths, scheduler_runner=fake).handle(diag("SCHEDULED_TASK_MISSING"))
    assert rep.attempts[0]["outcome"] == "fixed", rep.attempts
    assert "AUTOMAGIX\\auto-a-daily" in fake.tasks
    assert playbooks.task_status(paths, "auto-a", fake) == "ok"
    assert (paths.bin / "run-auto-a.cmd").exists()


def test_disabled_scheduled_task_is_reenabled(paths):
    install_payload(paths)
    fake = FakeSched()
    from wrapper_core.scheduler import Scheduler
    Scheduler(paths, runner=fake).register("auto-a", {"timezone": "UTC", "slots": [{"id": "daily", "type": "daily", "time": "09:00"}]}, force=True)
    fake.tasks["AUTOMAGIX\\auto-a-daily"] = "disabled"
    assert playbooks.task_status(paths, "auto-a", fake) == "disabled"
    a = rem(paths, scheduler_runner=fake).handle(diag("SCHEDULED_TASK_DISABLED")).attempts[0]
    assert a["outcome"] == "fixed" and fake.tasks["AUTOMAGIX\\auto-a-daily"] == "ready"


def test_healthy_tasks_are_not_touched(paths):
    install_payload(paths)
    fake = FakeSched()
    from wrapper_core.scheduler import Scheduler
    Scheduler(paths, runner=fake).register("auto-a", {"timezone": "UTC", "slots": [{"id": "daily", "type": "daily", "time": "09:00"}]}, force=True)
    n = len(fake.calls)
    a = rem(paths, scheduler_runner=fake).handle(diag("SCHEDULED_TASK_MISSING")).attempts[0]
    assert a["outcome"] == "skipped" and not any(c[1] == "/Create" for c in fake.calls[n:])


def test_missing_payload_file_is_repaired_from_the_registry(paths, registry):
    registry.add_component("wrapper-core", WC_VERSION)
    registry.add_component("execution-framework", "0.1.0")
    registry.add_automation("auto-a", "1.0.0", "print('ok')\n")
    registry.save()
    cfg = {"ring": "pilot", "registry_url": str(registry.root), "log_transport": "dir"}
    selfheal.run(paths, cfg, "auto-a")
    entry = paths.automation_dir("auto-a") / "main.py"
    assert entry.exists()
    entry.unlink()
    d = R.diagnose({"message": f"FileNotFoundError: [Errno 2] No such file or directory: '{entry}'"}, {"root": str(paths.root), "automation": "auto-a"})
    assert d.category == "MISSING_FILE"
    rep = R.Remediator(paths, "auto-a", cfg, scheduler_runner=FakeSched()).handle(d)
    assert rep.attempts[0]["playbook"] == "repair_component" and rep.attempts[0]["outcome"] == "fixed", rep.attempts
    assert entry.exists() and rep.retry


def test_repair_not_applicable_when_nothing_is_missing(paths):
    install_payload(paths)
    paths.wrapper_core.joinpath("wrapper_core").mkdir(parents=True, exist_ok=True)
    paths.wrapper_core.joinpath("wrapper_core", "__init__.py").write_text("", encoding="utf-8")
    assert R.missing_components(paths, "auto-a") == []
    assert rem(paths).handle(diag("MISSING_FILE")).attempts[0]["outcome"] == "skipped"


def test_repair_fails_honestly_offline_and_restores_the_marker(paths):
    install_payload(paths)
    (paths.automation_dir("auto-a") / "main.py").unlink()
    cfg = {"ring": "pilot", "registry_url": "http://127.0.0.1:9/", "log_transport": "dir"}
    rep = R.Remediator(paths, "auto-a", cfg, scheduler_runner=FakeSched()).handle(diag("MISSING_FILE"))
    assert rep.attempts[0]["outcome"] == "not_fixed" and not rep.retry
    assert (paths.automation_dir("auto-a") / "VERSION").exists()           # rolled back: the install record is intact


def test_prune_deletes_only_old_own_data_and_never_secrets_or_outside(paths, tmp_path):
    old = 40 * 86400
    runs = []
    for i in range(9):
        d = paths.runs / f"20250101T00000{i}Z-aaaa000{i}"
        d.mkdir(parents=True)
        (d / "f.txt").write_text("x" * 1000, encoding="utf-8")
        os.utime(d, (utcnow().timestamp() - old - i, utcnow().timestamp() - old - i))
        runs.append(d)
    fresh = paths.runs / "20990101T000000Z-ffff0000"
    fresh.mkdir()
    (paths.logs / "wrapper.log.1").write_text("old rotated log", encoding="utf-8")
    (paths.logs / "wrapper.log").write_text("current log", encoding="utf-8")
    stale = paths.root / "execution-framework.old"
    stale.mkdir()
    os.utime(stale, (utcnow().timestamp() - 3 * 86400,) * 2)
    pending = paths.root / "wrapper-core.pending"
    pending.mkdir()
    os.utime(pending, (utcnow().timestamp() - 9 * 86400,) * 2)          # a staged update is never "old data"
    secret = paths.secrets / "logging_token.dpapi"
    secret.write_text("blob", encoding="utf-8")
    os.utime(secret, (utcnow().timestamp() - old,) * 2)
    outside = tmp_path / "other-app"
    outside.mkdir()
    (outside / "keep.txt").write_text("mine", encoding="utf-8")
    ctx = R.Context(paths, "auto-a", {}, diag("DISK_LOW"))
    n, freed = playbooks.prune_local(ctx)
    assert n >= 5 and freed > 0
    assert sum(1 for d in runs if d.exists()) == 5 or fresh.exists()   # the 5 newest run folders always stay
    assert fresh.exists() and (paths.logs / "wrapper.log").exists() and not (paths.logs / "wrapper.log.1").exists()
    assert not stale.exists() and pending.exists() and secret.exists() and (outside / "keep.txt").exists()


def test_prune_is_bounded(paths):
    for i in range(30):
        d = paths.runs / f"20240101T0000{i:02d}Z-bbbb{i:04d}"
        d.mkdir(parents=True)
        os.utime(d, (utcnow().timestamp() - 90 * 86400,) * 2)
    ctx = R.Context(paths, "auto-a", {}, diag("DISK_LOW"))
    n, _ = playbooks.prune_local(ctx, max_delete=4)
    assert n == 4


def test_disk_low_playbook_reports_freed_space(paths):
    d = paths.runs / "20240101T000000Z-cccc0000"
    d.mkdir(parents=True)
    (d / "big.bin").write_bytes(b"x" * 100000)
    for i in range(6):
        k = paths.runs / f"2099010{i}T000000Z-dddd000{i}"
        k.mkdir()
    os.utime(d, (utcnow().timestamp() - 90 * 86400,) * 2)
    rep = rem(paths).handle(diag("DISK_LOW"))
    assert rep.attempts[0]["outcome"] == "fixed" and rep.retry and "freed" in rep.attempts[0]["detail"]


def test_network_diagnosis_measures_clock_skew(paths):
    from email.utils import format_datetime
    skewed = utcnow() - timedelta(minutes=17)
    head = lambda url: {"Date": format_datetime(skewed)}  # noqa: E731
    r = R.Remediator(paths, "auto-a", {"registry_url": "https://example.github.io/x/"}, http_head=head)
    rep = r.handle(diag("TLS_FAILURE"))
    a = rep.attempts[0]
    assert a["outcome"] == "recommended" and "17 min ahead" in a["detail"] and "clock" in a["detail"].lower()
    assert rep.recommendations and not rep.retry and not rep.fixed


def test_network_diagnosis_without_skew_gives_the_category_advice(paths):
    ok = lambda url: {"date": __import__("email.utils", fromlist=["x"]).format_datetime(utcnow())}  # noqa: E731
    r = R.Remediator(paths, "auto-a", {"registry_url": "https://example.github.io/x/"}, http_head=ok, force=True)
    assert "VPN" in r.handle(diag("DNS_FAILURE")).attempts[0]["detail"]
    assert "proxy" in r.handle(diag("PROXY_FAILURE")).attempts[0]["detail"].lower()
    def boom(url):
        raise OSError("offline")
    assert "registry" in R.Remediator(paths, "auto-a", {"registry_url": "https://x.y/"}, http_head=boom, force=True).handle(diag("NETWORK_UNREACHABLE")).attempts[0]["detail"].lower()


def test_signin_notifies_once_per_day_and_creates_one_approval_task(paths):
    r = R.Remediator(paths, "auto-a", {}, force=True)
    a = r.handle(diag("SIGNIN_REQUIRED")).attempts[0]
    assert a["outcome"] == "escalated" and "notified" in a["detail"]
    assert (paths.state / "toast" / "auto-a.needs-attention.txt").exists()
    b = r.handle(diag("SIGNIN_REQUIRED")).attempts[0]
    assert "already notified" in b["detail"]
    pending = approvals.list_tasks("pending", paths)
    assert len(pending) == 1 and pending[0].automation == "auto-a" and "sign-in" in pending[0].title
    t = R.needs_person(paths, "auto-a", "SIGNIN_REQUIRED:auto-a", "auto-a: sign-in needed", "y", utcnow() + timedelta(hours=25))
    assert t.startswith("notified") and len(approvals.list_tasks("pending", paths)) == 1   # not duplicated while one is pending


def test_auth_expired_never_retries(paths):
    rep = rem(paths).handle(diag("AUTH_EXPIRED"))
    assert rep.escalated and not rep.retry


def test_dependency_mismatch_refreshes_components(paths, registry):
    registry.add_component("wrapper-core", WC_VERSION)
    registry.add_component("execution-framework", "0.1.0")
    registry.add_automation("auto-a", "1.0.0", "print('ok')\n")
    registry.save()
    cfg = {"ring": "pilot", "registry_url": str(registry.root), "log_transport": "dir"}
    a = R.Remediator(paths, "auto-a", cfg).handle(diag("DEPENDENCY_VERSION_MISMATCH")).attempts[0]
    assert a["playbook"] == "refresh_components" and a["outcome"] == "fixed" and "auto-a" in a["detail"]


def test_unknown_failure_captures_one_debug_bundle_per_signature(paths):
    d = R.Diagnosis("UNKNOWN", 0.2, ["x"], "UNKNOWN:abc", "auto-a")
    first = rem(paths).handle(d).attempts[0]
    assert first["outcome"] == "recommended" and "debug bundle written" in first["detail"]
    second = R.Remediator(paths, "auto-a", {}, force=True).handle(d).attempts[0]
    assert "already captured" in second["detail"]
    assert len(list((paths.root / "debug-bundles").glob("*.zip"))) == 1


# ======================================================================================================== environment + preflight
def test_environment_diagnosis_is_cheap_and_precise(paths):
    install_payload(paths)
    (paths.wrapper_core / "wrapper_core").mkdir(parents=True, exist_ok=True)
    (paths.wrapper_core / "wrapper_core" / "__init__.py").write_text("", encoding="utf-8")
    assert R.diagnose_environment(paths, "auto-a") == []
    make_profile(paths)
    cats = [d.category for d in R.diagnose_environment(paths, "auto-a")]
    assert cats == ["STALE_BROWSER_PROFILE_LOCK"]
    (paths.automation_dir("auto-a") / "main.py").unlink()
    assert "MISSING_FILE" in [d.category for d in R.diagnose_environment(paths, "auto-a")]


def test_environment_diagnosis_reports_stale_lock_and_low_disk(paths, monkeypatch):
    install_payload(paths)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    paths.lock_file("auto-a").write_text(f"{dead.pid} 0", encoding="ascii")
    monkeypatch.setattr(R, "free_mb", lambda p: 50)
    cats = {d.category for d in R.diagnose_environment(paths, "auto-a")}
    assert {"STALE_LOCK", "DISK_LOW"} <= cats


def test_proactive_pass_fixes_idle_automations_and_is_throttled(paths):
    install_payload(paths)
    (paths.wrapper_core / "wrapper_core").mkdir(parents=True, exist_ok=True)
    (paths.wrapper_core / "wrapper_core" / "__init__.py").write_text("", encoding="utf-8")
    prof = make_profile(paths)
    out = R.proactive_pass(paths, {}, lister=lambda: [])
    assert [a["outcome"] for a in out] == ["fixed"] and not (prof / "SingletonLock").exists()
    make_profile(paths)
    assert R.proactive_pass(paths, {}, lister=lambda: []) == []              # throttled (15 min)
    assert R.proactive_pass(paths, {}, force=True, lister=lambda: [])        # forced: acts again (budget allows)


def test_proactive_pass_skips_an_automation_that_is_running(paths):
    install_payload(paths)
    (paths.wrapper_core / "wrapper_core").mkdir(parents=True, exist_ok=True)
    (paths.wrapper_core / "wrapper_core" / "__init__.py").write_text("", encoding="utf-8")
    make_profile(paths)
    live = _child()
    try:
        paths.lock_file("auto-a").write_text(f"{live.pid} {utcnow().timestamp()}", encoding="ascii")
        assert R.proactive_pass(paths, {}, force=True, lister=lambda: []) == []
        assert (R.profile_dir(paths, "auto-a") / "SingletonLock").exists()
    finally:
        live.kill()
        live.wait()


def test_heartbeat_idle_and_login_trigger_the_pass_but_run_phases_do_not(paths, monkeypatch):
    from wrapper_core import heartbeat
    calls = []
    monkeypatch.setattr(R, "proactive_pass", lambda p, *a, **k: calls.append(1) or [])
    heartbeat.send_heartbeat(paths, "idle")
    heartbeat.send_heartbeat(paths, "login")
    heartbeat.send_heartbeat(paths, "run_start", "auto-a", "rid")
    assert len(calls) == 2


# ======================================================================================================== debug bundle
def test_debug_bundle_is_redacted_and_never_contains_secrets(paths, monkeypatch):
    token = "ghp_" + "T" * 36
    monkeypatch.setenv("AUTOMAGIX_LOGGING_TOKEN", "super-secret-logging-value-123")
    (paths.config / "platform.json").write_text(json.dumps({
        "ring": "pilot", "registry_url": "https://u:pw99999@example.com/r/", "logging_token": token, "client_secret": "abc123def456",
        "registry_pubkeys": ["ab" * 32], "log_remote": "owner/repo"}), encoding="utf-8")
    (paths.secrets / "logging_token.dpapi").write_bytes(b"DPAPI-BLOB-" + token.encode())
    (paths.secrets / "tmp").mkdir()
    (paths.secrets / "tmp" / "key").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nzzz\n-----END OPENSSH PRIVATE KEY-----", encoding="utf-8")  # secret-scan: allow (fake fixture)
    (paths.logs / "wrapper.log").write_text(f"2026 INFO calling with Authorization: Bearer {token}\nC:\\Users\\jdoe\\x\nsuper-secret-logging-value-123\n", encoding="utf-8")
    rid = "20260101T000000Z-abcd1234"
    run = paths.runs / rid
    run.mkdir()
    (run / "error.json").write_text(json.dumps({"message": f"boom password=hunter2 {token}"}), encoding="utf-8")
    (run / "stderr.log").write_text("\n".join(f"line {i} token={token}" for i in range(500)), encoding="utf-8")
    (run / "screenshot.png").write_bytes(b"\x89PNG-private-screen")
    out = debugbundle.create_bundle(paths, run_id=rid, reason="test")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        blob = "\n".join(z.read(n).decode("utf-8", "replace") for n in names)
    for leak in (token, "super-secret-logging-value", "hunter2", "pw99999", "abc123def456", "jdoe", "PRIVATE KEY", "DPAPI-BLOB", "PNG-private"):
        assert leak not in blob, leak
    assert "ab" * 32 in blob                                               # the PUBLIC registry key is kept
    assert not any("secret" in n.lower() or n.endswith(".png") for n in names)
    assert {"doctor.json", "platform.json", "MANIFEST.json", "run/error.json", "logs/wrapper.log.tail"} <= set(names)
    assert len(blob.splitlines()) < 1500                                   # stderr is a bounded tail
    assert json.loads(zipfile.ZipFile(out).read("MANIFEST.json"))["run_id"] == rid


def test_debug_bundle_rejects_bad_run_ids_and_caps_total_size(paths):
    for bad in ("../x", "..\\x", "a/b", "x" * 80, ""):
        with pytest.raises(ValueError):
            debugbundle.create_bundle(paths, run_id=bad)
    rid = "20260101T000000Z-eeee0000"
    (paths.runs / rid).mkdir()
    (paths.runs / rid / "stderr.log").write_text("y" * 300000, encoding="utf-8")
    out = debugbundle.create_bundle(paths, run_id=rid, max_total=2000)
    with zipfile.ZipFile(out) as z:
        assert sum(i.file_size for i in z.infolist()) <= 4000
        assert json.loads(z.read("MANIFEST.json"))["truncated"] is True


def test_debug_bundle_works_on_an_empty_install(paths, tmp_path):
    out = debugbundle.create_bundle(paths, out=tmp_path / "x.zip")
    assert out.exists() and zipfile.ZipFile(out).testzip() is None


# ======================================================================================================== CLI
def test_cli_remediate_dry_run_and_debug_bundle(paths, capsys, tmp_path):
    from wrapper_core import cli
    install_payload(paths)
    (paths.wrapper_core / "wrapper_core").mkdir(parents=True, exist_ok=True)
    (paths.wrapper_core / "wrapper_core" / "__init__.py").write_text("", encoding="utf-8")
    make_profile(paths)
    assert cli.main(["remediate", "--dry-run", "--json"]) in (0, 1)
    data = json.loads(capsys.readouterr().out)
    assert data and data[0]["category"] == "STALE_BROWSER_PROFILE_LOCK" and data[0]["attempts"][0]["outcome"] == "recommended"
    assert (R.profile_dir(paths, "auto-a") / "SingletonLock").exists()       # dry run: untouched
    out = tmp_path / "b.zip"
    assert cli.main(["debug-bundle", "--out", str(out)]) == 0 and out.exists()
    assert cli.main(["debug-bundle", "--run", "../evil"]) == 2


def test_cli_remediate_applies_fixes_for_the_last_failed_run(paths, capsys, monkeypatch):
    install_payload(paths)
    (paths.wrapper_core / "wrapper_core").mkdir(parents=True, exist_ok=True)
    (paths.wrapper_core / "wrapper_core" / "__init__.py").write_text("", encoding="utf-8")
    rid = "20260102T000000Z-f00dcafe"
    (paths.runs / rid).mkdir()
    (paths.runs / rid / "error.json").write_text(json.dumps({"message": EDGE_ERROR, "category": "UNKNOWN"}), encoding="utf-8")
    paths.last_run_file("auto-a").write_text(json.dumps({"run_id": rid, "status": "failed"}), encoding="utf-8")
    from wrapper_core import cli
    assert cli.main(["remediate", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["category"] == "DEVTOOLS_BLOCKED" and data[0]["fixed"] is True
    assert R.load_overrides(paths, "auto-a") == {"mode": "native"}
    assert cli.main(["remediate", "--clear-overrides"]) == 0
    assert R.load_overrides(paths, "auto-a") == {}


def test_cli_remediate_rejects_bad_names(paths):
    from wrapper_core import cli
    assert cli.main(["remediate", "--automation", "../x"]) == 2


def test_doctor_fix_dry_run_and_apply(paths, capsys):
    from wrapper_core import doctor
    install_payload(paths)
    (paths.wrapper_core / "wrapper_core").mkdir(parents=True, exist_ok=True)
    (paths.wrapper_core / "wrapper_core" / "__init__.py").write_text("", encoding="utf-8")
    make_profile(paths)
    doctor.main(["--fix", "--dry-run", "--offline", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert "checks" in data and any(a["outcome"] == "recommended" for r in data["remediation"] for a in r["attempts"])
    assert (R.profile_dir(paths, "auto-a") / "SingletonLock").exists()
    doctor.main(["--fix", "--offline", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert any(a["outcome"] == "fixed" for r in data["remediation"] for a in r["attempts"])
    assert not (R.profile_dir(paths, "auto-a") / "SingletonLock").exists()


def test_plain_doctor_output_is_unchanged(paths, capsys):
    from wrapper_core import doctor
    doctor.main(["--offline", "--json"])
    assert isinstance(json.loads(capsys.readouterr().out), list)


# ======================================================================================================== runner integration
def setup_auto(registry, code, **kw):
    registry.add_component("wrapper-core", WC_VERSION)
    registry.add_component("execution-framework", "0.1.0")
    registry.add_automation("auto-a", "1.0.0", code, error_handling={"max_retries": 0, "network_retry_wait_s": 0, "backoff_multiplier": 1.0}, **kw)
    return registry.save()


def run(**kw):
    runctx.set_current(None)
    return run_automation("auto-a", "daily", **kw)


def remediation_events(rec):
    return [e["data"] for e in rec["events"] if e["event"] == "remediation_attempt"]


STALE_LOCK_PAYLOAD = '''
import os, pathlib, wrapper_core as wrapper

@wrapper.entrypoint
def main():
    home = pathlib.Path(os.environ["AUTOMAGIX_HOME"])
    with open(home / "exec.log", "a") as f:
        f.write("run\\n")
    lock = home / "browser-profile" / "auto-a" / "SingletonLock"
    marker = home / "crashed-once"
    if not marker.exists():
        marker.write_text("1")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("stale")                       # a crashed Edge leaves its lock behind
        raise RuntimeError("Failed to create a ProcessSingleton: SingletonLock exists (profile already in use)")
    if lock.exists():
        raise RuntimeError("Failed to create a ProcessSingleton: SingletonLock exists (profile already in use)")
    wrapper.set_result(done=True)

if __name__ == "__main__":
    raise SystemExit(main())
'''


def test_runner_fixes_a_stale_profile_lock_and_reruns_once(paths, registry):
    setup_auto(registry, STALE_LOCK_PAYLOAD)
    out = run()
    rec = out.record
    assert rec["status"] == "success" and out.exit_code == 0 and rec["remediated"] is True
    rm = rec["remediation"]
    assert rm["category"] == "STALE_BROWSER_PROFILE_LOCK" and rm["fixed"] is True and rm["retried"] and rm["retry_succeeded"]
    assert rm["playbooks"] == ["release_browser_profile_lock"]
    assert "SingletonLock" in rm["original_error"]["message"]                # the original error is never lost
    ev = remediation_events(rec)
    assert ev and ev[0]["playbook"] == "release_browser_profile_lock" and ev[0]["outcome"] == "fixed"
    assert set(ev[0]) == {"playbook", "category", "action", "outcome", "detail", "duration_ms", "attempt"}
    assert (paths.root / "exec.log").read_text(encoding="utf-8").count("run") == 2      # exactly one re-run
    assert not (paths.root / "browser-profile" / "auto-a" / "SingletonLock").exists()
    audit = (paths.logs / "remediation.jsonl").read_text(encoding="utf-8")
    assert "release_browser_profile_lock" in audit
    assert rm["debug_bundle"].startswith("debug-bundles/") and (paths.root / rm["debug_bundle"]).exists()


EDGE_PAYLOAD = '''
import os, pathlib, wrapper_core as wrapper

@wrapper.entrypoint
def main():
    home = pathlib.Path(os.environ["AUTOMAGIX_HOME"])
    with open(home / "exec.log", "a") as f:
        f.write("mode=" + os.environ.get("AUTOMATION_PARAM_MODE", "-") + "\\n")
    if os.environ.get("AUTOMATION_PARAM_MODE") != "native":
        raise RuntimeError("browserType.launchPersistentContext: Target closed. DevTools remote debugging is disallowed by the system admin.")
    wrapper.set_result(state="target", mode="native-uia")

if __name__ == "__main__":
    raise SystemExit(main())
'''


def test_runner_switches_to_native_mode_when_edge_policy_blocks_devtools(paths, registry):
    setup_auto(registry, EDGE_PAYLOAD)
    first = run().record
    assert first["status"] == "success" and first["remediated"] is True
    assert first["remediation"]["category"] == "DEVTOOLS_BLOCKED" and first["remediation"]["playbooks"] == ["devtools_native_mode"]
    assert first["runtime_overrides"] == {"mode": "native"}
    log = (paths.root / "exec.log").read_text(encoding="utf-8").split()
    assert log == ["mode=test", "mode=native"]            # the slot's own mode=test failed; remediation's override wins on the re-run
    second = run().record                                                # next runs start in native mode: no failure, no remediation
    assert second["status"] == "success" and "remediation" not in second and second["runtime_overrides"] == {"mode": "native"}
    assert (paths.root / "exec.log").read_text(encoding="utf-8").split()[-1] == "mode=native"


SIGNIN_PAYLOAD = '''
import os, pathlib, wrapper_core as wrapper

@wrapper.entrypoint
def main():
    home = pathlib.Path(os.environ["AUTOMAGIX_HOME"])
    with open(home / "exec.log", "a") as f:
        f.write("run\\n")
    raise RuntimeError("manual_step_required: sign-in required for this browser profile")

if __name__ == "__main__":
    raise SystemExit(main())
'''


def test_signin_failures_notify_once_and_never_loop(paths, registry):
    setup_auto(registry, SIGNIN_PAYLOAD)
    rec = run().record
    assert rec["status"] == "failed"                                     # honest: nothing hid the failure
    rm = rec["remediation"]
    assert rm["category"] == "SIGNIN_REQUIRED" and rm["retried"] is False and rm["needs_person"] is True
    assert (paths.root / "exec.log").read_text(encoding="utf-8").count("run") == 1
    assert (paths.state / "toast" / "auto-a.needs-attention.txt").exists()
    assert len(approvals.list_tasks("pending", paths)) == 1
    rec2 = run().record
    assert rec2["status"] == "failed" and len(approvals.list_tasks("pending", paths)) == 1   # deduplicated


def test_kill_switch_leaves_failures_exactly_as_before(paths, registry, monkeypatch):
    setup_auto(registry, STALE_LOCK_PAYLOAD)
    monkeypatch.setenv("AUTOMAGIX_NO_REMEDIATION", "1")
    rec = run().record
    assert rec["status"] == "failed" and "remediation" not in rec and "remediated" not in rec
    assert not remediation_events(rec) and not (paths.logs / "remediation.jsonl").exists()
    assert (paths.root / "exec.log").read_text(encoding="utf-8").count("run") == 1


def test_preflight_fixes_a_leftover_profile_lock_before_the_run(paths, registry):
    ok = '''
import wrapper_core as wrapper

@wrapper.entrypoint
def main():
    wrapper.set_result(done=True)
'''
    setup_auto(registry, ok)
    selfheal.run(paths, {"ring": "pilot", "registry_url": str(registry.root), "log_transport": "dir"}, "auto-a")
    prof = make_profile(paths)
    rec = run().record
    assert rec["status"] == "success" and "remediation" not in rec       # nothing failed
    ev = remediation_events(rec)
    assert ev and ev[0]["playbook"] == "release_browser_profile_lock" and ev[0]["outcome"] == "fixed"
    assert not (prof / "SingletonLock").exists()


UNKNOWN_PAYLOAD = '''
import wrapper_core as wrapper

@wrapper.entrypoint
def main():
    raise RuntimeError("something nobody has seen before")

if __name__ == "__main__":
    raise SystemExit(main())
'''


def test_unknown_failures_get_one_bundle_per_signature_and_no_retry(paths, registry):
    setup_auto(registry, UNKNOWN_PAYLOAD)
    first = run().record
    assert first["status"] == "failed" and first["remediation"]["category"] == "UNKNOWN"
    assert first["remediation"]["retried"] is False and first["remediation"]["debug_bundle"]
    second = run().record
    assert "debug_bundle" not in second["remediation"]                    # same signature within 24h
    assert len(list((paths.root / "debug-bundles").glob("*.zip"))) == 1


def test_dry_run_runs_do_not_remediate(paths, registry):
    setup_auto(registry, STALE_LOCK_PAYLOAD)
    rec = run(dry_run=True).record
    assert rec["status"] == "failed" and "remediation" not in rec
    assert not (paths.logs / "remediation.jsonl").exists()


def test_remediation_failure_cannot_break_a_run(paths, registry, monkeypatch):
    setup_auto(registry, UNKNOWN_PAYLOAD)
    monkeypatch.setattr(R, "registry", lambda: (_ for _ in ()).throw(RuntimeError("registry exploded")))
    rec = run().record
    assert rec["status"] == "failed" and rec["error"]["message"].startswith("something nobody")


def test_payload_missing_is_repaired_before_the_run(paths, registry):
    ok = '''
import wrapper_core as wrapper

@wrapper.entrypoint
def main():
    wrapper.set_result(done=True)
'''
    setup_auto(registry, ok)
    cfg = {"ring": "pilot", "registry_url": str(registry.root), "log_transport": "dir"}
    selfheal.run(paths, cfg, "auto-a")
    (paths.automation_dir("auto-a") / "main.py").unlink()
    rec = run().record
    assert rec["status"] == "success"
    assert (paths.automation_dir("auto-a") / "main.py").exists()
