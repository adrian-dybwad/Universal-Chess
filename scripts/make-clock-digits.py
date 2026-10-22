#!/usr/bin/env python3
"""Generate the seven-segment ``clock_digits.bmp`` sheet for the e-paper clock.

Layout matches the Centaur CLOCK_FONT contract used by
``universalchess.epaper.clock_digits``:

- 208×24 px, 1-bit, white background / black ink
- Thirteen 16×24 cells; ink lives in the left 12 columns (cells are trimmed
  to 12 px when drawn, with a 2 px gap between glyphs)
- Cells 0–9 = digits, cell 10 = colon; cells 11–12 reserved blank

Usage:
    python scripts/make-clock-digits.py
    python scripts/make-clock-digits.py --output /tmp/clock_digits.bmp
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

CELL_W = 16
CELL_H = 24
GLYPH_W = 12
CELLS = 13
SHEET_W = CELLS * CELL_W
SHEET_H = CELL_H

# Standard seven-segment bitmasks: a=top, b=UR, c=LR, d=bottom, e=LL, f=UL, g=mid
_SEGMENTS = {
    0: "abcdef",
    1: "bc",
    2: "abdeg",
    3: "abcdg",
    4: "bcfg",
    5: "acdfg",
    6: "acdefg",
    7: "abc",
    8: "abcdefg",
    9: "abcdfg",
}

# Horizontal/vertical segment rectangles inside the 12×24 ink area (inclusive).
# Thickness 2 keeps digits readable at native size and after nearest-neighbour
# downscale for the compact clock section.
_H = {
    "a": (2, 1, 9, 2),
    "g": (2, 11, 9, 12),
    "d": (2, 21, 9, 22),
}
_V = {
    "f": (1, 3, 2, 10),
    "b": (9, 3, 10, 10),
    "e": (1, 13, 2, 20),
    "c": (9, 13, 10, 20),
}


def _draw_digit(draw: ImageDraw.ImageDraw, origin_x: int, digit: int) -> None:
    for seg in _SEGMENTS[digit]:
        if seg in _H:
            x0, y0, x1, y1 = _H[seg]
        else:
            x0, y0, x1, y1 = _V[seg]
        draw.rectangle(
            (origin_x + x0, y0, origin_x + x1, y1),
            fill=0,
        )


def _draw_colon(draw: ImageDraw.ImageDraw, origin_x: int) -> None:
    # Two 2×2 blocks, vertically centred in the 12×24 ink area.
    for y0, y1 in ((7, 8), (15, 16)):
        draw.rectangle((origin_x + 5, y0, origin_x + 6, y1), fill=0)


def build_sheet() -> Image.Image:
    """Return a new 1-bit sheet with digits 0–9 and a colon."""
    sheet = Image.new("1", (SHEET_W, SHEET_H), 255)
    draw = ImageDraw.Draw(sheet)
    for digit in range(10):
        _draw_digit(draw, digit * CELL_W, digit)
    _draw_colon(draw, 10 * CELL_W)
    return sheet


def main() -> None:
    default_out = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "universalchess"
        / "resources"
        / "clock_digits.bmp"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=default_out,
        help=f"Output BMP path (default: {default_out})",
    )
    args = parser.parse_args()
    sheet = build_sheet()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.output, format="BMP")
    print(f"Wrote {args.output} ({sheet.size[0]}x{sheet.size[1]}, mode {sheet.mode})")


if __name__ == "__main__":
    main()
