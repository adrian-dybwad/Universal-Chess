"""Overflow handling for TextWidget on the 128px e-paper panel.

Why these tests exist
---------------------
Localized strings are longer than the English they replace. At the game-over
winner's designed 16px, "Les blancs gagnent" is 135px and "Ganan las blancas"
is 129px, so centered CLIP drawing starts at a negative x and loses glyphs on
both edges. wrapText=True on an 18px-tall slot (one line of font_size+2) wraps
to two lines and then silently drops the second, so the user sees "Les blancs".

Overflow.FIT must keep every glyph on the panel: wrap when the slot is tall
enough for the wrapped lines, otherwise shrink the font until one line fits.

How a regression manifests
--------------------------
CLIP's x origin is negative, FIT's drawn lines are wider than the widget, or
FIT shrinks a headline that had room to wrap. The French winner string is the
case that showed on device.
"""

import pathlib

import pytest
from PIL import Image, ImageDraw

from universalchess.epaper.text import Justify, Overflow, TextWidget
from universalchess.resources import (
    ResourceLoader,
    get_resource_loader,
    set_resource_loader,
)

RES_DIR = str(pathlib.Path(__file__).resolve().parents[1] / "resources")
FRENCH_WHITE_WINS = "Les blancs gagnent"
SPANISH_WHITE_WINS = "Ganan las blancas"
PANEL_WIDTH = 128


@pytest.fixture
def bundled_fonts():
    """TrueType measurements; PIL's default bitmap font ignores font_size."""
    previous = get_resource_loader()
    set_resource_loader(ResourceLoader(RES_DIR))
    yield
    set_resource_loader(previous)


def _widget(text, *, width=PANEL_WIDTH, height=18, font_size=16,
            overflow=Overflow.CLIP, wrap=False, min_font_size=8):
    return TextWidget(
        0, 0, width, height, lambda **_: None,
        text=text, font_size=font_size, overflow=overflow,
        wrapText=wrap, min_font_size=min_font_size,
        justify=Justify.CENTER, transparent=True,
    )


def test_clip_starts_past_the_left_edge_on_french_winner(bundled_fonts):
    """CLIP of a 135px string in 128px starts at a negative x.

    Why: that negative origin is how glyphs are lost on both edges. FIT must
    not do the same. Failure: CLIP's x is still >= 0 (the string no longer
    overflows, so the test is not measuring clipping) or FIT's x is negative.
    """
    draw = ImageDraw.Draw(Image.new("1", (1, 1)))
    clipped = _widget(FRENCH_WHITE_WINS, overflow=Overflow.CLIP)
    assert clipped._get_x_position(FRENCH_WHITE_WINS, draw) < 0
    fitted = _widget(FRENCH_WHITE_WINS, overflow=Overflow.FIT)
    fitted._ensure_fitted()
    line = fitted.wrap_lines()[0]
    assert fitted._get_x_position(line, draw) >= 0


def test_fit_in_one_line_slot_shrinks_until_the_line_fits(bundled_fonts):
    """An 18px slot cannot hold two wrapped 16px lines, so FIT must shrink.

    Why: wrapText=True here would drop "gagnent". Failure: fitted_font_size stays
    16, or the one drawn line is still wider than 128px.
    """
    widget = _widget(FRENCH_WHITE_WINS, height=18, overflow=Overflow.FIT)
    assert widget.fitted_font_size < 16
    assert widget.fitted_font_size >= widget.min_font_size
    draw = ImageDraw.Draw(Image.new("1", (1, 1)))
    width = int(draw.textlength(FRENCH_WHITE_WINS, font=widget._font))
    assert width <= PANEL_WIDTH


def test_fit_in_two_line_slot_wraps_at_designed_size(bundled_fonts):
    """A 36px slot can hold two 16px lines, so FIT wraps rather than shrinking.

    Why: shrinking a headline when there is room looks like a different widget.
    Failure: fitted_font_size drops below 16, or wrap_lines stays one line so
    the overflow is still clipped.
    """
    widget = _widget(FRENCH_WHITE_WINS, height=36, overflow=Overflow.FIT)
    assert widget.fitted_font_size == 16
    lines = widget.wrap_lines()
    assert len(lines) == 2
    draw = ImageDraw.Draw(Image.new("1", (1, 1)))
    for line in lines:
        assert int(draw.textlength(line, font=widget._font)) <= PANEL_WIDTH


def test_fit_leaves_short_english_headline_unchanged(bundled_fonts):
    """"White wins" already fits at 16px; FIT must not wrap or shrink it.

    Why: a FIT that always wraps would split "White wins" and a FIT that always
    shrinks would make English smaller than the designed headline. Failure:
    fitted_font_size != 16 or wrap_lines has more than one line.
    """
    widget = _widget("White wins", height=18, overflow=Overflow.FIT)
    assert widget.fitted_font_size == 16
    assert widget.wrap_lines() == ["White wins"]


def test_spanish_winner_fits_under_fit(bundled_fonts):
    """Spanish is 1px over at 16px; FIT must still keep it on the panel.

    Why: the 1px case is easy to miss with a wrap-only policy (one word may
    still overflow after wrap). Failure: a drawn line is wider than 128px.
    """
    widget = _widget(SPANISH_WHITE_WINS, height=18, overflow=Overflow.FIT)
    draw = ImageDraw.Draw(Image.new("1", (1, 1)))
    for line in widget.wrap_lines():
        assert int(draw.textlength(line, font=widget._font)) <= PANEL_WIDTH


def test_shrink_does_not_wrap_even_when_height_allows(bundled_fonts):
    """Overflow.SHRINK is for one-line slots that must not become two lines.

    Why: a clock time or a one-line title that wrapped would collide with the
    row below. Failure: wrap_lines returns two lines, or the font never drops.
    """
    widget = _widget(FRENCH_WHITE_WINS, height=36, overflow=Overflow.SHRINK)
    assert widget.fitted_font_size < 16
    assert widget.wrap_lines() == [FRENCH_WHITE_WINS]


def test_wrap_text_true_still_wraps_without_shrinking(bundled_fonts):
    """Existing wrapText=True callers (splash, coach) must keep wrapping at the
    designed size. Failure: FIT-like shrinking, or a single unwrapped line.
    """
    widget = _widget(FRENCH_WHITE_WINS, height=36, wrap=True)
    assert widget.fitted_font_size == 16
    assert len(widget.wrap_lines()) == 2
