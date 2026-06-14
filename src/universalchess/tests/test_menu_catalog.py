"""Tests for the shared menu catalog and its loader.

The catalog (menu.json + icons.json) is the single source of truth that drives
both the e-paper board menus and the web UI. These tests guard the structural
invariants the renderers rely on: that the packaged catalog loads, that every
cross-reference resolves, and that validation rejects the specific authoring
mistakes that would otherwise produce a blank/broken menu at runtime.
"""

import json

import pytest

from universalchess.menus.catalog import CatalogError, MenuCatalog, load_catalog


# Minimal valid icon registry reused by the synthetic-catalog tests below.
_MIN_ICONS = {"version": 1, "icons": {"settings": {"description": "gear"}}}


def _write(tmp_path, menu: dict, icons: dict = None):
    """Write menu/icons JSON to tmp files and return their paths."""
    menu_path = tmp_path / "menu.json"
    icons_path = tmp_path / "icons.json"
    menu_path.write_text(json.dumps(menu), encoding="utf-8")
    icons_path.write_text(json.dumps(icons or _MIN_ICONS), encoding="utf-8")
    return menu_path, icons_path


def test_packaged_catalog_loads_and_validates():
    """The shipped catalog must load and pass validation.

    Guards against a malformed packaged menu.json/icons.json. If a reference is
    broken or JSON is invalid, load_catalog raises CatalogError and this fails
    immediately rather than the board/web rendering an empty menu later.
    """
    catalog = load_catalog()
    assert isinstance(catalog, MenuCatalog)
    # Sanity: the known board roots are present.
    assert "main" in catalog.roots()
    assert "settings" in catalog.roots()
    # Sanity: the flat node list is non-trivial.
    assert catalog.has_node("settings.connectivity")


def test_every_node_icon_is_registered():
    """Every node icon must exist in the icon registry.

    A typo'd icon id renders as a blank placeholder square on the board. This
    walks all nodes and asserts each icon resolves; a missing id fails here
    instead of shipping an invisible menu entry.
    """
    catalog = load_catalog()
    icon_ids = catalog.icon_ids()
    for node in catalog.raw_menu()["nodes"]:
        icon = node.get("icon")
        if icon is not None:
            assert icon in icon_ids, f"node {node['id']} uses unregistered icon {icon}"


def test_children_and_targets_resolve_to_nodes():
    """Every children/target reference must resolve to a real node.

    Dangling navigation references would crash or dead-end the menu. Failure
    manifests as a KeyError from get_node when a renderer follows the reference;
    this asserts they all resolve up front.
    """
    catalog = load_catalog()
    for node in catalog.raw_menu()["nodes"]:
        for child_id in node.get("children", []):
            assert catalog.has_node(child_id), f"{node['id']} -> missing child {child_id}"
        target = node.get("target")
        if target is not None:
            assert catalog.has_node(target), f"{node['id']} -> missing target {target}"


def test_board_main_menu_keys_match_renderer_contract():
    """The main menu's board selection keys must stay stable.

    The main loop routes on these exact keys (Universal/Settings/Centaur). If a
    catalog edit changes a key, board routing silently breaks; this pins them.
    """
    catalog = load_catalog()
    keys = [c["key"] for c in catalog.children("main")]
    assert keys == ["Universal", "Settings", "Centaur"]


def test_settings_order_matches_board_layout():
    """Settings children order must match the intended board layout.

    The board renders settings in this order; a reordering here would reorder
    the physical menu. Pinning the order makes such a change deliberate.
    """
    catalog = load_catalog()
    keys = [c["key"] for c in catalog.children("settings")]
    assert keys == ["Players", "TimeControl", "Positions", "DisplaySound", "Connectivity", "System"]


