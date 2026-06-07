"""Tests for the top-level Settings submenu entry composition.

Background / why these tests exist
----------------------------------
The combined "Display & Sound" menu is promoted to the top-level Settings list
so it sits one tap away and shares its definition with the in-game long-press.
These tests pin that the entry exists with the agreed key/label/icon and that
the rest of the Settings list is preserved.
"""

from universalchess.menus.settings_menu import create_settings_entries

DISPLAY_SOUND_KEY = "DisplaySound"


def _player(player_type="human", **overrides):
    base = {"type": player_type, "engine": "stockfish", "hand_brain_mode": "normal"}
    base.update(overrides)
    return base


def _game_settings(**overrides):
    base = {"time_control": 0}
    base.update(overrides)
    return base


def test_settings_includes_display_sound_entry():
    """Settings exposes the combined Display & Sound menu entry.

    Why: the merged menu must be reachable directly from Settings (not buried in
    System). The key is the contract the main-loop dispatch keys off of.

    How the regression manifests: the DisplaySound key is missing, so selecting
    it from Settings does nothing and the only access is the in-game long-press.
    """
    entries = create_settings_entries(_game_settings(), _player(), _player())
    by_key = {e.key: e for e in entries}

    assert DISPLAY_SOUND_KEY in by_key, "Settings must offer the Display & Sound menu"
    entry = by_key[DISPLAY_SOUND_KEY]
    assert entry.label == "Display\n& Sound"
    assert entry.icon_name == "display"


def test_settings_full_entry_order():
    """Display & Sound is the second item (right below Players); rest unchanged.

    Why: the merged menu is the most-used in-game adjustment, so it sits second,
    directly under Players. Asserting the full ordered key list guards against
    accidental removal/reordering of a sibling entry.

    How the regression manifests: a missing/extra key, or DisplaySound landing in
    an unexpected position, changes this exact list.
    """
    keys = [e.key for e in create_settings_entries(_game_settings(), _player(), _player())]

    assert keys == [
        "Players",
        DISPLAY_SOUND_KEY,
        "TimeControl",
        "Positions",
        "Chromecast",
        "System",
        "About",
    ]
