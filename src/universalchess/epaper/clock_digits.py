"""Seven-segment bitmap digits for the e-paper chess clock.

The sheet ``clock_digits.bmp`` is a 1-bit image of 208×24 px: thirteen 16×24
cells. Cells 0–9 are digits; cell 10 is the colon. When drawing, each cell is
trimmed to the left 12 columns and glyphs advance with a 2 px gap (12+2),
matching the Centaur CLOCK_FONT layout so times stay fixed-pitch and compact.

Only strings of digits and colons are drawable. ``FLAG`` and the long-form
remainders from :func:`universalchess.clock_format.format_clock_time` stay on
TrueType via the clock widget's TextWidget fallback.
"""

from __future__ import annotations

from typing import Optional

from PIL import Image

CELL_WIDTH = 16
CELL_HEIGHT = 24
GLYPH_WIDTH = 12
GAP_PX = 2
CELL_COUNT = 13
SHEET_WIDTH = CELL_COUNT * CELL_WIDTH
SHEET_HEIGHT = CELL_HEIGHT

_CHAR_TO_INDEX = {str(d): d for d in range(10)}
_CHAR_TO_INDEX[":"] = 10

# Cached sheet from ResourceLoader (installed once at first use).
_sheet: Optional[Image.Image] = None


def is_digit_clock_string(text: str) -> bool:
    """True when every character is a digit or colon (non-empty)."""
    if not text:
        return False
    return all(ch in _CHAR_TO_INDEX for ch in text)


def glyph_for_index(sheet: Image.Image, index: int) -> Image.Image:
    """Left 12×24 crop of cell ``index`` (0–12)."""
    if index < 0 or index >= CELL_COUNT:
        raise ValueError(f"clock digit cell index out of range: {index}")
    x0 = index * CELL_WIDTH
    return sheet.crop((x0, 0, x0 + GLYPH_WIDTH, CELL_HEIGHT)).convert("1")


def glyph_for_char(sheet: Image.Image, char: str) -> Image.Image:
    """Trimmed glyph for a single digit or colon character."""
    try:
        index = _CHAR_TO_INDEX[char]
    except KeyError as exc:
        raise ValueError(f"not a clock digit/colon character: {char!r}") from exc
    return glyph_for_index(sheet, index)


def _scale_for_height(height: int) -> tuple[int, int, int]:
    """Glyph width, height, and advance for a target row height.

    At the native 24 px height the advance is 14 (12+2). Shorter targets scale
    width and gap by the same ratio so spacing stays proportional.
    """
    if height <= 0:
        raise ValueError(f"clock digit height must be positive: {height}")
    if height == CELL_HEIGHT:
        return GLYPH_WIDTH, CELL_HEIGHT, GLYPH_WIDTH + GAP_PX
    glyph_w = max(1, round(GLYPH_WIDTH * height / CELL_HEIGHT))
    gap = max(1, round(GAP_PX * height / CELL_HEIGHT))
    return glyph_w, height, glyph_w + gap


def measure(text: str, height: int = CELL_HEIGHT) -> int:
    """Pixel width of ``text`` when drawn at ``height`` (native 24 by default)."""
    if not text:
        return 0
    glyph_w, _, advance = _scale_for_height(height)
    n = len(text)
    return n * glyph_w + (n - 1) * (advance - glyph_w)


def draw(
    sprite: Image.Image,
    x: int,
    y: int,
    text: str,
    sheet: Image.Image,
    height: int = CELL_HEIGHT,
) -> int:
    """Paste seven-segment glyphs for ``text`` onto ``sprite``.

    Returns the pixel width used. Raises ``ValueError`` if ``text`` contains
    characters other than digits and colons.
    """
    if not is_digit_clock_string(text):
        raise ValueError(f"clock digits cannot draw {text!r}")

    glyph_w, glyph_h, advance = _scale_for_height(height)
    cursor = x
    for char in text:
        glyph = glyph_for_char(sheet, char)
        if glyph_h != CELL_HEIGHT or glyph_w != GLYPH_WIDTH:
            glyph = glyph.resize((glyph_w, glyph_h), Image.NEAREST)
        sprite.paste(glyph, (cursor, y))
        cursor += advance
    # Last glyph has no trailing gap in the measured width.
    return measure(text, height=height)


def load_sheet() -> Optional[Image.Image]:
    """Load and cache ``clock_digits.bmp`` via the app ResourceLoader.

    Returns None when no loader is registered or the file is missing (tests
    without bundled resources, early startup). Callers fall back to TrueType.
    """
    global _sheet
    if _sheet is not None:
        return _sheet
    from universalchess.resources import get_resource_loader

    loader = get_resource_loader()
    if loader is None:
        return None
    image = loader.get_image("clock_digits.bmp")
    if image is None:
        return None
    _sheet = image.convert("1")
    return _sheet


def reset_sheet_cache() -> None:
    """Drop the cached sheet (tests that swap ResourceLoader)."""
    global _sheet
    _sheet = None
