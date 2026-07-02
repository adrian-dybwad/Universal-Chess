"""Figurine-aware rendering of a chess move string on the e-paper display.

The bundled e-paper font has no figurine piece glyphs (U+2654..2658), so any
widget that shows a move in figurine notation composites the board's piece
sprites in place of the glyphs. This module centralizes that layout so the
analysis move list and the hint alert render figurine identically. Non-figurine
notations (SAN/LAN/UCI) contain no glyphs and draw as a single text run.
"""

from typing import Optional

from PIL import Image

# Figurine glyph -> piece letter, to swap the notation formatter's glyph for the
# matching piece sprite when drawing on the board.
_FIGURINE_TO_LETTER = {
    "\u2654": "K",
    "\u2655": "Q",
    "\u2656": "R",
    "\u2657": "B",
    "\u2658": "N",
}

# Piece letter -> x offset in the 16px sprite sheet (uppercase = white art,
# lowercase = black art), mirroring ChessBoardWidget._piece_x.
_PIECE_SPRITE_X = {
    "P": 16, "R": 32, "N": 48, "B": 64, "Q": 80, "K": 96,
    "p": 112, "r": 128, "n": 144, "b": 160, "q": 176, "k": 192,
}


def sprite_sheet(explicit: Optional[Image.Image] = None) -> Optional[Image.Image]:
    """The piece sprite sheet to composite from.

    Uses ``explicit`` when provided (e.g. a widget given its own sheet), else
    falls back to the module-level sheet the app installs at startup. Returns
    None when no sheet is available, in which case callers fall back to letters.
    """
    if explicit is not None:
        return explicit
    from . import chess_board
    return chess_board._chess_sprites


def _piece_glyph_image(sheet, letter: str, size: int) -> Optional[Image.Image]:
    """Crop (and scale) the piece sprite for ``letter`` from the sheet."""
    if sheet is None:
        return None
    x = _PIECE_SPRITE_X.get(letter)
    if x is None:
        return None
    crop = sheet.crop((x, 0, x + 16, 16))
    if size != 16:
        crop = crop.resize((size, size), Image.NEAREST)
    return crop


def measure_move_string(draw, text: str, font, glyph_size: int) -> int:
    """Pixel width ``draw_move_string`` will occupy, for centering.

    Mirrors ``draw_move_string``'s advance: each figurine glyph advances
    ``glyph_size + 1`` (the sprite path) and text runs advance by their measured
    text width.
    """
    width = 0
    run = ""
    for ch in text:
        if ch in _FIGURINE_TO_LETTER:
            if run:
                width += int(draw.textlength(run, font=font))
                run = ""
            width += glyph_size + 1
        else:
            run += ch
    if run:
        width += int(draw.textlength(run, font=font))
    return width


def draw_move_string(sprite, draw, x: int, y: int, text: str, white_side: bool,
                     font, glyph_size: int, sheet) -> int:
    """Draw ``text`` at ``(x, y)``, compositing piece sprites for figurine glyphs.

    White art is used for a white move, black art for a black move. When no
    sprite sheet is available the piece letter is drawn instead so the move stays
    legible rather than dropping the piece. Returns the x after the drawn string.
    """
    run = ""

    def flush(cur_x: int) -> int:
        nonlocal run
        if run:
            draw.text((cur_x, y), run, font=font, fill=0)
            cur_x += int(draw.textlength(run, font=font))
            run = ""
        return cur_x

    for ch in text:
        letter = _FIGURINE_TO_LETTER.get(ch)
        if letter is None:
            run += ch
            continue
        x = flush(x)
        if not white_side:
            letter = letter.lower()
        img = _piece_glyph_image(sheet, letter, glyph_size)
        if img is not None:
            sprite.paste(img, (int(x), int(y)))
            x += glyph_size + 1
        else:
            fallback = letter.upper()
            draw.text((x, y), fallback, font=font, fill=0)
            x += int(draw.textlength(fallback, font=font)) + 1
    return flush(x)
