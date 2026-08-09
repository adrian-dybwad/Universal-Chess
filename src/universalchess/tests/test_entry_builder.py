"""Tests for the catalog -> IconMenuEntry builder.

The board renderer builds its static menus from catalog nodes via this module.
These tests guard the node->entry field mapping, the per-key overrides dynamic
menus rely on, the skip-key behavior used to hide the Centaur entry, and the
help lookup used by the board help dialog.
"""

from universalchess.menus.catalog import load_catalog
from universalchess.menus.catalog.entry_builder import (
    build_menu_entries,
    help_for_key,
    node_to_entry,
)


def test_node_to_entry_maps_fields_and_style():
    """A node's fields and epaper style must map onto IconMenuEntry.

    Guards the field mapping: if a style key (e.g. height_ratio) is dropped, the
    board entry renders at the wrong size. Uses the main.play node which has a
    full epaper style block.
    """
    catalog = load_catalog()
    entry = node_to_entry(catalog.get_node("main.play"))
    assert entry.key == "Universal"
    assert entry.label == "PLAY"
    assert entry.icon_name == "universal_logo"
    assert entry.height_ratio == 2.0
    assert entry.icon_size == 80
    assert entry.layout == "vertical"
    assert entry.font_size == 32
    assert entry.bold is True


def test_node_to_entry_overrides_label_and_icon():
    """Overrides must replace the catalog label/icon for dynamic entries.

    Dynamic menus pass computed label/icon. If overrides were ignored, the menu
    would show stale static text (e.g. "Time Control" instead of "Time 5 min").
    """
    catalog = load_catalog()
    entry = node_to_entry(
        catalog.get_node("settings.timecontrol"), label="Time\n5 min", icon="timer_checked"
    )
    assert entry.label == "Time\n5 min"
    assert entry.icon_name == "timer_checked"


def test_node_to_entry_forwards_state_footer_fields():
    """description + trailing_icon must map onto the entry while keeping node style.

    Why this test exists: the WiFi/Bluetooth merged status button reads as a
    toggle only when its Enabled/Disabled description and checkbox trailing icon
    reach the renderer *and* the node's vertical readout chrome is preserved.
    Uses wifi.enabled, which has a full vertical epaper block.

    How a regression manifests: if node_to_entry dropped the new kwargs, the
    footer fields would be None (no checkbox/label renders); if it dropped the
    epaper style, the button would lose its vertical layout/height.
    """
    catalog = load_catalog()
    entry = node_to_entry(
        catalog.get_node("wifi.enabled"),
        label="MyNetwork",
        icon="wifi_full",
        description="Enabled",
        trailing_icon="checkbox_checked",
    )
    # Footer fields forwarded.
    assert entry.description == "Enabled"
    assert entry.trailing_icon_name == "checkbox_checked"
    # Node's vertical readout chrome preserved alongside the footer.
    assert entry.layout == "vertical"
    assert entry.selectable is True
    assert entry.label == "MyNetwork"
    assert entry.icon_name == "wifi_full"


def test_build_menu_entries_order_and_keys():
    """Children must be built in declared order with their selection keys.

    The main loop routes on entry order/keys; a reorder or dropped child would
    misroute. This pins the settings container's keys and order.
    """
    entries = build_menu_entries("settings")
    keys = [e.key for e in entries]
    assert keys == [
        "Players", "Game", "Display", "Sound",
        "Connectivity", "Engines", "Agents", "System",
    ]


def test_build_menu_entries_skip_keys_hides_entry():
    """skip_keys must omit the named entry entirely.

    Used to hide the Centaur entry when the original software is absent. If skip
    were ignored, a non-functional entry would appear. Builds the main menu
    without Centaur and asserts it is gone while the others remain.
    """
    entries = build_menu_entries("main", skip_keys={"Centaur"})
    keys = [e.key for e in entries]
    assert keys == ["Universal", "Positions", "Settings"]


def test_help_for_key_returns_catalog_help():
    """help_for_key must return the catalog help tip for a container child.

    The board help dialog shows this text. A wrong/empty return would show no
    help for the entry. Checks a known entry's help string.
    """
    assert help_for_key("main", "Positions") == (
        "Set up a predefined position on the board to practice or analyze."
    )
    # Unknown key yields None rather than raising, so the dialog can fall back.
    assert help_for_key("main", "Nonexistent") is None
