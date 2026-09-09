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
    assert entry.height_ratio == 2.4
    assert entry.icon_size == 80
    assert entry.layout == "vertical"
    assert entry.font_size == 32
    assert entry.bold is True
    # Default IconMenuEntry.border_width is 2. PLAY declares none so it uses
    # the same frame as the other root-menu rows; the extra height is what
    # keeps the 80px logo and 32px label inside that stroke.
    assert entry.border_width == 2


def test_node_to_entry_main_menu_secondary_rows_are_shorter():
    """Lichess, Centaur, and the Positions/Settings pair use 0.8 height.

    Why this test exists: PLAY's 80px logo and 32px label need the extra
    band those rows give up. If a secondary row returns to 1.0, PLAY shrinks
    back to 112px and the artwork sits on the cell edges again.

    How a regression manifests: any of these entries has height_ratio 1.0
    (or PLAY is no longer 2.4), so the four-row root menu no longer leaves
    inset around the knight.
    """
    catalog = load_catalog()
    play = node_to_entry(catalog.get_node("main.play"))
    lichess = node_to_entry(catalog.get_node("players.lichess"))
    centaur = node_to_entry(catalog.get_node("main.centaur"))
    positions = node_to_entry(catalog.get_node("main.positions"))
    settings = node_to_entry(catalog.get_node("main.settings"))
    assert play.height_ratio == 2.4
    assert lichess.height_ratio == 0.8
    assert centaur.height_ratio == 0.8
    assert positions.height_ratio == 0.8
    assert settings.height_ratio == 0.8


def test_centaur_board_label_is_one_line():
    """The e-paper Centaur row is the product name, not 'Original Centaur'.

    Why this test exists: two lines of 'Original\\nCentaur' needed a taller
    cell. Shortening the board label to Centaur is what lets that row share
    the 0.8 height with Lichess. The web label stays Original Centaur.

    How a regression manifests: boardLabel is missing so the board renders
    the two-line web string, which clips in the shorter cell.
    """
    node = load_catalog().get_node("main.centaur")
    assert node["label"] == "Original Centaur"
    assert node["boardLabel"] == "Centaur"
    assert "\n" not in node["boardLabel"]


def test_node_to_entry_forwards_shared_row():
    """Positions and Settings must carry the same epaper.row so they sit together.

    How the regression manifests: row is dropped and the pair stacks full-width.
    """
    catalog = load_catalog()
    positions = node_to_entry(catalog.get_node("main.positions"))
    settings = node_to_entry(catalog.get_node("main.settings"))
    assert positions.row == "secondary"
    assert settings.row == "secondary"
    assert positions.layout == "vertical"
    assert settings.layout == "vertical"


def test_node_to_entry_icon_only_blanks_the_board_label():
    """epaper.icon_only blanks the board label so the half-width cell is a glyph.

    Why this test exists: Positions and Settings share a 64px-wide cell. The
    web still names them; the board drops the words so the icon can fill the
    button. _row_to_entry always forwards the engine's resolved label, so
    icon_only must win over that override or the names still render.

    How a regression manifests: entry.label is "Positions"/"Settings" and the
    pair draws cramped text under a 24px icon. The web catalog label must stay.
    """
    catalog = load_catalog()
    positions_node = catalog.get_node("main.positions")
    settings_node = catalog.get_node("main.settings")
    assert positions_node["label"] == "Positions"
    assert settings_node["label"] == "Settings"
    assert positions_node["epaper"]["icon_only"] is True
    assert settings_node["epaper"]["icon_only"] is True

    positions = node_to_entry(positions_node, label="Positions")
    settings = node_to_entry(settings_node, label="Settings")
    assert positions.label == ""
    assert settings.label == ""
    assert positions.icon_name == "positions"
    assert settings.icon_name == "settings"
    assert positions.help == positions_node["help"]
    assert settings.help == settings_node["help"]


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
    without Centaur and asserts it is gone while PLAY, Positions, Lichess, and
    Settings remain.
    """
    entries = build_menu_entries("main", skip_keys={"Centaur"})
    keys = [e.key for e in entries]
    assert keys == ["Universal", "Lichess", "Positions", "Settings"]


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
