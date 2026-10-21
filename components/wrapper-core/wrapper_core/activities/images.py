"""Optional image activities: OCR and on-screen image search (UiPath: Read PDF With OCR, OCR Text, Find Image / Click Image).

These are thin, guarded hooks around third-party libraries that are NOT installed by default (the endpoint is airgapped and these
need native components):

  * ``ocr_image``      -> ``pytesseract`` + Pillow + the Tesseract engine installed on the machine
  * ``find_on_screen`` -> ``pyautogui`` + Pillow (+ ``opencv-python`` for the ``confidence`` option)

Without them a ``NotAvailable`` error explains what to install. Image-based automation is a last resort (it is resolution,
theme and scaling dependent): prefer APIs, browser selectors (execution-framework) or UI Automation (``windows_ui``).
"""
from __future__ import annotations

import shutil
from typing import Optional, Tuple

from ..retry import DataError
from ._common import NotAvailable, emit


def ocr_image(path: str, lang: str = "eng", config: str = "") -> str:
    """Return the text found in an image file (png/jpg/tiff/...)."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as exc:
        raise NotAvailable("OCR needs `pip install pytesseract pillow` and the Tesseract engine on this machine") from exc
    if not shutil.which(getattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")):
        raise NotAvailable("the Tesseract engine is not on PATH (set pytesseract.pytesseract.tesseract_cmd)")
    try:
        with Image.open(path) as im:
            text = pytesseract.image_to_string(im, lang=lang, config=config)
    except FileNotFoundError as exc:
        raise DataError(f"image not found: {path}") from exc
    emit("images.ocr_image", chars=len(text))
    return text


def find_on_screen(template: str, confidence: Optional[float] = 0.9, region: Optional[Tuple[int, int, int, int]] = None,
                   grayscale: bool = True) -> Optional[Tuple[int, int, int, int]]:
    """Locate `template` (an image file) on the screen: ``(left, top, width, height)`` or None. Needs an interactive desktop."""
    try:
        import pyautogui  # type: ignore
    except ImportError as exc:
        raise NotAvailable("find_on_screen needs `pip install pyautogui pillow` (and opencv-python for `confidence`)") from exc
    try:
        kw = {"grayscale": grayscale, "region": region}
        if confidence is not None:
            kw["confidence"] = confidence
        box = pyautogui.locateOnScreen(template, **kw)
    except pyautogui.ImageNotFoundException:
        box = None
    except NotImplementedError as exc:
        raise NotAvailable("pyautogui could not use `confidence`: install opencv-python") from exc
    emit("images.find_on_screen", found=box is not None)
    return None if box is None else (box.left, box.top, box.width, box.height)
