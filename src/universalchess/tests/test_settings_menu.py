"""Tests for the top-level Settings submenu entry composition.

Background / why these tests exist
----------------------------------
Display and Sound are independent top-level Settings entries (split out of the
former combined "Display & Sound" item), placed right after Time Control. These
tests pin that both entries exist with the agreed key/label/icon and that the
rest of the Settings list is preserved.
"""

from universalchess.menus.settings_menu import create_settings_entries

DISPLAY_KEY = "Display"
SOUND_KEY = "Sound"


def _player(player_type="human", **overrides):
    base = {"type": player_type, "engine": "stockfish", "hand_brain_mode": "normal"}
    base.update(overrides)
    return base


def _game_settings(**overrides):
    base = {"time_control": 0}
    base.update(overrides)
    return base


def test_settings_includes_separate_display_and_sound_entries():
    """Settings exposes Display and Sound as two independent menu entries.

    Why: Display and Sound were split into separate sibling entries (no longer a
    combined "Display & Sound" item, not buried in System). The keys are the
    contract the main-loop dispatch keys off of.

    How the regression manifests: a Display or Sound key is missing (or the old
    'DisplaySound' key reappears), so selecting it from Settings does nothing or
    routes to a removed handler.
    """
    entries = create_settings_entries(_game_settings(), _player(), _player())
    by_key = {e.key: e for e in entries}

    assert DISPLAY_KEY in by_key, "Settings must offer the Display menu"
    assert SOUND_KEY in by_key, "Settings must offer the Sound menu"
    assert "DisplaySound" not in by_key, "the combined entry was split into Display + Sound"

    display_entry = by_key[DISPLAY_KEY]
    assert display_entry.label == "Display"
    assert display_entry.icon_name == "display"

    sound_entry = by_key[SOUND_KEY]
    assert sound_entry.label == "Sound"
    assert sound_entry.icon_name == "sound"


def test_settings_full_entry_order():
    """Settings groups game setup first, then appearance, then device groups.

    Why: the regrouping pass keeps the pre-game setup items first (Players, Time
    Control), then the appearance pair Display and Sound, then Positions, then the
    two device groups (Connectivity, System). Chromecast moved into Connectivity
    and About moved into System, so neither appears at this level. Asserting the
    full ordered key list guards against accidental removal/reordering of a
    sibling.

    How the regression manifests: Display/Sound land out of order, Chromecast or
    About reappears here, Connectivity is missing, or an item lands in an
    unexpected position - changing this exact list.
    """
    keys = [e.key for e in create_settings_entries(_game_settings(), _player(), _player())]

    assert keys == [
        "Players",
        "TimeControl",
        DISPLAY_KEY,
        SOUND_KEY,
        "Positions",
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
