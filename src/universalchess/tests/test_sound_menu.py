"""Tests for the Sound submenu entry composition.

Background / why these tests exist
----------------------------------
The Sound submenu is a list of per-category toggles plus a master "Sound
Enabled" switch. The optimization pass promotes the master switch to the top of
the list (it governs every other row), and the entry list is extracted into a
pure ``build_sound_entries(settings)`` so the ordering/labelling can be pinned
without driving the menu loop.
"""

from universalchess.menus.sound_menu import build_sound_entries


def _settings(**overrides):
    base = {
        "enabled": True,
        "piece_event": False,
        "game_event": True,
        "error": True,
        "key_press": False,
    }
    base.update(overrides)
    return base


def test_master_sound_toggle_is_first_entry():
    """'Sound Enabled' (master switch) is the first row of the Sound submenu.

    Why this test exists: the master switch gates all other categories, so it
    reads top-of-list rather than buried beneath the per-category toggles where
    it previously sat. The cursor also opens on this row.

    How the regression manifests: the 'enabled' key is no longer at index 0,
    pushing the master switch back below the category toggles.
    """
    keys = [e.key for e in build_sound_entries(_settings())]

    assert keys[0] == "enabled", "master Sound Enabled toggle must lead the list"
    assert keys == ["enabled", "piece_event", "game_event", "error", "key_press"]


def test_master_toggle_marked_bold():
    """The master switch is rendered bold to distinguish it from categories.

    Why: the master switch is structurally different (it governs the rest), and
    the bold styling is the visual cue. Pinning it guards the styling against an
    accidental reset when the row moves.
    """
    by_key = {e.key: e for e in build_sound_entries(_settings())}
    assert by_key["enabled"].bold is True


def test_toggle_icons_reflect_setting_state():
    """Each row's icon reflects its on/off state.

    Why: the checkbox-style icon is the only indication of whether a category is
    enabled; a regression mapping the wrong icon would misreport the state.

    How the regression manifests: an 'on' setting shows the empty/'timer' icon or
    vice versa.
    """
    by_key = {e.key: e for e in build_sound_entries(_settings(enabled=True, key_press=False))}

    assert by_key["enabled"].icon_name == "timer_checked"
    assert by_key["key_press"].icon_name == "timer"
