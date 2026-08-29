"""Centered TextWidget lines must sit on the widget midline, including wraps.

Why these tests exist
---------------------
``Overflow.FIT`` prefers a non-wrapping layout when each explicit line already
fits. That layout then painted the whole string in one ``draw.text()`` call.
PIL left-aligns newline-separated lines to the same x, so a short first line
("Loading") sat under the left edge of a longer second line ("Challenge...")
instead of on the widget center. Word-wrapped FIT already painted line by
line and looked centered; explicit ``\\n`` (splash waiting copy, game-started)
did not.

How a regression manifests
--------------------------
The shorter line's ink midpoint sits well left of the widget center, and its
left edge lines up with the longer line instead of sitting inward of it.
"""

import pathlib

import pytest
from PIL import Image

from universalchess.epaper.text import Justify, Overflow, TextWidget
from universalchess.resources import (
    ResourceLoader,
    get_resource_loader,
    set_resource_loader,
)

RES_DIR = str(pathlib.Path(__file__).resolve().parents[1] / "resources")

# Lichess waiting splash: first line is much shorter. FIT keeps wrap=False
# because both lines already fit, which is the paint path that left-aligned.
LOADING_CHALLENGE = "Loading\nChallenge..."
WIDGET_WIDTH = 120
FONT_SIZE = 18
LINE_HEIGHT = FONT_SIZE + 2


@pytest.fixture
def bundled_fonts():
    """TrueType measurements; PIL's default bitmap font ignores font_size."""
    previous = get_resource_loader()
    set_resource_loader(ResourceLoader(RES_DIR))
    yield
    set_resource_loader(previous)


def _widget(text, *, justify=Justify.CENTER, overflow=Overflow.FIT,
            width=WIDGET_WIDTH, height=LINE_HEIGHT * 2):
    return TextWidget(
        0, 0, width, height, lambda **_: None,
        text=text, font_size=FONT_SIZE, overflow=overflow,
        justify=justify, transparent=True,
    )


def _rendered(widget: TextWidget) -> Image.Image:
    """Sprite of ``widget`` via the public blit path (black text on white)."""
    sprite = Image.new("1", (widget.width, widget.height), 255)
    widget.draw_on(sprite, 0, 0, text_color=0)
    return sprite


def _ink_x_range(sprite: Image.Image, y0: int, y1: int):
    """Inclusive (left, right) columns of black ink in the y band, or None."""
    pixels = sprite.load()
    height = sprite.size[1]
    width = sprite.size[0]
    xs = [
        x
        for y in range(max(0, y0), min(height, y1))
        for x in range(width)
        if pixels[x, y] == 0
    ]
    if not xs:
        return None
    return min(xs), max(xs)


def _line_ink(sprite: Image.Image, line_index: int, line_height: int = LINE_HEIGHT):
    return _ink_x_range(
        sprite, line_index * line_height, (line_index + 1) * line_height
    )


@pytest.mark.usefixtures("bundled_fonts")
def test_centered_explicit_newlines_center_each_line():
    """A short line above a longer one must each sit on the widget midline.

    Why: "Loading\\nChallenge..." is FIT with wrap=False. Painting the block as
    one string left-aligns "Loading" to "Challenge...". Failure: line 0's ink
    midpoint is more than 2px off center, or its left edge matches the longer
    line (block alignment) instead of sitting inward of it.
    """
    widget = _widget(LOADING_CHALLENGE)
    assert widget.fitted_wrap is False
    sprite = _rendered(widget)

    first = _line_ink(sprite, 0)
    second = _line_ink(sprite, 1)
    assert first is not None
    assert second is not None

    center = WIDGET_WIDTH / 2
    for left, right in (first, second):
        midpoint = (left + right) / 2
        assert abs(midpoint - center) <= 2

    # "Loading" is the shorter line: its left ink must sit inward of the longer
    # line, not share an edge with it.
    assert first[0] > second[0] + 5


@pytest.mark.usefixtures("bundled_fonts")
def test_centered_word_wrapped_lines_stay_on_the_midline():
    """Word-wrapped FIT already painted per line; that path must stay centered.

    Why: a paint-path unification that only fixed explicit newlines could still
    break WRAP. Failure: a wrapped line's ink midpoint drifts more than 2px
    off the widget center.
    """
    widget = TextWidget(
        0, 0, 128, 36, lambda **_: None,
        text="Les blancs gagnent", font_size=16,
        overflow=Overflow.FIT, justify=Justify.CENTER, transparent=True,
    )
    assert widget.fitted_wrap is True
    assert len(widget.wrap_lines()) == 2
    sprite = _rendered(widget)
    winner_line_height = widget.fitted_font_size + 2
    center = 128 / 2
    for index in range(2):
        band = _line_ink(sprite, index, winner_line_height)
        assert band is not None
        midpoint = (band[0] + band[1]) / 2
        assert abs(midpoint - center) <= 2


@pytest.mark.usefixtures("bundled_fonts")
def test_left_justified_explicit_newlines_share_a_left_edge():
    """LEFT must still stack lines on one origin; centering is per-justify.

    Why: painting line-by-line for CENTER must not start independently
    centering LEFT text. Failure: the two lines' left ink columns differ by
    more than the 1px left-bearing compensation.
    """
    widget = _widget(LOADING_CHALLENGE, justify=Justify.LEFT)
    sprite = _rendered(widget)
    first = _line_ink(sprite, 0)
    second = _line_ink(sprite, 1)
    assert first is not None
    assert second is not None
    assert abs(first[0] - second[0]) <= 1
