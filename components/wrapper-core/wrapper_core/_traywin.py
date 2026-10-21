"""Windows notification-area icon (ctypes only; imported lazily by ``tray.main`` on win32).

One hidden top-level window + one NOTIFYICONDATAW + a 1 s timer. The window procedure is a closure over ``_State`` so nothing is
global; every Win32 prototype is declared (64-bit safe handles); the icon is always removed on every exit path (WM_DESTROY,
WM_ENDSESSION, atexit). Returns 0 even when the shell is unavailable: a missing indicator is never an error.
"""
from __future__ import annotations

import atexit
import ctypes
import json
import os
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from . import _trayicon, tray

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY, WM_CLOSE, WM_COMMAND, WM_TIMER = 0x0002, 0x0010, 0x0111, 0x0113
WM_QUERYENDSESSION, WM_ENDSESSION, WM_NULL, WM_APP = 0x0011, 0x0016, 0x0000, 0x8000
WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0203, 0x0205
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_BOTTOMALIGN = 0x0002, 0x0020
LR_DEFAULTCOLOR = 0
CMD_RUNS, CMD_SITE = 2, 3
TIMER_ID = 1
TICK_MS = 250                   # stop-file / PID checks four times a second; the tooltip text changes once a second
CALLBACK_MSG = WM_APP + 1
ADD_RETRY_S = 30


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("guidItem", GUID)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


def _api():
    u, s, k = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("shell32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    u.RegisterClassW.argtypes, u.RegisterClassW.restype = [ctypes.POINTER(WNDCLASSW)], wintypes.ATOM
    u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    u.CreateWindowExW.restype = wintypes.HWND
    u.DefWindowProcW.argtypes, u.DefWindowProcW.restype = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], LRESULT
    u.GetMessageW.argtypes, u.GetMessageW.restype = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT], ctypes.c_int
    u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DispatchMessageW.argtypes, u.DispatchMessageW.restype = [ctypes.POINTER(wintypes.MSG)], LRESULT
    u.PostQuitMessage.argtypes = [ctypes.c_int]
    u.DestroyWindow.argtypes, u.DestroyWindow.restype = [wintypes.HWND], wintypes.BOOL
    u.SetTimer.argtypes, u.SetTimer.restype = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID], ctypes.c_size_t
    u.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    u.RegisterWindowMessageW.argtypes, u.RegisterWindowMessageW.restype = [wintypes.LPCWSTR], wintypes.UINT
    u.CreatePopupMenu.restype = wintypes.HMENU
    u.AppendMenuW.argtypes, u.AppendMenuW.restype = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR], wintypes.BOOL
    u.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
    u.DestroyMenu.argtypes = [wintypes.HMENU]
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    u.GetShellWindow.restype = wintypes.HWND
    u.GetSystemMetrics.argtypes, u.GetSystemMetrics.restype = [ctypes.c_int], ctypes.c_int
    u.CreateIconFromResourceEx.argtypes = [ctypes.c_char_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    u.CreateIconFromResourceEx.restype = wintypes.HICON
    u.DestroyIcon.argtypes = [wintypes.HICON]
    s.Shell_NotifyIconW.argtypes, s.Shell_NotifyIconW.restype = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)], wintypes.BOOL
    s.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(NOTIFYICONIDENTIFIER), ctypes.POINTER(wintypes.RECT)]
    s.Shell_NotifyIconGetRect.restype = ctypes.c_long
    k.GetModuleHandleW.argtypes, k.GetModuleHandleW.restype = [wintypes.LPCWSTR], wintypes.HMODULE
    return u, s, k


class _State:
    def __init__(self, name: str, pid: int, stop_file: Optional[Path], timeout_s: int, url: Optional[str], runs_dir: Path, verify: bool):
        self.name, self.pid, self.stop_file, self.timeout_s, self.url, self.runs_dir, self.verify = name, pid, stop_file, timeout_s, url, runs_dir, verify
        self.pid_start = 0
        self.start = time.monotonic()
        self.added = False
        self.hwnd = None
        self.hicon = None
        self.taskbar_created = 0
        self.nid: Optional[NOTIFYICONDATAW] = None
        self.exit_code = 0
        self.last_sec = -1


def _make_icon(u) -> Optional[int]:
    wanted = u.GetSystemMetrics(49) or 16               # SM_CXSMICON (16 at 100% DPI, 20/24/32 when scaled)
    size = _trayicon.best_size(wanted)
    dib = _trayicon.dib_for(size)
    return u.CreateIconFromResourceEx(dib, len(dib), True, 0x00030000, wanted, wanted, LR_DEFAULTCOLOR) or None


