"""Tests that TextWidget does not clip the first glyph of left-justified text.

Why these tests exist
---------------------
The bundled Font.ttc has glyphs with a negative left side bearing (their leftmost
ink column sits at x = -1). ``TextWidget`` rendered left-justified text flush at
``x = 0`` inside a sprite exactly ``width`` px wide, so that x = -1 column fell
outside the raster and was clipped at draw time. On the 1:1 128px e-paper panel
this shaved the left edge of wide glyphs (e.g. "White" -> "/Vhite",
"Stockfish" -> missing left of "S"), which is what the chess-clock labels and
player/engine names showed.

The fix shifts left-justified text right by any negative left side bearing so the
first glyph's leftmost column stays inside the sprite, without changing glyphs
that already have a non-negative bearing.
"""

import pathlib

from PIL import Image, ImageDraw, ImageFont

from universalchess.epaper.text import TextWidget, Justify

FONT_PATH = str(pathlib.Path(__file__).resolve().parents[1] / "resources" / "Font.ttc")


def _ink_count(sprite: Image.Image) -> int:
    """Count black (ink) pixels in a 1-bit sprite."""
    return sum(1 for pixel in sprite.getdata() if pixel == 0)


def _has_negative_left_bearing(text: str, font: ImageFont.FreeTypeFont) -> bool:
    """Whether the first glyph's leftmost ink column falls left of x = 0."""
    probe = ImageDraw.Draw(Image.new("1", (1, 1), 255))
    return probe.textbbox((0, 0), text, font=font)[0] < 0


def test_left_justified_first_glyph_not_clipped():
    # Why: guards the reported clipping. "White" has left bearing -1 in Font.ttc,
    # so drawing flush at x=0 loses its first ink column. Comparing the widget's
    # rendered ink to a reference drawn with the same bearing-corrected offset
    # detects the regression: without the shift the widget sprite has fewer ink
    # pixels (the clipped column) and the counts differ.
    font = ImageFont.truetype(FONT_PATH, 10)
    text = "White"
    assert _has_negative_left_bearing(text, font), (
        "Test precondition: Font.ttc must render this glyph with a negative left "
        "side bearing, otherwise it cannot exercise the clipping path."
    )

    width, height = 40, 16
    widget = TextWidget(0, 0, width, height, lambda **_: None,
                        text=text, font=font, justify=Justify.LEFT,
                        transparent=True)
    rendered = widget._get_sprite(0)

    # Reference: the full glyph drawn shifted right by the negative bearing so no
    # column falls outside the raster. This is the exact pixel set the widget
    # must preserve once the bearing is compensated.
    left_bearing = ImageDraw.Draw(Image.new("1", (1, 1), 255)).textbbox(
        (0, 0), text, font=font)[0]
    reference = Image.new("1", (width, height), 255)
    ImageDraw.Draw(reference).text((-left_bearing, -1), text, font=font, fill=0)

    assert _ink_count(rendered) == _ink_count(reference)


def test_left_justified_mask_first_glyph_not_clipped():
    # Why: transparent widgets composite via the text mask, a separate render
    # path from the sprite. The same bearing clip must not drop the first column
    # from the mask, or transparent text (all clock labels/names are transparent)
    # would still show the shave even if the sprite were fixed.
    font = ImageFont.truetype(FONT_PATH, 10)
    text = "White"

    width, height = 40, 16
    widget = TextWidget(0, 0, width, height, lambda **_: None,
                        text=text, font=font, justify=Justify.LEFT,
                        transparent=True)
    mask = widget.get_mask()

    left_bearing = ImageDraw.Draw(Image.new("1", (1, 1), 255)).textbbox(
        (0, 0), text, font=font)[0]
    reference = Image.new("1", (width, height), 0)
    ImageDraw.Draw(reference).text((-left_bearing, -1), text, font=font, fill=255)

    # Mask marks text pixels white (opaque); count those.
    widget_opaque = sum(1 for pixel in mask.getdata() if pixel != 0)
    reference_opaque = sum(1 for pixel in reference.getdata() if pixel != 0)
    assert widget_opaque == reference_opaque


def test_non_negative_bearing_position_unchanged():
    # Why: the fix must be surgical. Glyphs whose bearing is >= 0 already have
    # natural left padding; shifting them would change existing layouts. This
    # pins that such text keeps its flush-left x=0 origin (no regression).
    font = ImageFont.truetype(FONT_PATH, 10)
    text = "Black"  # 'B' has a non-negative (zero) left bearing in Font.ttc
    probe = ImageDraw.Draw(Image.new("1", (1, 1), 255))
    assert probe.textbbox((0, 0), text, font=font)[0] >= 0

    widget = TextWidget(0, 0, 40, 16, lambda **_: None,
                        text=text, font=font, justify=Justify.LEFT)
    draw = ImageDraw.Draw(Image.new("1", (1, 1), 255))
    assert widget._get_x_position(text, draw) == 0
