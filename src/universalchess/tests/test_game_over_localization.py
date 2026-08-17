"""Locale-swap test for the game-over widget's result/termination text.

Guards that the widget renders its winner line and termination reason through the
i18n localizer rather than hardcoded English, so a Spanish device shows Spanish
end-of-game text. The widget is built with a stub game state (no real observers)
and its result is updated directly; only the derived strings are asserted.

Each test states the regression it guards and how it would surface.
"""

from unittest.mock import MagicMock

import pytest

from universalchess import i18n
from universalchess.epaper.game_over import GameOverWidget


@pytest.fixture(autouse=True)
def reset_i18n(monkeypatch):
    """Reset the i18n locale cache around each test and default to English."""
    i18n._active_locale = None
    i18n._bundles.clear()
    monkeypatch.setattr(
        "universalchess.services.language_service.get_language", lambda: "en"
    )
    yield
    i18n._active_locale = None
    i18n._bundles.clear()


def _widget():
    """A GameOverWidget wired to a stub game state (no real subscriptions)."""
    return GameOverWidget(0, 144, 128, 72, update_callback=lambda *a, **k: None,
                          game_state=MagicMock())


def _set_locale(monkeypatch, code):
    monkeypatch.setattr(
        "universalchess.services.language_service.get_language", lambda: code
    )
    i18n.refresh_active_language()


def test_english_result_and_termination():
    """In English, a checkmate win renders the English winner/termination.

    Baseline for the locale swap below; a regression that broke the English path
    (e.g. wrong i18n keys) would surface here as the raw key or wrong text.
    """
    widget = _widget()
    widget.set_result(result="1-0", termination="CHECKMATE")
    assert widget.winner == "White wins"
    assert widget.termination == "Checkmate"


def test_spanish_result_and_termination(monkeypatch):
    """In Spanish, the same ending renders Spanish winner/termination.

    Why: the end-of-game screen must localize with the device. How a regression
    manifests: the widget keeps hardcoded English ("White wins"/"Checkmate")
    regardless of locale, so this assertion fails while the English test passes.
    """
    _set_locale(monkeypatch, "es")
    widget = _widget()
    widget.set_result(result="1-0", termination="CHECKMATE")
    assert widget.winner == "Ganan las blancas"
    assert widget.termination == "Jaque mate"


def test_french_result_and_termination(monkeypatch):
    """In French, the same ending renders French winner/termination.

    Why: French is a shipped UI locale; the end-of-game screen must localize
    with the device. How a regression manifests: the widget keeps hardcoded
    English regardless of locale, so this assertion fails while the English
    test passes.
    """
    _set_locale(monkeypatch, "fr")
    widget = _widget()
    widget.set_result(result="1-0", termination="CHECKMATE")
    assert widget.winner == "Les blancs gagnent"
    assert widget.termination == "Mat"


def test_german_result_and_termination(monkeypatch):
    """In German, the same ending renders German winner/termination.

    Why: German is the newest shipped UI locale, so it is the one a bundle gap
    would strand. How a regression manifests: the German bundle misses these
    keys and the widget falls back to English while the Spanish and French tests
    still pass.
    """
    _set_locale(monkeypatch, "de")
    widget = _widget()
    widget.set_result(result="1-0", termination="CHECKMATE")
    assert widget.winner == "Weiß gewinnt"
    assert widget.termination == "Schachmatt"
