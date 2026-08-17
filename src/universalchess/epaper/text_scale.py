"""Text-size scaling for e-paper widgets.

A single Display > Text Size setting (``small`` / ``medium`` / ``large``) scales
the font sizes of widgets that opt in: the coach statement panel, the move
list, game-over and setup status, the chess clock labels, the help dialog,
info overlays, and icon menus (which also raise their minimum row height so
Large can use extra space on the buttons).

The mapping is kept here as a pure lookup so the board renderer and any other
caller resolve a size name to the exact same pixel sizes, and so the single
source of truth for the scale factors lives in one place. ``medium`` is the
identity factor, so existing (unscaled) layouts are unchanged.
"""

TEXT_SIZES = ("small", "medium", "large")
DEFAULT_TEXT_SIZE = "medium"

# Multiplicative factor applied to a widget's base (``medium``) font size.
# Chosen as a clear but modest step on the 128px panel: small is noticeably
# tighter, large fits fewer lines/rows (paging absorbs the difference).
_SCALE = {"small": 0.8, "medium": 1.0, "large": 1.25}

__all__ = [
    "TEXT_SIZES",
    "DEFAULT_TEXT_SIZE",
    "normalize_text_size",
    "scale_font",
    "read_text_size",
]


def normalize_text_size(value: str) -> str:
    """Return a valid text-size name, defaulting unknown/blank input to medium.

    Matching is case-insensitive and whitespace-tolerant so values coming from
    config files or the web form ("Large", " small ") resolve correctly.
    """
    normalized = (value or "").strip().lower()
    return normalized if normalized in _SCALE else DEFAULT_TEXT_SIZE


def scale_font(base_size: int, text_size: str) -> int:
    """Scale ``base_size`` (the medium size) by the factor for ``text_size``.

    The result is rounded to the nearest pixel and clamped to at least 1 so a
    tiny base size can never scale to a non-positive, unrenderable font size.
    Unknown ``text_size`` values fall back to medium (identity), returning
    ``base_size`` unchanged.
    """
    factor = _SCALE[normalize_text_size(text_size)]
    return max(1, round(base_size * factor))


def read_text_size() -> str:
    """Return the persisted Display > Text Size, normalized to a valid name.

    Read live so a menu change takes effect on the next widget that is built
    (the next menu screen, the next game-widget rebuild). Unknown or missing
    values become ``medium``.
    """
    from universalchess.board.settings import Settings

    return normalize_text_size(
        Settings.read("game", "text_size", DEFAULT_TEXT_SIZE)
    )
