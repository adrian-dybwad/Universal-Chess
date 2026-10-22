"""Locale-swap test for the analysis widget's move-quality word.

Guards that the quality bar renders Blunder / Mistake / Inaccuracy / Good /
Brilliant through the i18n localizer rather than the English classifier labels,
so a non-English device never shows "Inaccuracy" (or the other English tokens)
on the bar. The classifier keeps returning English tokens; only the display
path localizes.

Labels follow Lichess ``site.xml`` wording where it fits the 128px quality bar
with the accuracy percent beside it. Dutch uses the shorter adjective
``Onnauwkeurig`` because Lichess's noun ``Onnauwkeurigheid`` overflows that
row; Good/Brilliant use single-word forms so the bar stays readable.

Each test states the regression it guards and how it would surface.
"""

import pytest

from universalchess import i18n
from universalchess.epaper.game_analysis import localize_quality_word

# English classifier token -> expected label per shipped UI locale.
# en is the identity mapping (source tokens).
_QUALITY_BY_LOCALE = {
    "en": {
        "Blunder": "Blunder",
        "Mistake": "Mistake",
        "Inaccuracy": "Inaccuracy",
        "Good": "Good",
        "Brilliant": "Brilliant",
    },
    "fr": {
        "Blunder": "Gaffe",
        "Mistake": "Erreur",
        "Inaccuracy": "Imprécision",
        "Good": "Bon",
        "Brilliant": "Brillant",
    },
    "de": {
        "Blunder": "Patzer",
        "Mistake": "Fehler",
        "Inaccuracy": "Ungenauigkeit",
        "Good": "Gut",
        "Brilliant": "Brillant",
    },
    "es": {
        "Blunder": "Error grave",
        "Mistake": "Error",
        "Inaccuracy": "Imprecisión",
        "Good": "Buena",
        "Brilliant": "Brillante",
    },
    "it": {
        "Blunder": "Errore grave",
        "Mistake": "Errore",
        "Inaccuracy": "Imprecisione",
        "Good": "Buona",
        "Brilliant": "Brillante",
    },
    "nl": {
        "Blunder": "Blunder",
        "Mistake": "Fout",
        "Inaccuracy": "Onnauwkeurig",
        "Good": "Goed",
        "Brilliant": "Briljant",
    },
    "pl": {
        "Blunder": "Błąd",
        "Mistake": "Pomyłka",
        "Inaccuracy": "Niedokładność",
        "Good": "Dobry",
        "Brilliant": "Świetny",
    },
    "ru": {
        "Blunder": "Зевок",
        "Mistake": "Ошибка",
        "Inaccuracy": "Неточность",
        "Good": "Хороший",
        "Brilliant": "Отличный",
    },
    "tr": {
        "Blunder": "Vahim hata",
        "Mistake": "Hata",
        "Inaccuracy": "Kusurlu",
        "Good": "İyi",
        "Brilliant": "Parlak",
    },
}


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


def _set_locale(monkeypatch, code):
    monkeypatch.setattr(
        "universalchess.services.language_service.get_language", lambda: code
    )
    i18n.refresh_active_language()


def test_empty_quality_word_stays_empty():
    """No last move means no quality word; localization must not invent one.

    Why: the quality bar is blank before any move. How a regression manifests:
    a missing-key fallback returns the key string, putting ink on an empty bar.
    """
    assert localize_quality_word("") == ""


@pytest.mark.parametrize(
    "locale,word,expected",
    [
        (locale, word, label)
        for locale, mapping in _QUALITY_BY_LOCALE.items()
        for word, label in mapping.items()
    ],
)
def test_quality_word_localizes_in_every_shipped_locale(
    monkeypatch, locale, word, expected
):
    """Every shipped UI locale translates every quality classifier token.

    Why: the quality bar was shipping English tokens on non-English devices;
    translating only French (or only Inaccuracy) would leave the same leak in
    every other locale and word. How a regression manifests: one (locale, word)
    pair keeps the English token while the English baseline still passes.
    """
    _set_locale(monkeypatch, locale)
    assert localize_quality_word(word) == expected


def test_table_covers_every_shipped_locale_bundle():
    """The expectation table must list every non-English bundle beside English.

    Why: adding a UI locale without quality-word expectations would let that
    locale ship English forever. How a regression manifests: a locale file
    exists under i18n/locale/ but is absent from ``_QUALITY_BY_LOCALE``.
    """
    from pathlib import Path

    locale_dir = Path(i18n.__file__).parent / "locale"
    shipped = {p.stem for p in locale_dir.glob("*.json")}
    assert shipped == set(_QUALITY_BY_LOCALE)