def test_web_sections_present_in_expected_order():
    """Web tabs are derived from catalog sections; order/content is pinned.

    The React Settings sidebar renders these tabs in order. A missing or
    reordered section would move/remove a settings tab; this guards the set.
    """
    catalog = load_catalog()
    section_ids = [s["id"] for s in catalog.sections()]
    assert section_ids == ["players", "display", "game", "accounts", "engines", "system"]


def test_web_implemented_submenus_are_enabled_for_web():
    """Catalog platform flags must expose menus implemented by the React app.

    The web UI has first-class pages/cards for Positions and the full
    Connectivity group, plus Settings sections for Engines/System. If these
    nodes stay board-only, menu-schema consumers see stale platform metadata and
    web/e-paper parity drifts.

    Regression manifestation: a web-implemented node lists only "board", so a
    web renderer or validation tool hides a menu that exists in the React app.
    """
    catalog = load_catalog()
    web_enabled = {
        node["id"]
        for node in catalog.raw_menu()["nodes"]
        if "web" in node.get("platforms", ["board", "web"])
    }

    assert {
        "settings.positions",
        "settings.connectivity",
        "connectivity",
        "connectivity.wifi",
        "connectivity.bluetooth",
        "connectivity.chromecast",
        "connectivity.accounts",
        "system.engines",
        "system.analysis",
        "system.about",
    }.issubset(web_enabled)


def test_option_sets_resolve_for_select_fields():
    """Every select field that names an optionSet must resolve to options.

    A select referencing a missing optionSet renders an empty dropdown on the
    web. This asserts each referenced set exists and is non-empty.
    """
    catalog = load_catalog()
    for node in catalog.raw_menu()["nodes"]:
        name = node.get("optionSet")
        if name is not None:
            options = catalog.option_set(name)
            assert options, f"node {node['id']} -> empty optionSet {name}"


def test_duplicate_node_id_raises(tmp_path):
    """Duplicate ids must be rejected.

    Two nodes with the same id would shadow each other in the id index, so a
    lookup returns the wrong node. Validation must raise CatalogError naming the
    duplicate rather than silently keeping the last one.
    """
    menu = {
        "roots": ["a"],
        "nodes": [
            {"id": "a", "type": "menu", "children": []},
            {"id": "a", "type": "action"},
        ],
    }
    menu_path, icons_path = _write(tmp_path, menu)
    with pytest.raises(CatalogError, match="duplicate node id: a"):
        load_catalog(menu_path, icons_path)


def test_unknown_icon_raises(tmp_path):
    """An unregistered icon id must be rejected.

    Catches the typo that would render a blank placeholder. Failure to validate
    here would let the bad id ship; the test asserts CatalogError names the icon.
    """
    menu = {"roots": ["a"], "nodes": [{"id": "a", "type": "action", "icon": "nope"}]}
    menu_path, icons_path = _write(tmp_path, menu)
    with pytest.raises(CatalogError, match="unknown icon 'nope'"):
        load_catalog(menu_path, icons_path)


def test_unknown_child_reference_raises(tmp_path):
    """A dangling child reference must be rejected.

    A child id with no matching node dead-ends navigation. Validation must
    raise CatalogError identifying the missing child instead of deferring to a
    runtime KeyError.
    """
    menu = {"roots": ["a"], "nodes": [{"id": "a", "type": "menu", "children": ["ghost"]}]}
    menu_path, icons_path = _write(tmp_path, menu)
    with pytest.raises(CatalogError, match="unknown child 'ghost'"):
        load_catalog(menu_path, icons_path)


def test_unknown_root_reference_raises(tmp_path):
    """A root pointing at a missing node must be rejected.

    Roots are entry points; an unknown root would render nothing. This asserts
    validation flags it rather than producing an empty top-level menu.
    """
    menu = {"roots": ["missing"], "nodes": [{"id": "a", "type": "menu"}]}
    menu_path, icons_path = _write(tmp_path, menu)
    with pytest.raises(CatalogError, match="unknown node 'missing'"):
        load_catalog(menu_path, icons_path)
