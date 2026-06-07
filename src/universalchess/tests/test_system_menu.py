"""Tests for the System submenu entry composition.

Background / why these tests exist
----------------------------------
Display and Sound were merged into a single "Display & Sound" menu that is
promoted up to the top-level Settings list (and reused by the in-game
long-press). As a result the System submenu must no longer expose its own
Display or Sound entries. These tests pin that removal so a future edit cannot
silently reintroduce the duplicate entry points.
"""

from universalchess.menus.system_menu import create_system_entries, create_power_entries


class _FakeBoard:
    """Minimal board stub: create_system_entries only needs the sleep timer."""

    def get_inactivity_timeout(self):
        return 0


def _game_settings(**overrides):
    base = {"analysis_mode": False}
    base.update(overrides)
    return base


def test_system_menu_no_longer_has_display_or_sound():
    """System must not expose Display or Sound after the merge.

    Why: those two items moved into the combined Display & Sound menu under
    Settings. Leaving them here would mean two divergent entry points for the
    same settings.

    How the regression manifests: 'Display' or 'Sound' reappears in the System
    key set, re-creating the duplicate (and the System list grows back by two).
    """
    keys = [e.key for e in create_system_entries(_FakeBoard(), _game_settings())]

    assert "Display" not in keys, "Display must live in the Display & Sound menu, not System"
    assert "Sound" not in keys, "Sound must live in the Display & Sound menu, not System"


def test_system_menu_order_groups_related_items_and_isolates_power():
    """System lists connectivity, engines, device, reset, then a Power submenu.

    Why this test exists: the optimization pass regroups System so the two
    engine-related items sit together (Engine Manager then Analysis Engine) and
    the destructive power actions (Shutdown/Reboot) are isolated behind a single
    Power submenu entry rather than living loose at the top level.

    How the regression manifests: a reordering that splits the engine items, or
    a revert that puts Shutdown/Reboot back at the top level, changes this exact
    key sequence and fails here.
    """
    keys = [e.key for e in create_system_entries(_FakeBoard(), _game_settings())]

    assert keys == [
        "WiFi",
        "Bluetooth",
        "Accounts",
        "Engines",
        "AnalysisMode",
        "Inactivity",
        "ResetSettings",
        "Power",
    ]


def test_shutdown_and_reboot_moved_off_top_level():
    """Shutdown/Reboot must not be top-level System entries after the regroup.

    Why: they are destructive/power actions now grouped under the Power submenu;
    leaving them at the top level keeps the accidental-press risk the regroup was
    meant to remove.

    How the regression manifests: 'Shutdown' or 'Reboot' reappears in the
    top-level System key set.
    """
    keys = [e.key for e in create_system_entries(_FakeBoard(), _game_settings())]

    assert "Shutdown" not in keys, "Shutdown belongs in the Power submenu"
    assert "Reboot" not in keys, "Reboot belongs in the Power submenu"


def test_analysis_entry_relabelled_engine_not_mode():
    """The analysis System item is labelled 'Analysis Engine', not 'Analysis Mode'.

    Why this test exists: 'Analysis Mode' was ambiguous against the in-game
    'Show Analysis' view toggle; the System item selects the *engine* (and is not
    safely changeable mid-game), so it is disambiguated to 'Analysis Engine'. The
    dispatch key stays 'AnalysisMode' to avoid touching call sites.

    How the regression manifests: the label reverts to containing 'Mode', so the
    user again sees two confusingly-named 'Analysis' controls.
    """
    entries = create_system_entries(_FakeBoard(), _game_settings())
    analysis = next(e for e in entries if e.key == "AnalysisMode")

    assert analysis.label == "Analysis\nEngine"
    assert "Mode" not in analysis.label


def test_power_submenu_contains_only_shutdown_and_reboot():
    """The Power submenu holds exactly Shutdown then Reboot.

    Why this test exists: the isolated power actions must remain reachable after
    being pulled out of the top level. Pinning the contents/order guards both the
    accidental loss of an action and an unexpected addition.

    How the regression manifests: a missing action (length/keys change) or a
    reordering flips this list.
    """
    keys = [e.key for e in create_power_entries()]

    assert keys == ["Shutdown", "Reboot"]
