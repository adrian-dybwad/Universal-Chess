"""Tests for the shared figurine-aware move-string renderer.

Why these tests exist
---------------------
Both the analysis move list and the hint alert draw moves in the selected
notation, compositing piece sprites for figurine glyphs (the e-paper font has
none). This pins the width math used for centering and the two draw paths
(sprite present vs. letter fallback) so a regression -- a wrong advance that
mis-centers the hint, or a crash/blank when the glyph can't be drawn -- is
caught here rather than as garbled text on the panel.
"""

from PIL import Image, ImageDraw, ImageFont

from universalchess.epaper import move_render

FONT = ImageFont.load_default()
KNIGHT = "\u2658"  # figurine (white) knight glyph produced by the formatter


def _canvas():
    img = Image.new("1", (128, 32), 255)
    return img, ImageDraw.Draw(img)


def test_measure_plain_text_is_text_width():
    # A move with no glyphs (e.g. UCI/SAN pawn move) measures as plain text width.
    _, draw = _canvas()
    assert move_render.measure_move_string(draw, "e4", FONT, 16) == int(
        draw.textlength("e4", font=FONT)
    )


def test_measure_counts_glyph_as_sprite_advance():
    # A figurine glyph advances glyph_size+1; the trailing square text adds its
    # own width. A wrong glyph advance would mis-center the hint.
    _, draw = _canvas()
    glyph_size = 16
    expected = glyph_size + 1 + int(draw.textlength("f3", font=FONT))
    assert move_render.measure_move_string(draw, f"{KNIGHT}f3", FONT, glyph_size) == expected


def test_draw_with_sheet_advances_by_measured_width_and_inks():
    # With a sprite sheet the glyph is composited: the end-x must equal
    # start + measured width (so centering is exact) and the black knight slot
    # paints ink onto the canvas.
    img, draw = _canvas()
    sheet = Image.new("1", (208, 16), 255)
    ImageDraw.Draw(sheet).rectangle([48, 0, 63, 15], fill=0)  # black in the "N" slot
    glyph_size = 16
    start = 4

    end = move_render.draw_move_string(
        img, draw, start, 8, f"{KNIGHT}f3", True, FONT, glyph_size, sheet
    )

    assert end == start + move_render.measure_move_string(draw, f"{KNIGHT}f3", FONT, glyph_size)
    assert img.getextrema() == (0, 255)  # something was drawn


def test_draw_without_sheet_falls_back_to_letters():
    # No sheet -> the piece letter is drawn instead of the sprite so the move is
    # still legible; it must not raise and must ink the canvas.
    img, draw = _canvas()
    end = move_render.draw_move_string(img, draw, 4, 8, f"{KNIGHT}f3", True, FONT, 16, None)
    assert end > 4
    assert img.getextrema() == (0, 255)
