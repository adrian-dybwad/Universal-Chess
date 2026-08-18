"""Tests that Positions is a main-menu entry on the board, as on the web.

Background / why these tests exist
----------------------------------
Positions was a Settings entry on the board while the web renders it as its own
page, reached from the main navigation rather than from inside Settings. So the
two interfaces disagreed about what kind of thing it is: a device setting, or a
way to start playing.

Setting up a position is the latter -- it begins a game from a chosen start,
which is what the row does when selected. It now sits in the board's main menu
between PLAY and Settings, beside the other ways into a game.

The main menu is rendered from the shared ``main`` container but dispatched by
row key inside ``main()``, so a row added to the container without a matching
branch renders and then does nothing when selected. That coupling is invisible in
either file alone, so it is asserted against the application module's source:
the branch is a statement about the code, and reading it with ``ast`` states it
directly.
"""

import ast
import json
from pathlib import Path

import universalchess
from universalchess.epaper.icon_menu import IconMenuWidget
from universalchess.epaper.status_bar import STATUS_BAR_HEIGHT
from universalchess.menus.catalog.entry_builder import build_menu_entries
from universalchess.menus.catalog.loader import load_catalog
from universalchess.tests.app_source import BOARD_APP_PY, function_node

# The panel the board renders the menu into, below the status bar. Mirrors what
# main() hands the MenuManager.
DISPLAY_WIDTH = 128
DISPLAY_HEIGHT = 296

CATALOG_JSON = Path(universalchess.__file__).resolve().parent / "menus" / "catalog" / "menu.json"

POSITIONS_NODE = "main.positions"
POSITIONS_KEY = "Positions"
ROOT_DISPATCH = "main"
SETTINGS_DISPATCH = "_handle_settings"

# The rows Positions is placed between, by catalog id.
PLAY_NODE = "main.play"
SETTINGS_NODE = "main.settings"


def _catalog():
    return load_catalog()




def _compared_strings(function_name):
    """String literals the function compares against, i.e. the keys it dispatches.

    Includes ``in (...)`` membership tests as well as equality, since the root
    loop routes several tokens to the same branch that way.
    """
    compared = set()
    for node in ast.walk(function_node(function_name)):
        if not isinstance(node, ast.Compare):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                compared.add(comparator.value)
            elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                compared.update(
                    element.value
                    for element in comparator.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                )
    return compared


def _raw_nodes():
    """The catalog as authored, keyed by id (needed for the ``key`` field)."""
    return {n["id"]: n for n in json.loads(CATALOG_JSON.read_text())["nodes"]}


def test_positions_is_a_main_menu_entry():
    """The main menu lists Positions directly.

    Why this test exists: this is the placement the web has -- Positions is a
    page of its own, not something inside Settings -- and it is what the move is
    for. Setting up a position starts a game, so it belongs with the other ways
    into one.

    How a regression manifests: the row returns to Settings, so the board again
    files a way of starting a game among the device settings.
    """
    catalog = _catalog()
    assert catalog.has_node(POSITIONS_NODE)
    assert POSITIONS_NODE in catalog.child_ids("main")


def test_positions_sits_between_play_and_settings():
    """Positions is ordered after PLAY and before Settings.

    Why this test exists: it belongs beside PLAY because both start a game, and
    before Settings because Settings is where the device configuration begins.
    Matching the placement but not the position still moves the row somewhere the
    user has to hunt for.

    How a regression manifests: an append puts Positions last, below Original
    Centaur, where it reads as an unrelated afterthought.
    """
    children = _catalog().child_ids("main")
    assert children.index(PLAY_NODE) < children.index(POSITIONS_NODE)
    assert children.index(POSITIONS_NODE) < children.index(SETTINGS_NODE)


def test_positions_is_no_longer_a_settings_entry():
    """Settings no longer offers Positions.

    Why this test exists: leaving the old row would give the board two routes to
    the same screen and restore the mismatch this change removes.

    How a regression manifests: ``settings.positions`` resolves again, so
    Positions appears in both the main menu and Settings while the web has it in
    one place.
    """
    catalog = _catalog()
    assert not catalog.has_node("settings.positions")
    assert POSITIONS_NODE not in catalog.child_ids("settings")


