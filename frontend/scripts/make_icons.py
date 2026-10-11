#!/usr/bin/env python3
"""Draws the app icons, so the page can be installed as a PWA.

The finished PNGs are committed next to the app; run this only when the artwork
changes:

    python3 frontend/scripts/make_icons.py

It deliberately uses nothing but the standard library: the machine that builds
the frontend has no image tooling, and the icons should be reproducible without
one. Each icon is a sky-blue tile with a white invoice sheet and a few lines of
"text", rendered with supersampling for clean edges.

A full-bleed variant (`maskable`, and the iOS touch icon, which iOS rounds
itself) is drawn without the rounded corners or transparency.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"

# The app's palette (see styles.css).
TOP = (56, 189, 248)     # --accent       #38bdf8
BOTTOM = (14, 165, 233)  # --accent-strong #0ea5e9
PAPER = (248, 250, 252)  # the invoice sheet
INK = (3, 105, 161)      # the lines of text on it

# How many samples per axis are averaged into each pixel.
SUPERSAMPLE = 3


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t), round(a[2] + (b[2] - a[2]) * t))


def _in_rounded_rect(
    x: float, y: float, x0: float, y0: float, x1: float, y1: float, radius: float
) -> bool:
    """True when (x, y) is inside the rectangle, corners rounded by `radius`.

    All coordinates share one unit; clamping the point into the inner rectangle
    turns the corner test into a single distance check.
    """
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + radius), x1 - radius)
    cy = min(max(y, y0 + radius), y1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def _sample(x: float, y: float, bleed: bool) -> tuple[int, int, int, int]:
    """The RGBA of one point, in a 0..1 design space."""
    # The tile itself. A maskable/apple icon bleeds to every edge; the plain
    # icon is a rounded square on transparency.
    if not _in_rounded_rect(x, y, 0.0, 0.0, 1.0, 1.0, 0.0 if bleed else 0.22):
        return (0, 0, 0, 0)
    color = _mix(TOP, BOTTOM, y)

    # The sheet of paper.
    if _in_rounded_rect(x, y, 0.30, 0.20, 0.70, 0.80, 0.05):
        color = PAPER
        # Rows of "text", short and long, then a bolder total at the foot.
        for row_y in (0.33, 0.43, 0.53):
            if _in_rounded_rect(x, y, 0.38, row_y, 0.62, row_y + 0.05, 0.02):
                color = INK
        if _in_rounded_rect(x, y, 0.38, 0.66, 0.56, 0.72, 0.03):
            color = BOTTOM
    return (*color, 255)


def _render(size: int, bleed: bool) -> bytes:
    """A size×size RGBA image, as raw bytes for write_png."""
    data = bytearray(size * size * 4)
    step = 1.0 / (size * SUPERSAMPLE)
    for py in range(size):
        for px in range(size):
            red = green = blue = alpha = 0.0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    x = (px * SUPERSAMPLE + sx + 0.5) * step
                    y = (py * SUPERSAMPLE + sy + 0.5) * step
                    r, g, b, a = _sample(x, y, bleed)
                    # Premultiplied so a partly covered edge pixel keeps its hue.
                    red += r * a
                    green += g * a
                    blue += b * a
                    alpha += a
            if alpha == 0:
                pixel = (0, 0, 0, 0)
            else:
                pixel = (
                    round(red / alpha),
                    round(green / alpha),
                    round(blue / alpha),
                    round(alpha / (SUPERSAMPLE * SUPERSAMPLE)),
                )
            offset = (py * size + px) * 4
            data[offset : offset + 4] = bytes(pixel)
    return bytes(data)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def write_png(path: Path, size: int, bleed: bool) -> None:
    rgba = _render(size, bleed)
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # filter type: none
        raw += rgba[y * stride : (y + 1) * stride]
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    print(f"wrote {path} ({size}×{size}, {'maskable' if bleed else 'rounded'})")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # The plain icons for the manifest, one maskable for Android's adaptive
    # shape, and the iOS touch icon the system rounds for us.
    write_png(OUT_DIR / "icon-192.png", 192, bleed=False)
    write_png(OUT_DIR / "icon-512.png", 512, bleed=False)
    write_png(OUT_DIR / "icon-maskable-512.png", 512, bleed=True)
    write_png(OUT_DIR / "apple-touch-icon.png", 180, bleed=True)


if __name__ == "__main__":
    main()
