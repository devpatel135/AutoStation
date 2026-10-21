"""The AUTOSTATION train icon, generated from pixel art (no binary blob needed at runtime).

Pure Python, stdlib only, any OS: ``train_rgba(size)`` -> rows of (r, g, b, a); ``dib_for(size)`` -> the DIB bytes Windows'
CreateIconFromResourceEx wants; ``ico_bytes()`` -> a complete multi-size .ico (16/32/48). ``python -m wrapper_core._trayicon
--write wrapper_core/assets/train.ico [--png preview.png]`` regenerates the committed asset.

The art is a front view of an electric train: light-blue windscreen, yellow headlights and a red stripe on a blue body with a
dark outline, so it stays readable on both light and dark taskbars.
"""
from __future__ import annotations

import struct
import sys
import zlib
from typing import List, Tuple

SIZES = (16, 32, 48)
BASE = 16

PALETTE = {
    ".": (0, 0, 0, 0),
    "K": (20, 33, 61, 255),      # outline (dark navy)
    "B": (47, 127, 211, 255),    # body blue
    "C": (170, 228, 255, 255),   # windscreen
    "Y": (255, 214, 64, 255),    # headlights
    "R": (232, 64, 64, 255),     # stripe
    "G": (140, 150, 165, 255),   # bogie / rail grey
    "W": (255, 255, 255, 255),   # glint
}

ART = (
    "................",
    ".....KKKKKK.....",
    "....KKBBBBKK....",
    "...KBBBBBBBBK...",
    "...KBCCCCCCBK...",
    "...KBCWCCCCBK...",
    "...KBCCCCCCBK...",
    "...KBRRRRRRBK...",
    "...KBBBBBBBBK...",
    "...KBYBBBBYBK...",
    "..KKBBBBBBBBKK..",
    "..KGKKKKKKKKGK..",
    "...KKGGGGGGKK...",
    "..GGGGGGGGGGGG..",
    "................",
    "................",
)

Pixel = Tuple[int, int, int, int]


def _check_art() -> None:
    assert len(ART) == BASE and all(len(r) == BASE for r in ART), "train art must be 16x16"
    assert set("".join(ART)) <= set(PALETTE), "unknown palette key in the art"


_check_art()


def train_rgba(size: int) -> List[List[Pixel]]:
    """Rows (top to bottom) of RGBA pixels at ``size`` (a multiple of 16 keeps the pixel art crisp)."""
    if size < BASE or size > 256:
        raise ValueError("size must be 16..256")
    return [[PALETTE[ART[y * BASE // size][x * BASE // size]] for x in range(size)] for y in range(size)]


def dib_for(size: int) -> bytes:
    """BITMAPINFOHEADER + 32bpp BGRA pixels (bottom-up) + 1bpp AND mask: the payload of one .ico image."""
    rows = train_rgba(size)
    pixels = bytearray()
    for row in reversed(rows):
        for r, g, b, a in row:
            pixels += bytes((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4
    mask = bytearray()
    for row in reversed(rows):
        bits = bytearray(mask_stride)
        for x, (_r, _g, _b, a) in enumerate(row):
            if a == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bits
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0)
    return header + bytes(pixels) + bytes(mask)


def ico_bytes(sizes=SIZES) -> bytes:
    images = [dib_for(s) for s in sizes]
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for s, img in zip(sizes, images):
        out += struct.pack("<BBBBHHII", s if s < 256 else 0, s if s < 256 else 0, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    for img in images:
        out += img
    return bytes(out)


def best_size(wanted: int) -> int:
    """The smallest provided size >= ``wanted`` (the system then scales it down), else the largest."""
    for s in SIZES:
        if s >= wanted:
            return s
    return SIZES[-1]


def png_bytes(size: int = 32, scale: int = 8) -> bytes:
    """A PNG preview (nearest-neighbour upscaled, on a mid-grey background) for eyeballing the art."""
    rows = train_rgba(size)
    bg = (128, 128, 128)
    raw = bytearray()
    for row in rows:
        line = bytearray()
        for r, g, b, a in row:
            px = bytes(((r * a + bg[0] * (255 - a)) // 255, (g * a + bg[1] * (255 - a)) // 255, (b * a + bg[2] * (255 - a)) // 255))
            line += px * scale
        raw += (b"\x00" + bytes(line)) * scale

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    w = size * scale
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, w, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m wrapper_core._trayicon")
    ap.add_argument("--write", metavar="ICO")
    ap.add_argument("--png", metavar="PNG")
    a = ap.parse_args(argv)
    if a.write:
        with open(a.write, "wb") as fh:
            fh.write(ico_bytes())
    if a.png:
        with open(a.png, "wb") as fh:
            fh.write(png_bytes())
    return 0


if __name__ == "__main__":
    sys.exit(main())
