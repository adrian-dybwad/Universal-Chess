"""Figurine-aware rendering of a chess move string on the e-paper display.

The bundled e-paper font has no figurine piece glyphs (U+2654..2658), so any
widget that shows a move in figurine notation composites the board's piece
sprites in place of the glyphs. This module centralizes that layout so the
move list and the hint alert render figurine identically. Non-figurine
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
    """Build (and scale) the figurine glyph for ``letter`` from the sheet.

    LEGACY/SPLIT: crop row 0, which is black-on-white glyph art (LEGACY light-
    square piece, SPLIT ink), directly usable as a figurine. The column is
    resolved from the layout (LEGACY reserves column 0 for the empty square).
    COLORWAY: the sheet has no black-on-white row, so compose the glyph in code
    (white tile + alpha/ink), giving a hollow white-piece / filled black-piece
    figurine consistent with the board.
    """
    if sheet is None:
        return None
    from .chess_board import (
        TILE, detect_sheet_layout, image_has_alpha, composite_piece,
    )

    layout = detect_sheet_layout(sheet.width, sheet.height,
                                 has_alpha=image_has_alpha(sheet))
    if layout.is_colorway:
        glyph = Image.new("1", (TILE, TILE), 255)  # 255 == white
        if not composite_piece(glyph, 0, 0, TILE, sheet, layout, letter):
            return None
    else:
        x = layout.piece_column_x(letter)
        if x < 0 or x + TILE > sheet.width:
            return None
        glyph = sheet.crop((x, 0, x + TILE, TILE))
    if size != TILE:
        glyph = glyph.resize((size, size), Image.NEAREST)
    return glyph


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
                     font, glyph_size: int, sheet, fill: int = 0) -> int:
    """Draw ``text`` at ``(x, y)``, compositing piece sprites for figurine glyphs.

    White art is used for a white move, black art for a black move. When no
    sprite sheet is available the piece letter is drawn instead so the move stays
    legible rather than dropping the piece. Returns the x after the drawn string.

    ``fill`` is the text color (0=black default, 255=white). White is used to draw
    a move inverted on a black-filled cell (the selected-move highlight); the piece
    sprite is inverted to match so its black-on-white art becomes white-on-black and
    blends into the filled cell rather than punching a white box into it.
    """
    run = ""

    def flush(cur_x: int) -> int:
        nonlocal run
        if run:
            draw.text((cur_x, y), run, font=font, fill=fill)
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
            if fill == 255:
                # Invert the glyph so white piece art sits on a black background,
                # matching the inverted (selected) cell instead of overwriting it
                # with the sprite's white background.
                img = Image.eval(img.convert("L"), lambda p: 255 - p)
            sprite.paste(img, (int(x), int(y)))
            x += glyph_size + 1
        else:
            fallback = letter.upper()
            draw.text((x, y), fallback, font=font, fill=fill)
            x += int(draw.textlength(fallback, font=font)) + 1
    return flush(x)
