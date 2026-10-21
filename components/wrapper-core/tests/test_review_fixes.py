"""Regression tests for the findings of the wrapper-core security review (remediation, approvals, queue, http, tray, schedule, assets)."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wrapper_core import debugbundle, manifest as mf, playbooks, remediation as R, runner as runner_mod, selfheal, tray
from wrapper_core import assetconf, schedule as sc, scheduler, testing
from wrapper_core.activities import approvals, connectors, http as ahttp, queue as qmod
from wrapper_core.paths import Paths
from wrapper_core.retry import DataError, LogicError
from wrapper_core.util import iso

from test_remediation import FakeSched, diag, install_payload, make_profile, rem  # noqa: E402


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.delenv("AUTOMAGIX_NO_REMEDIATION", raising=False)
    monkeypatch.setattr(runner_mod, "_sleep", lambda s: None)
    monkeypatch.setattr(playbooks, "list_browser_processes", lambda: [])


def _sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


# ================================================================ 1. repair_component is transactional
def test_corrupt_component_with_registry_offline_is_not_fixed_and_the_install_record_survives(paths):
    install_payload(paths)
    cfg = {"ring": "pilot", "registry_url": "http://127.0.0.1:9/", "log_transport": "dir"}
    before = selfheal.read_installed(paths)
    rep = R.Remediator(paths, "auto-a", cfg, scheduler_runner=FakeSched()).handle(diag("COMPONENT_CORRUPT"))
    att = rep.attempts[0]
    assert att["playbook"] == "repair_component" and att["outcome"] == "not_fixed", att
    assert not rep.retry and not rep.fixed
    assert (paths.automation_dir("auto-a") / "VERSION").exists()
    assert selfheal.read_installed(paths) == before            # the automation is still visible to doctor, tasks and the idle pass
    assert not list((paths.state / "repair-pending").glob("*.json"))


def test_a_repair_that_died_half_way_is_recovered_at_the_next_start(paths):
    install_payload(paths)
    marker = paths.automation_dir("auto-a") / "VERSION"
    inst = selfheal.read_installed(paths)
    pend = playbooks.pending_repair_file(paths, "auto-a")
    from wrapper_core.util import write_json
    write_json(pend, {"installed_before": inst, "markers": {str(marker): "1.0.0\n"}, "automation": "auto-a", "at": iso()})
    marker.unlink()                                            # the process died after wiping the marker and the record
    wiped = {"automations": {}, "components": {}}
    selfheal.write_installed(paths, wiped)
    R.preflight(paths, "auto-a", {}, None)
    assert marker.exists() and selfheal.read_installed(paths)["automations"].get("auto-a") == "1.0.0"
    assert not pend.exists()


# ================================================================ 2. never touch a browser while another run owns it
def test_profile_lock_is_left_alone_while_another_run_is_live(paths):
    prof = make_profile(paths)
    child = _sleeper()
    try:
        lock = paths.lock_file("auto-a")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"{child.pid} {time.time()}", encoding="ascii")
        killed = []
        procs = [(901, "msedge.exe", f'msedge.exe --user-data-dir="{prof}"')]
        rep = R.Remediator(paths, "auto-a", {}, lister=lambda: procs, killer=lambda pid: killed.append(pid) or True, force=True).handle(
            diag("STALE_BROWSER_PROFILE_LOCK"))
        assert rep.attempts[0]["outcome"] == "skipped" and killed == []
        assert (prof / "SingletonLock").exists()
    finally:
        child.kill()
        child.wait()


def test_the_runners_own_lock_does_not_block_its_own_remediation(paths):
    prof = make_profile(paths)
    lock = paths.lock_file("auto-a")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{os.getpid()} {time.time()}", encoding="ascii")        # the runner holds the lock while it remediates a failed run
    assert R.other_run_live(paths, "auto-a") is False
    rep = R.Remediator(paths, "auto-a", {}, lister=lambda: [], killer=lambda pid: True).handle(diag("STALE_BROWSER_PROFILE_LOCK"))
    assert rep.attempts[0]["outcome"] == "fixed" and not (prof / "SingletonLock").exists()


def test_the_command_line_is_rechecked_right_before_each_kill(paths):
    prof = make_profile(paths)
    state = {"calls": 0}

    def lister():
        state["calls"] += 1
        # the first listing (fix's loop) sees the process; by the time of the re-check just before the kill it has gone (the PID could be reused)
        return [(777, "msedge.exe", f'msedge.exe --user-data-dir="{prof}"')] if state["calls"] <= 1 else []

    killed = []
    R.Remediator(paths, "auto-a", {}, lister=lister, killer=lambda pid: killed.append(pid) or True).handle(diag("STALE_BROWSER_PROFILE_LOCK"))
    assert killed == []


# ================================================================ 3. the payload is not re-run blindly
class _Ctx:
    def __init__(self, events):
        self._e = events

    def read_events(self):
        return self._e


def _item(outcome="success"):
    return {"event": "item_processed", "data": {"outcome": outcome}}


def test_retry_after_remediation_is_gated():
    ok = runner_mod._retry_allowed
    assert ok("STALE_BROWSER_PROFILE_LOCK", _Ctx([]), {})[0] is True                        # startup problem, nothing processed
    assert ok("DEVTOOLS_BLOCKED", _Ctx([{"event": "start"}]), {})[0] is True
    assert ok("STALE_BROWSER_PROFILE_LOCK", _Ctx([_item()]), {})[0] is False                # items already processed
    assert ok("DISK_LOW", _Ctx([]), {})[0] is False                                         # could be mid-run
    assert ok("DISK_LOW", _Ctx([_item()]), {"remediation_retry": True})[0] is True          # idempotent automation opted in
    assert ok("DISK_LOW", _Ctx([]), {"remediation_retry": "true"})[0] is True


class _Boom:
    def read_events(self):
        raise RuntimeError("x")


def test_unknown_progress_means_no_retry():
    assert runner_mod._retry_allowed("MISSING_FILE", _Boom(), {})[0] is False


# ================================================================ 4. matchers
@pytest.mark.parametrize("message,tb,category", [
    ("boom", ['File "wrapper_core/runner.py", line 401, in run'], "UNKNOWN"),
    ("boom", ['File "wrapper_core/runner.py", line 407, in run'], "UNKNOWN"),
    ("request failed: HTTP 401 Unauthorized", None, "AUTH_EXPIRED"),
    ("status code 401 from the API", None, "AUTH_EXPIRED"),
    ("HTTP 407 Proxy Authentication Required", None, "PROXY_FAILURE"),
    ("error: 407", None, "PROXY_FAILURE"),
])
def test_status_code_matchers_are_anchored(message, tb, category):
    assert R.diagnose({"message": message, "traceback_tail": tb}, {}).category == category


# ================================================================ 5. time zones
def _no_tzdata(monkeypatch):
    import zoneinfo

    def boom(name):
        raise zoneinfo.ZoneInfoNotFoundError(name)
    monkeypatch.setattr(zoneinfo, "ZoneInfo", boom)


def test_unresolvable_zone_is_refused_for_task_registration_and_warned_elsewhere(monkeypatch, caplog):
    _no_tzdata(monkeypatch)
    with pytest.raises(mf.TimezoneUnresolvable):
        mf._tz("Europe/London", strict=True)
    with pytest.raises(ValueError):
        sc._localize("09:00", "Europe/London")
    with pytest.raises(ValueError):
        scheduler._local_time("09:00", "Europe/London")
    assert mf._tz("UTC", strict=True) is timezone.utc and not mf.tz_resolves("Europe/London") and mf.tz_resolves("UTC") and mf.tz_resolves(None)
    import logging
    wlog = logging.getLogger("wrapper_core")
    wlog.addHandler(caplog.handler)
    try:
        mf._TZ_WARNED.discard("Asia/Nowhere")
        assert mf._tz("Asia/Nowhere") is None            # non-strict: still falls back, but never silently
    finally:
        wlog.removeHandler(caplog.handler)
    assert "cannot be resolved" in caplog.text


def test_register_skips_a_slot_whose_zone_cannot_be_resolved(paths, monkeypatch):
    _no_tzdata(monkeypatch)
    fake = FakeSched()
    s = scheduler.Scheduler(paths, runner=fake) if hasattr(scheduler, "Scheduler") else None
    if s is None:
        pytest.skip("scheduler class not found")
    touched = s.register("auto-a", {"timezone": "Europe/London", "slots": [{"id": "daily", "type": "daily", "time": "09:00"}]})
    assert touched == [] and fake.tasks == {}


def test_manifest_validate_rejects_garbage_time_zone_names():
    m = {"schema_version": 1, "components": {}, "automations": {"a": {"description": "d", "owner": "o", "versions": {"1.0.0": {"url": "u", "sha256": "0" * 64}},
                                                                          "rings": {"dev": "1.0.0", "pilot": "1.0.0", "broad": "1.0.0"},
                                                                          "schedule": {"timezone": "../../etc", "slots": []}}}}
    assert any("IANA" in e for e in mf.validate(m))
    m["automations"]["a"]["schedule"]["timezone"] = "Europe/London"
    assert not any("IANA" in e for e in mf.validate(m))


# ================================================================ 6. approvals
def test_bidi_and_format_characters_are_stripped():
    assert approvals._clean("1250" + chr(0x202E) + "00" + chr(0x200B) + chr(0x2066), 50, "t") == "125000"


def _serve(paths, idle_s=30.0):
    info = {}
    ready = threading.Event()

    def on_ready(url, token, port):
        info.update(url=url, token=token, port=port)
        ready.set()

    th = threading.Thread(target=lambda: info.__setitem__("n", approvals.serve(paths, 0, False, idle_s, on_ready)), daemon=True)
    th.start()
    assert ready.wait(5)
    return th, info


def _get(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_non_ascii_token_gets_a_clean_403_not_a_dropped_connection(paths):
    th, info = _serve(paths)
    assert _get(f"http://127.0.0.1:{info['port']}/?t=%C3%A9") == 403
    assert _get(info["url"]) == 200            # (the daemon server thread ends with the test process)


def test_an_idle_half_open_connection_does_not_block_the_page(paths):
    th, info = _serve(paths)
    s = socket.create_connection(("127.0.0.1", info["port"]))
    try:
        s.sendall(b"GET /?t=x HTTP/1.0\r\nHost: 127.0.0.1")        # incomplete headers, connection kept open
        t0 = time.monotonic()
        assert _get(info["url"]) == 200
        assert time.monotonic() - t0 < 3
    finally:
        s.close()


def test_port_cannot_be_shared_and_probes_do_not_keep_the_page_alive(paths):
    assert approvals._LoopbackServer.allow_reuse_address is False
    th, info = _serve(paths, idle_s=1.2)
    with pytest.raises(OSError):
        approvals._LoopbackServer(("127.0.0.1", info["port"]), approvals.BaseHTTPRequestHandler)
    end = time.monotonic() + 2.4
    while time.monotonic() < end and th.is_alive():
        try:
            _get(f"http://127.0.0.1:{info['port']}/?t=wrong", timeout=1)      # unauthenticated probing must not count as activity
        except OSError:
            pass
        time.sleep(0.15)
    th.join(3)
    assert not th.is_alive()


def test_needs_person_creates_an_approval_even_for_a_long_title(paths):
    R.needs_person(paths, "auto-a", "k1", "T" * 300, "body " * 400)
    tasks = approvals.list_tasks("pending", paths)
    assert len(tasks) == 1 and len(tasks[0].title) <= 120


def test_old_decided_approvals_are_pruned_automatically(paths):
    t = approvals.create("old", "x", automation="auto-a", notify=False, paths=paths)
    approvals.decide(t.id, "approve", paths=paths)
    f = approvals._dir(paths) / f"{t.id}.json"
    old = time.time() - 40 * 86400
    os.utime(f, (old, old))
    marker = approvals._dir(paths) / ".pruned"
    if marker.exists():
        marker.unlink()
    approvals.create("new", "x", automation="auto-a", notify=False, paths=paths)
    assert not f.exists()


# ================================================================ 7. debug bundle
def _run_dir(paths, name, run_id, files, synopsis="ordinary run"):
    d = paths.runs / run_id
    d.mkdir(parents=True, exist_ok=True)
    for fn, text in files.items():
        (d / fn).write_text(text, encoding="utf-8")
    from wrapper_core.util import write_json
    write_json(paths.state / "runs" / f"{name}.json", {"run_id": run_id, "status": "failed", "synopsis": synopsis})
    return d


def test_bundle_picks_the_requested_automations_run_and_withholds_classified_output(paths):
    install_payload(paths, "auto-a")
    install_payload(paths, "auto-b")
    (paths.automation_dir("auto-b") / "automation.yaml").write_text(
        "name: auto-b\nversion: 1.0.0\nruntime: python\nentrypoint: main.py\ndata_classification: confidential\n", encoding="utf-8")
    _run_dir(paths, "auto-a", "20260101T000000Z-aaaaaaaa", {"events.jsonl": '{"event":"x","data":{"who":"ordinary"}}\n'})
    time.sleep(0.05)
    _run_dir(paths, "auto-b", "20260101T000100Z-bbbbbbbb", {"events.jsonl": '{"event":"x","data":{"invoice":"ACME-4711"}}\n', "stderr.log": "ACME secret stuff"},
             synopsis="invoice 4711 for ACME failed")
    import zipfile
    out = debugbundle.create_bundle(paths, reason="t", automation="auto-a")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert "run/events.jsonl.tail" in names                                 # auto-a's own run, though auto-b's is newer
        assert "ordinary" in z.read("run/events.jsonl.tail").decode()
        assert "ACME" not in z.read("last_runs.json").decode()                  # classified automation: no synopsis in the bundle
    out_b = debugbundle.create_bundle(paths, reason="t", automation="auto-b", out=paths.root / "b.zip")
    with zipfile.ZipFile(out_b) as z:
        assert "run/summary.json" in z.namelist() and not any(n.startswith("run/events") or n.endswith("stderr.log.tail") for n in z.namelist())
        assert all("ACME" not in z.read(n).decode("utf-8", "replace") for n in z.namelist())


def test_bundle_failure_leaves_no_partial_zip(paths, monkeypatch):
    def boom(*a, **k):
        Path(a[1]).write_bytes(b"PK partial")
        raise RuntimeError("disk full")
    monkeypatch.setattr(debugbundle, "_write_bundle", boom)
    out = paths.root / "x.zip"
    with pytest.raises(RuntimeError):
        debugbundle.create_bundle(paths, out=out)
    assert not out.exists() and not list(paths.root.glob("*.part"))


def test_scrub_covers_posix_home_paths_sas_signatures_and_emails():
    s = R.scrub_text("open /home/alice/work/x.py?sig=AbC%2Bdef%3D&sv=2020 mail bob@example.com see /usr/lib/python3/dist-packages/foo")
    assert "alice" not in s and "AbC" not in s and "bob@example.com" not in s
    assert "/usr/lib/python3/dist-packages/foo" in s                         # ordinary paths with a digit stay readable


# ================================================================ 8. queue fencing
@pytest.fixture
def q(tmp_path):
    t = [1000.0]
    qmod._clock = lambda: t[0]
    q = qmod.open("fence", tmp_path / "fence.db")
    q.t = t
    yield q
    qmod._clock = time.time


def test_a_slow_worker_cannot_finish_a_claim_that_was_requeued_to_someone_else(q):
    q.add("R1", {})
    a = q.next("worker-a")
    q.t[0] += 1000
    assert q.requeue_stale(older_than_s=500) == 1
    b = q.next("worker-b")
    assert b is not None and b.reference == "R1"
    with pytest.raises(LogicError):
        q.succeed(a)                       # worker A woke up late: it must NOT complete worker B's claim
    q.succeed(b)
    assert q.get("R1").status == "successful"


# ================================================================ 9. http
def test_download_failure_leaves_no_part_file_and_no_leaked_descriptor(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(ahttp, "_urlopen", boom)
    with pytest.raises(Exception):
        ahttp.download("https://example.invalid/x.bin", tmp_path / "x.bin")
    assert list(tmp_path.glob("*.part")) == []


@pytest.mark.parametrize("value,expected", [("nan", None), ("-5", None), ("inf", None), ("30", 30.0), ("999", 60.0), ("abc", None)])
def test_retry_after_is_sanitised(value, expected):
    class E:
        headers = {"Retry-After": value}
    assert ahttp.Client._retry_after(E()) == expected


def test_https_to_loopback_http_redirect_is_refused_outside_test_mode(monkeypatch):
    monkeypatch.delenv("AMX_ALLOW_TEST_HOSTS", raising=False)
    req = urllib.request.Request("https://example.com/a")
    h = ahttp._SafeRedirect()
    with pytest.raises(DataError):
        h.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1:8080/admin")
    monkeypatch.setenv("AMX_ALLOW_TEST_HOSTS", "1")
    assert h.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1:8080/ok") is not None


def test_graph_drive_id_cannot_walk_the_url():
    for bad in (".", "..", "...", "a/b"):
        with pytest.raises(ValueError):
            connectors.Graph._owner(None, bad)
    assert connectors.Graph._owner(None, "b!abc-123") == "/drives/b%21abc-123"


# ================================================================ 10. tray
def test_site_url_ignores_a_bad_port_instead_of_raising():
    assert tray.site_url({"registry_url": "https://h.github.io:99999/r/registry/"}) is None
    assert tray.site_url({"registry_url": "https://h.github.io:abc/r/registry/"}) is None
    assert tray.site_url({"registry_url": "https://h.github.io/r/registry/"}) == "https://h.github.io/r/"


def test_pid_reuse_is_detected_by_creation_time():
    assert tray.pid_alive(os.getpid())
    if os.name == "nt":
        started = tray.process_start_time(os.getpid())
        assert isinstance(started, int) and tray.pid_alive(os.getpid(), started)
        assert not tray.pid_alive(os.getpid(), started + 10 * 10_000_000)        # recorded 10 s apart: a different process owns this PID now
    else:
        assert tray.process_start_time(os.getpid()) is None


# ================================================================ 11. test harness / assets / schedules / remediation state
def test_the_test_harness_disables_remediation_by_default():
    assert "AUTOMAGIX_NO_REMEDIATION" in testing._ENV_KEYS


def test_nan_and_infinity_are_not_valid_assets(tmp_path):
    clean, problems = assetconf.validate_assets({"ratio": float("nan"), "limit": float("inf"), "ok": 1.5}, None)
    assert clean == {"ok": 1.5} and len(problems) == 2
    f = tmp_path / "u.json"
    f.write_text('{"ratio": NaN}', encoding="utf-8")
    probs: list = []
    assert assetconf._read_layer(f, "user", {}, probs) == {} and probs


def test_an_untrusted_machine_assets_file_is_ignored(paths, tmp_path, monkeypatch):
    f = tmp_path / "assets.json"
    f.write_text('{"greeting": "injected"}', encoding="utf-8")
    monkeypatch.delenv("AUTOMAGIX_MACHINE_ASSETS", raising=False)
    monkeypatch.setattr(assetconf, "machine_assets_path", lambda: f)
    monkeypatch.setattr(assetconf, "machine_file_trusted", lambda p: (False, "owned by a standard user"))
    assert "greeting" not in assetconf.effective_assets(paths, params={})
    monkeypatch.setattr(assetconf, "machine_file_trusted", lambda p: (True, ""))
    assert assetconf.effective_assets(paths, params={}).get("greeting") == "injected"


def test_concurrent_state_updates_are_not_lost(paths):
    def work(i):
        for j in range(15):
            R.update_state(paths, lambda st, k=f"pb{i}:{j}": st["attempts"].setdefault(k, []).append("t"))

    ts = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(R.load_state(paths)["attempts"]) == 90
    assert not (paths.state / "remediation.json.lock").exists()


def test_prune_only_deletes_names_this_tool_creates_and_never_the_spool(paths):
    root = paths.root
    keep_dir = root / "my-photos.old"
    keep_dir.mkdir()
    spool = paths.spool / "kind-abc.json"
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text("{}", encoding="utf-8")
    own = root / "wrapper-core.old"
    own.mkdir()
    ancient = time.time() - 90 * 86400
    for p in (keep_dir, spool, own):
        os.utime(p, (ancient, ancient))
    ctx = R.Context(paths, "auto-a", {}, diag("DISK_LOW"))
    playbooks.prune_local(ctx)
    assert keep_dir.exists() and spool.exists() and not own.exists()


def test_quarantine_rotation_never_touches_another_automations_folders(paths):
    make_profile(paths, "a")
    bad = paths.root / "profiles.bad"
    bad.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        (bad / f"a-b-2020010{i}T000000").mkdir()
        (bad / f"a-2020010{i}T000000").mkdir()
    R.Remediator(paths, "a", {}).handle(diag("CORRUPT_BROWSER_PROFILE", automation="a"))
    assert len([d for d in bad.iterdir() if d.name.startswith("a-b-")]) == 5
    assert len([d for d in bad.iterdir() if d.name.startswith("a-2")]) == 3


@pytest.mark.skipif(os.name == "nt", reason="SingletonLock is a symlink on POSIX only")
def test_a_symlink_singleton_lock_is_removed_without_following_it(paths, tmp_path):
    prof = make_profile(paths, locks=())
    target = tmp_path / "precious.txt"
    target.write_text("keep", encoding="utf-8")
    link = prof / "SingletonLock"
    os.symlink(str(target), str(link))
    assert not R.inside_root(paths, link) and R.inside_root(paths, link, allow_link=True)
    rep = R.Remediator(paths, "auto-a", {}, lister=lambda: []).handle(diag("STALE_BROWSER_PROFILE_LOCK"))
    assert rep.attempts[0]["outcome"] == "fixed" and not link.is_symlink() and target.read_text(encoding="utf-8") == "keep"


def test_owner_repo_remote_with_legacy_dir_transport_is_healed_to_github_api(tmp_path, monkeypatch):
    import json
    from wrapper_core.paths import Paths, platform_config
    monkeypatch.setenv("AUTOMAGIX_HOME", str(tmp_path))
    monkeypatch.delenv("AUTOMAGIX_LOG_TRANSPORT", raising=False)
    monkeypatch.delenv("AUTOMAGIX_LOG_REMOTE", raising=False)
    p = Paths().ensure()
    (p.config / "platform.json").write_text(json.dumps({"ring": "broad", "log_remote": "owner/logs-repo", "log_transport": "dir"}), encoding="utf-8")
    assert platform_config(p)["log_transport"] == "github-api"
    (p.config / "platform.json").write_text(json.dumps({"ring": "broad", "log_remote": str(tmp_path / "logs"), "log_transport": "dir"}), encoding="utf-8")
    assert platform_config(p)["log_transport"] == "dir"            # a real directory stays a directory
