"""Game-over winner text stays on the 128px panel, including French.

Why these tests exist
---------------------
The winner line is a 16px headline in a slot that was one line tall. French
"Les blancs gagnent" is 135px at that size, so CLIP clipped both ends. FIT
must wrap into a second line when the widget gives it height, and Display >
Text Size must scale the designed fonts.

How a regression manifests
--------------------------
French winner ink is less than English (clipped glyphs), or Large still
uses a 16px winner font.
"""

from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageDraw

from universalchess import i18n
from universalchess.epaper.game_over import GameOverWidget
from universalchess.epaper.text_scale import scale_font
from universalchess.resources import ResourceLoader, get_resource_loader, set_resource_loader
from universalchess.tests.test_text_overflow import RES_DIR

FRENCH_WHITE_WINS = "Les blancs gagnent"


@pytest.fixture
def bundled_fonts():
    previous = get_resource_loader()
    set_resource_loader(ResourceLoader(RES_DIR))
    yield
    set_resource_loader(previous)


@pytest.fixture(autouse=True)
def reset_i18n(monkeypatch):
    i18n._active_locale = None
    i18n._bundles.clear()
    monkeypatch.setattr(
        "universalchess.services.language_service.get_language", lambda: "en"
    )
    yield
    i18n._active_locale = None
    i18n._bundles.clear()


def _widget(text_size="medium"):
    return GameOverWidget(
        0, 144, 128, 72, update_callback=lambda *a, **k: None,
        game_state=MagicMock(), text_size=text_size,
    )


def _ink(sprite: Image.Image) -> int:
    return sum(1 for pixel in sprite.getdata() if pixel == 0)


def test_french_winner_is_fully_visible(bundled_fonts, monkeypatch):
    """French "Les blancs gagnent" must not lose glyphs to the 128px edge.

    Why: that string is the overflow that showed on device. Failure: a drawn
    winner line is wider than 128px, or the sprite has no more ink than a
    one-line CLIP of the same string would (the clipped-both-sides case).
    """
    monkeypatch.setattr(
        "universalchess.services.language_service.get_language", lambda: "fr"
    )
    i18n.refresh_active_language()

    widget = _widget()
    widget.set_result("1-0", "CHECKMATE")
    sprite = Image.new("1", (128, 72), 255)
    widget.render(sprite)

    draw = ImageDraw.Draw(Image.new("1", (1, 1)))
    for line in widget._winner_text.wrap_lines():
        assert int(draw.textlength(line, font=widget._winner_text._font)) <= 128

    assert _ink(sprite) > 0
    assert widget.winner == FRENCH_WHITE_WINS


def test_large_text_size_scales_winner_font():
    """Large must use the scaled headline size, not the medium 16px.

    Why: game-over ignored text_size, so Display > Text Size did nothing on
    the result screen. Failure: both widgets report font_size 16.
    """
    medium = _widget("medium")
    large = _widget("large")
    assert medium._winner_text.font_size == 16
    assert large._winner_text.font_size == scale_font(16, "large")
