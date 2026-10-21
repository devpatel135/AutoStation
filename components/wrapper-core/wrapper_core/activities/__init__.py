"""Activities: the building blocks a UiPath workflow expects, as plain, unattended-safe Python.

    from wrapper_core.activities import excel, mail, files, http, db, queue, credentials, windows_ui, pdf, images, notify

Submodules import lazily, so ``import wrapper_core.activities`` costs nothing. Everything is stdlib-only by default;
optional third-party libraries (pdfminer.six, pytesseract, pyautogui/opencv) are guarded with a clear ``NotAvailable``.
Errors are raised as the platform's classified exceptions (NetworkError, AuthExpired, SystemUnavailable, DataError,
LogicError) so ``wrapper.run_item`` / the retry engine handle them without any extra code.
"""
from __future__ import annotations

import importlib

_SUBMODULES = ("excel", "mail", "files", "http", "db", "queue", "credentials", "windows_ui", "pdf", "images", "notify")

__all__ = list(_SUBMODULES) + ["NotAvailable", "ActivityError"]


def __getattr__(name: str):
    if name in _SUBMODULES:
        mod = importlib.import_module("." + name, __name__)
        globals()[name] = mod
        return mod
    if name in ("NotAvailable", "ActivityError"):
        from . import _common
        return getattr(_common, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
