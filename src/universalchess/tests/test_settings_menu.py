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
    """Settings groups game setup first, then appearance, then device groups.

    Why: the regrouping pass makes the three pre-game setup items contiguous
    (Players, Time Control, Positions), then Display & Sound, then the two device
    groups (Connectivity, System). Chromecast moved into Connectivity and About
    moved into System, so neither appears at this level. Asserting the full
    ordered key list guards against accidental removal/reordering of a sibling.

    How the regression manifests: Chromecast or About reappears here, Connectivity
    is missing, or an item lands in an unexpected position - changing this exact
    list.
    """
    keys = [e.key for e in create_settings_entries(_game_settings(), _player(), _player())]

    assert keys == [
        "Players",
        "TimeControl",
        "Positions",
        DISPLAY_SOUND_KEY,
        "Connectivity",
        "System",
    ]


def test_settings_no_longer_has_chromecast_or_about():
    """Chromecast and About are no longer top-level Settings items.

    Why: Chromecast moved into the new Connectivity submenu (grouped with WiFi/
    Bluetooth/Accounts) and About moved into System. Leaving either here would
    re-create the inconsistent placement the regroup removed.

    How the regression manifests: 'Chromecast' or 'About' reappears in the
    top-level Settings key set.
    """
    keys = [e.key for e in create_settings_entries(_game_settings(), _player(), _player())]

    assert "Chromecast" not in keys, "Chromecast lives in Connectivity now"
    assert "About" not in keys, "About lives in System now"
