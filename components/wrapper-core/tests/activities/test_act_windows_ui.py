from __future__ import annotations

import os
import sys
import time

import pytest

from wrapper_core import retry
from wrapper_core.activities import windows_ui as ui
from wrapper_core.activities._common import NotAvailable

WIN = sys.platform == "win32"
IN_CI = bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))
DESKTOP = WIN and not IN_CI and not os.environ.get("AMX_SKIP_DESKTOP_TESTS") and ui.has_interactive_desktop()


@pytest.mark.skipif(WIN, reason="off-Windows behaviour")
def test_everything_needs_windows():
    with pytest.raises(NotAvailable):
        ui.Session().start()
    with pytest.raises(NotAvailable):
        ui.launch("notepad.exe")
    assert ui.has_interactive_desktop() is False
    assert retry.classify_exception(NotAvailable("x")) is retry.Category.LOGIC_ERROR


def test_error_classification():
    assert retry.classify_exception(ui.ElementNotFound("x")) is retry.Category.LOGIC_ERROR          # selector problem: do not retry blindly
    assert retry.classify_exception(ui.UIHostError("x")) is retry.Category.SYSTEM_UNAVAILABLE       # host died: retryable
    assert "UIAutomationClient" in ui.HOST_SCRIPT and "ConvertFrom-Json" in ui.HOST_SCRIPT


def test_host_script_loads_ui_automation_lazily_and_never_evals_input():
    s = ui.HOST_SCRIPT
    assert s.count("function Ensure-UIA") == 1 and "Invoke-Expression" not in s and "iex " not in s.lower()
    assert "AMX_UI_TEST" in s                                      # the test-only 'sleep' command is gated


@pytest.mark.skipif(not WIN, reason="needs powershell.exe")
def test_host_protocol_ping_errors_and_clean_shutdown():
    with ui.Session(call_timeout=60) as s:
        r = s.call("ping")
        assert r["pong"] is True and r["ps"].startswith("5.")      # Windows PowerShell 5.1, not pwsh
        with pytest.raises(ui.UIError, match="unknown command"):
            s.call("definitely-not-a-command")
        with pytest.raises(ui.UIError, match="unknown element handle"):
            s.call("click", handle="e999")
        with pytest.raises(ui.UIError, match="disabled"):
            s.call("sleep", seconds=1)                              # test-only command is off by default
        assert s.call("ping")["pong"]                              # still healthy after errors
        proc = s._proc
    assert proc.poll() is not None                                  # closed on exit


@pytest.mark.skipif(not WIN, reason="needs powershell.exe")
def test_hung_host_is_killed_and_reported_not_hung_forever():
    s = ui.Session(call_timeout=30).start({"AMX_UI_TEST": "1"})
    try:
        t0 = time.monotonic()
        with pytest.raises(ui.UIHostError, match="did not answer"):
            s.call("sleep", timeout=2, seconds=60)
        assert time.monotonic() - t0 < 15
        assert s._proc.poll() is not None
        with pytest.raises(ui.UIHostError):
            s.call("ping")                                          # clear error, not a hang
    finally:
        s.close()
    with ui.Session() as fresh:                                     # a new session works again
        assert fresh.call("ping")["pong"]


@pytest.mark.skipif(not WIN or IN_CI, reason="UI Automation assemblies/desktop are not exercised on CI runners")
def test_ui_automation_loads_and_missing_window_is_element_not_found():
    with ui.session():
        wins = ui.list_windows()
        assert isinstance(wins, list)
        with pytest.raises(ui.ElementNotFound):
            ui.find_window(r"^amx-no-such-window-\d{9}$", timeout=1)
        assert ui.find(None, name="amx-no-such-element", timeout=1) is None


@pytest.mark.skipif(not DESKTOP, reason="needs an interactive desktop (skipped on CI / non-interactive sessions)")
def test_notepad_end_to_end():
    with ui.session(call_timeout=60):
        proc = ui.launch("notepad.exe")
        win = None
        try:
            win = ui.find_window(r"Notepad", timeout=20)
            edit = ui.find(win, control_type="Edit", timeout=5) or ui.find(win, control_type="Document", timeout=5)
            assert edit is not None, "notepad has no edit/document control"
            ui.set_text(edit, "hello from AutoStation")
            assert "hello from AutoStation" in ui.get_text(edit)
            shot = ui.screenshot(os.path.join(os.environ["AUTOMAGIX_HOME"], "shot.png"), window=win)
            assert os.path.getsize(shot) > 1000
        finally:
            if win is not None:
                try:
                    ui.close(win)
                    # "Do you want to save?" -> don't save
                    for label in ("Don't save", "Don’t save", "No"):
                        dlg = ui.find(None, name=label, control_type="Button", timeout=3)
                        if dlg is not None:
                            ui.click(dlg)
                            break
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
