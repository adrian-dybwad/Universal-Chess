"""Tests that Engines is a top-level Settings entry on the board, as on the web.

Background / why these tests exist
----------------------------------
The web gives Engines its own Settings tab, a peer of Players, Game, Display and
System. The board buried the same feature inside System, so the two interfaces
disagreed about what kind of thing engine management is, and the board reached it
one level deeper than everything else in Settings.

The web structure wins: the board lists Engines directly in Settings, ordered as
the web orders its tabs (after Connectivity, before System).

The board's Settings list is dispatched by row key in ``_handle_settings`` rather
than through catalog actions, so a row added to the container without a matching
branch renders and then does nothing when selected. That coupling is invisible in
either file alone, so it is asserted here against the application module's
source: the branch is a statement about the code, and reading it with ``ast``
states it directly.
"""

import ast
import json
from pathlib import Path

import universalchess
from universalchess.menus.catalog.loader import load_catalog
from universalchess.tests.app_source import BOARD_APP_PY, function_node
CATALOG_JSON = Path(universalchess.__file__).resolve().parent / "menus" / "catalog" / "menu.json"

ENGINES_NODE = "settings.engines"
SETTINGS_DISPATCH = "_handle_settings"
SYSTEM_CONTEXT = "_build_system_context"
ENGINE_MANAGER_ACTION = "engine_manager"


def _catalog():
    return load_catalog()




def _compared_strings(function_name):
    """String literals the function compares against, i.e. the keys it dispatches."""
    return {
        comparator.value
        for node in ast.walk(function_node(function_name))
        if isinstance(node, ast.Compare)
        for comparator in node.comparators
        if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str)
    }


def _registered_actions(function_name):
    """Action names a ``_build_*_context`` function registers on its context."""
    return {
        node.args[0].value
        for node in ast.walk(function_node(function_name))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register_action"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }


def _raw_nodes():
    """The catalog as authored, keyed by id (needed for the ``key`` field)."""
    return {n["id"]: n for n in json.loads(CATALOG_JSON.read_text())["nodes"]}


def test_engines_is_a_top_level_settings_entry():
    """Settings lists Engines itself rather than reaching it through System.

    Why this test exists: this is the placement the web has had all along, and
    the point of the move -- engine management is a peer of Players and Display,
    not a system-maintenance detail.

    How a regression manifests: the row returns to a submenu, so the board once
    again hides it a level deeper than the web and the two interfaces disagree
    about what Engines is.
    """
    catalog = _catalog()
    assert catalog.has_node(ENGINES_NODE)
    assert ENGINES_NODE in catalog.child_ids("settings")


def test_engines_is_ordered_like_the_web_settings_tabs():
    """Engines sits after Connectivity and before System.

    Why this test exists: the web orders its tabs with Engines between
    Connectivity and System. Matching the placement but not the order still
    leaves a user who learned one interface hunting on the other.

    How a regression manifests: an append puts Engines last, after System, which
    on the web is the final tab.
    """
    children = _catalog().child_ids("settings")
    assert children.index("settings.connectivity") < children.index(ENGINES_NODE)
    assert children.index(ENGINES_NODE) < children.index("settings.system")


def test_engines_is_no_longer_inside_the_system_menu():
    """No System row opens the engine manager any more.

    Why this test exists: leaving the old row would give the board two routes to
    the same screen and restore the mismatch this change removes.

    How a regression manifests: ``system.engines`` resolves again, or some other
    System child carries the engine-manager action, so Engines appears in both
    places on the board and neither matches the web.
    """
    catalog = _catalog()
    assert not catalog.has_node("system.engines")
    for child_id in catalog.child_ids("system"):
        node = catalog.get_node(child_id)
        assert node.get("action") != ENGINE_MANAGER_ACTION, child_id


def test_engines_row_opens_the_engine_manager():
    """The row is an action wired to the engine manager, shared with the web.

    Why this test exists: the engine manager is code-driven because its list is
    dynamic, so this row is an action rather than a submenu with a target.
    Keeping it on both platforms is what lets the web tab's label and icon come
    from the same definition the board renders.

    How a regression manifests: the action name drifts and selecting Engines
    raises for an unregistered action, or the node is narrowed to one platform
    and the other loses its definition.
    """
    node = _catalog().get_node(ENGINES_NODE)
    assert node["type"] == "action"
    assert node.get("action") == ENGINE_MANAGER_ACTION
    assert node.get("section") == "engines"
    assert sorted(node.get("platforms", ["board", "web"])) == ["board", "web"]


def test_every_settings_row_can_actually_be_selected():
    """Each row in the Settings container has a dispatch branch in the app.

    Why this test exists: the Settings list is rendered from the catalog but
    dispatched by row key in ``_handle_settings``. Nothing connects the two, so a
    row added to the container alone draws normally and does nothing at all when
    selected -- a dead entry that looks entirely healthy. This is the general
    invariant behind the Engines move rather than a check of that one row.

    How a regression manifests: a new Settings entry is added to the catalog
    without its branch, and selecting it returns to the same list with no
    feedback, which reads as a frozen board rather than as missing wiring.
    """
    raw = _raw_nodes()
    dispatched = _compared_strings(SETTINGS_DISPATCH)
    undispatched = [
        f"{child_id} (key={raw[child_id].get('key')!r})"
        for child_id in _catalog().child_ids("settings")
        if raw[child_id].get("key") not in dispatched
    ]
    assert undispatched == [], (
        f"these Settings rows render but {SETTINGS_DISPATCH} never matches their "
        f"key, so selecting them does nothing: {undispatched}"
    )


def test_system_context_drops_the_engine_manager_action():
    """The System context stops registering an action it no longer serves.

    Why this test exists: the action moved with the row. Wiring left behind for a
    row that is gone is indistinguishable from wiring still in use, and invites
    someone to restore the row it appears to support.

    How a regression manifests: the two contexts drift and it becomes unclear
    which menu owns engine management.
    """
    assert ENGINE_MANAGER_ACTION not in _registered_actions(SYSTEM_CONTEXT)