def run(name: str, pid: int, stop_file: Optional[Path], timeout_s: int, url: Optional[str], runs_dir: Path, verify: bool = False,
        pid_start: int = 0) -> int:
    try:
        u, s, k = _api()
    except Exception:  # noqa: BLE001
        return 0
    if not u.GetShellWindow():                           # no desktop shell (service / non-interactive session): nothing to show
        return 0
    st = _State(name, pid, stop_file, timeout_s, url, runs_dir, verify)
    st.pid_start = int(pid_start or 0)

    def remove_icon() -> None:
        if st.added and st.nid is not None:
            try:
                s.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(st.nid))
            except Exception:  # noqa: BLE001
                pass
            st.added = False

    def build_nid() -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID = st.hwnd, 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = CALLBACK_MSG
        nid.hIcon = st.hicon
        nid.szTip = tray.tooltip(st.name, time.monotonic() - st.start)
        return nid

    def add_icon() -> bool:
        st.nid = build_nid()
        st.added = bool(s.Shell_NotifyIconW(NIM_ADD, ctypes.byref(st.nid)))
        return st.added

    def open_runs() -> None:
        try:
            if st.runs_dir.is_dir():
                os.startfile(str(st.runs_dir))           # noqa: S606 - a validated local directory, no shell
        except OSError:
            pass

    def open_site() -> None:
        if st.url:
            try:
                webbrowser.open(st.url)                  # https only, validated by tray.site_url
            except Exception:  # noqa: BLE001
                pass

    def show_menu() -> None:
        menu = u.CreatePopupMenu()
        if not menu:
            return
        try:
            u.AppendMenuW(menu, MF_STRING | MF_GRAYED, 1, tray.sanitize_name(st.name) + " - running")
            u.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            u.AppendMenuW(menu, MF_STRING, CMD_RUNS, "Open run log folder")
            if st.url:
                u.AppendMenuW(menu, MF_STRING, CMD_SITE, "Open AUTOSTATION dashboard")
            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            u.SetForegroundWindow(st.hwnd)               # required or the menu will not dismiss on an outside click
            u.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN, pt.x, pt.y, 0, st.hwnd, None)
            u.PostMessageW(st.hwnd, WM_NULL, 0, 0)
        finally:
            u.DestroyMenu(menu)

    def should_stop() -> bool:
        if time.monotonic() - st.start > st.timeout_s:
            return True
        if st.stop_file is not None and st.stop_file.exists():
            return True
        return not tray.pid_alive(st.pid, st.pid_start or None)

    def wndproc(hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TIMER:
                if should_stop():
                    u.DestroyWindow(hwnd)
                elif not st.added:
                    if time.monotonic() - st.start > ADD_RETRY_S:
                        u.DestroyWindow(hwnd)            # the shell never accepted the icon: give up quietly
                    else:
                        add_icon()
                else:
                    sec = int(time.monotonic() - st.start)
                    if sec != st.last_sec:
                        st.last_sec = sec
                        st.nid = build_nid()
                        st.nid.uFlags = NIF_TIP
                        s.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(st.nid))
                return 0
            if msg == CALLBACK_MSG:
                event = lparam & 0xFFFF
                if event == WM_LBUTTONDBLCLK:
                    open_runs()
                elif event == WM_RBUTTONUP:
                    show_menu()
                return 0
            if msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                if cmd == CMD_RUNS:
                    open_runs()
                elif cmd == CMD_SITE:
                    open_site()
                return 0
            if msg == WM_CLOSE:
                u.DestroyWindow(hwnd)
                return 0
            if msg == WM_QUERYENDSESSION:
                return 1
            if msg == WM_ENDSESSION:
                if wparam:
                    remove_icon()
                    u.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                u.KillTimer(hwnd, TIMER_ID)
                remove_icon()
                u.PostQuitMessage(0)
                return 0
            if st.taskbar_created and msg == st.taskbar_created:   # explorer restarted: its tray was wiped, add the icon again
                st.added = False
                add_icon()
                return 0
        except Exception:  # noqa: BLE001 - a window procedure must never raise into the message loop
            pass
        return u.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc_ref = WNDPROC(wndproc)                          # keep alive for the lifetime of the window
    cls = WNDCLASSW()
    cls.lpfnWndProc = proc_ref
    cls.hInstance = k.GetModuleHandleW(None)
    cls.lpszClassName = f"AutostationTray{os.getpid()}"
    if not u.RegisterClassW(ctypes.byref(cls)):
        return 0
    st.hwnd = u.CreateWindowExW(0x80, cls.lpszClassName, "AUTOSTATION tray", 0, 0, 0, 0, 0, None, None, cls.hInstance, None)   # WS_EX_TOOLWINDOW, never shown
    if not st.hwnd:
        return 0
    st.taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")
    st.hicon = _make_icon(u)
    atexit.register(remove_icon)
    add_icon()                                           # if explorer is not ready yet the timer retries for ADD_RETRY_S seconds
    if verify:
        rect = wintypes.RECT()
        ident = NOTIFYICONIDENTIFIER()
        ident.cbSize, ident.hWnd, ident.uID = ctypes.sizeof(NOTIFYICONIDENTIFIER), st.hwnd, 1
        hr = s.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(rect)) if st.added else -1
        print(json.dumps({"added": st.added, "registered": hr == 0, "hresult": hr & 0xFFFFFFFF,
                          "rect": [rect.left, rect.top, rect.right, rect.bottom] if hr == 0 else None, "name": st.name}), flush=True)
    u.SetTimer(st.hwnd, TIMER_ID, TICK_MS, None)
    msg = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u.TranslateMessage(ctypes.byref(msg))
        u.DispatchMessageW(ctypes.byref(msg))
    remove_icon()
    if st.hicon:
        u.DestroyIcon(st.hicon)
    if st.stop_file is not None:
        try:
            st.stop_file.unlink()
        except OSError:
            pass
    return st.exit_code