def test_positions_keeps_the_key_and_platforms_it_is_dispatched_by():
    """The row keeps its dispatch key and stays on both platforms.

    Why this test exists: the root loop routes on the key, and the web needs the
    definition to keep supplying the page's label and icon. Moving the node
    between containers must not quietly change either.

    How a regression manifests: the key drifts and selecting Positions does
    nothing, or the node is narrowed to one platform and the other loses it.
    """
    node = _catalog().get_node(POSITIONS_NODE)
    assert node["key"] == POSITIONS_KEY
    assert node["type"] == "submenu"
    assert sorted(node.get("platforms", ["board", "web"])) == ["board", "web"]


def test_selecting_positions_from_the_main_menu_opens_it():
    """The root loop dispatches the Positions key.

    Why this test exists: the branch moved with the row. Without it the row
    renders and selecting it falls through to the end of the loop, redrawing the
    main menu -- which reads as a dead or frozen board rather than as missing
    wiring.

    How a regression manifests: the catalog gains the row and main() never
    matches its key, so Positions is unreachable from anywhere.
    """
    assert POSITIONS_KEY in _compared_strings(ROOT_DISPATCH)


def test_settings_no_longer_dispatches_positions():
    """The Settings handler stops routing a row it no longer shows.

    Why this test exists: dispatch left behind for a row that is gone is
    indistinguishable from dispatch still in use, and invites someone to restore
    the row it appears to support.

    How a regression manifests: both handlers claim Positions and it becomes
    unclear which menu owns it.
    """
    assert POSITIONS_KEY not in _compared_strings(SETTINGS_DISPATCH)


def test_the_main_menu_still_fits_on_one_screen():
    """Four rows draw without scrolling, and PLAY stays the dominant one.

    Why this test exists: the main menu allocates height by weight, so adding a
    row takes screen from the rows already there. Below the widget's minimum
    button height the menu silently starts scrolling, which turns a glanceable
    list into one the user has to page through, and PLAY -- deliberately the
    largest target on the board -- would shrink toward the others. Settings was
    reduced to the same weight as Positions and Original Centaur to pay for the
    new row.

    How a regression manifests: a later row or a raised weight pushes the total
    past the screen, and the bottom entry disappears below the fold with no
    indication it is there.
    """
    entries = build_menu_entries("main")
    widget = IconMenuWidget(
        0, STATUS_BAR_HEIGHT, DISPLAY_WIDTH, DISPLAY_HEIGHT - STATUS_BAR_HEIGHT,
        lambda *a, **k: None, entries=entries,
    )

    assert len(widget._buttons) == len(entries), "the main menu scrolls: a row is off screen"
    heights = {button.key: button.height for button in widget._buttons}
    assert min(heights.values()) >= widget.min_button_height
    play_height = heights["Universal"]
    assert all(
        play_height >= 2 * height
        for key, height in heights.items()
        if key != "Universal"
    ), f"PLAY is no longer twice the height of every other row: {heights}"


def test_every_main_menu_row_can_actually_be_selected():
    """Each row in the main container has a dispatch branch in main().

    Why this test exists: the main menu is rendered from the catalog but
    dispatched by row key, and nothing connects the two. This is the general
    invariant behind the Positions move rather than a check of that one row --
    the same gap that would silently break any future main-menu entry.

    How a regression manifests: a row is added to the container without its
    branch and does nothing when selected, with no error to explain why.
    """
    raw = _raw_nodes()
    dispatched = _compared_strings(ROOT_DISPATCH)
    undispatched = [
        f"{child_id} (key={raw[child_id].get('key')!r})"
        for child_id in _catalog().child_ids("main")
        if raw[child_id].get("key") not in dispatched
    ]
    assert undispatched == [], (
        f"these main-menu rows render but {ROOT_DISPATCH}() never matches their "
        f"key, so selecting them does nothing: {undispatched}"
    )
