"""The Positions menu is drawn in the device's language.

Why these tests exist
---------------------
Positions builds its rows from INI section and entry keys by replacing
underscores and calling ``str.title``. That is English by construction, and it
is assembled in a variable, so :mod:`test_board_strings_are_localized` cannot
see it. The chrome around the menu (End Game?, Cancel, the Lichess alert)
already went through ``t()``; opening Positions on a Spanish board still showed
Pawn Endgames and Mate In 1 Back Rank.

Custom overlay entries have no bundle key -- the name is the user's -- so those
still title-case. Packaged categories and positions must not.
"""

import configparser
from pathlib import Path

import pytest

from universalchess.menus.positions_menu import (
    build_category_entries,
    build_position_entries,
    localized_category_label,
    localized_position_label,
)

POSITIONS_INI = (
    Path(__file__).resolve().parents[1] / "defaults" / "config" / "positions.ini"
)
SHIPPED_LOCALES = ("es", "fr", "de", "nl")
CUSTOM_CATEGORY = "custom"


def _with_locale(monkeypatch, locale):
    """Switch the board bundle to ``locale`` and clear the cache afterwards."""
    from universalchess import i18n

    monkeypatch.setattr(
        "universalchess.services.language_service.get_language", lambda: locale
    )
    i18n._active_locale = None
    i18n._bundles.clear()
    i18n.refresh_active_language()
    return i18n


def _reset_i18n():
    from universalchess import i18n

    i18n._active_locale = None
    i18n._bundles.clear()


@pytest.fixture
def spanish(monkeypatch):
    """Spanish board strings, with the locale cache cleared on the way out."""
    i18n = _with_locale(monkeypatch, "es")
    try:
        yield i18n
    finally:
        _reset_i18n()


def _packaged_catalog():
    """Section -> entry names from the shipped positions.ini."""
    config = configparser.ConfigParser(interpolation=None)
    config.read(POSITIONS_INI)
    return {section: [name for name, _value in config.items(section)] for section in config.sections()}


def test_category_rows_are_in_the_device_language(spanish):
    """Opening Positions must not title-case English INI section names.

    Why: that is how the whole menu stayed English after every other screen
    was localized. How a regression manifests: the pawn-endgames row still
    reads Pawn Endgames (the title-case of the section id) on a Spanish board.
    """
    entries = build_category_entries({"pawn_endgames": {"a": ("fen", None)}, "puzzles": {"b": ("fen", None)}})
    by_key = {entry.key: entry.label for entry in entries}
    assert "Pawn Endgames" not in by_key["pawn_endgames"]
    assert "Puzzles" not in by_key["puzzles"]
    assert spanish.t("positions.category.pawn_endgames") in by_key["pawn_endgames"]
    assert spanish.t("positions.category.puzzles") in by_key["puzzles"]


def test_packaged_position_rows_are_in_the_device_language(spanish):
    """A packaged position row must not be the title-case of its INI key.

    Why: the second-level Positions menu is the list of those keys. How a
    regression manifests: mate_in_1_back_rank still reads Mate In 1 Back Rank
    on a Spanish board.
    """
    entries = build_position_entries(
        "puzzles",
        {"mate_in_1_back_rank": ("8/8/8/8/8/8/8/8 w - - 0 1", None)},
        {},
    )
    label = entries[0].label.replace("\n", " ")
    assert "Mate In 1 Back Rank" not in label
    assert spanish.t("positions.item.mate_in_1_back_rank") == label


def test_custom_and_unknown_ids_still_title_case(spanish):
    """A name with no bundle key keeps the title-case fallback.

    Why: overlay entries and test fixtures are not in the bundle; showing the
    raw key ``positions.item.start_pos`` would be worse than Start Pos. How a
    regression manifests: a custom row renders the lookup key, or a packaged
    lookup is skipped and Puzzles returns.
    """
    assert localized_position_label("start_pos", category="custom") == "Start Pos"
    assert localized_position_label("my_trap", category=CUSTOM_CATEGORY) == "My Trap"
    assert localized_category_label("user_openings") == "User Openings"
    assert localized_category_label("puzzles") != "Puzzles"


def test_english_category_labels_match_the_old_title_case():
    """English boards must keep the labels title-case already produced.

    Why: localizing must not rewrite the English menu; only the other locales
    change. How a regression manifests: an English row no longer equals the
    title-case of its INI id.
    """
    catalog = _packaged_catalog()
    for category in catalog:
        assert localized_category_label(category) == category.replace("_", " ").title()


@pytest.mark.parametrize("locale", SHIPPED_LOCALES)
def test_every_packaged_position_string_is_translated(locale):
    """Every shipped category and position has a bundle entry in each locale.

    Why: ``t()`` falls back to English for a missing key, so a position added
    to the INI and forgotten in es.json ships as an English row in a Spanish
    menu. How a regression manifests: the missing keys are listed by name.
    Custom-section *items* are the user's and are not required; the Custom
    *category* label is.
    """
    from universalchess import i18n

    catalog = _packaged_catalog()
    bundle = i18n._load_bundle(locale)
    missing = []
    for category, names in catalog.items():
        if f"positions.category.{category}" not in bundle:
            missing.append(f"positions.category.{category}")
        if category == CUSTOM_CATEGORY:
            continue
        for name in names:
            if f"positions.item.{name}" not in bundle:
                missing.append(f"positions.item.{name}")
    assert missing == [], f"{locale}.json translates nothing for: {missing}"
