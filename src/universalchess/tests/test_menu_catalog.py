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
    instead of shipping an invisible menu entry. Icons may be a plain string or
    a state map ``{state: icon}`` (e.g. a toggle's checked/unchecked glyphs);
    every value of the latter must also be registered.
    """
    catalog = load_catalog()
    icon_ids = catalog.icon_ids()
    for node in catalog.raw_menu()["nodes"]:
        icon = node.get("icon")
        if isinstance(icon, dict):
            for state, icon_name in icon.items():
                assert icon_name in icon_ids, (
                    f"node {node['id']} state '{state}' uses unregistered icon {icon_name}"
                )
        elif icon is not None:
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

    The main loop routes on these exact keys. If a catalog edit changes a key,
    board routing silently breaks; this pins them.
    """
    catalog = load_catalog()
    keys = [c["key"] for c in catalog.children("main")]
    assert keys == ["Universal", "Lichess", "Centaur", "Positions", "Settings"]


def test_settings_order_matches_board_layout():
    """This array is the Settings order for both surfaces.

    The board renders its Settings menu from it and the web derives its tab
    sequence from it, so a reordering here moves both. Pinning it makes such a
    change deliberate and reviewed rather than incidental.

    The web used to carry its own ordered list of tabs, which is how Agents came
    to sit third on the board and seventh on the web. Agents now sits after
    Engines on both, because that was the order the web had and the web's order
    was the one being kept. Every entry here backs a web tab: Positions, which did
    not, is a main-menu entry on both surfaces.
    """
    catalog = load_catalog()
    keys = [c["key"] for c in catalog.children("settings")]
    assert keys == [
        "Players", "Game", "Display", "Sound",
        "Connectivity", "Engines", "Agents", "System",
    ]


def test_display_and_sound_are_separate_settings_nodes():
    """Display and Sound must be two independent Settings submenu nodes.

    Why this test exists: Display and Sound were split out of the former combined
    'DisplaySound' entry into separate sibling entries (right after Game) on both
    the board and the web. This pins the post-split structure so a regression that
    re-merges them, drops one, or restores the old combined node fails here.

    How a regression manifests: if the combined node is restored, 'settings.display'
    or 'settings.sound' is absent (KeyError-style miss caught by has_node), or the
    stale 'settings.displaysound' node reappears in the id index.
    """
    catalog = load_catalog()
    assert catalog.has_node("settings.display")
    assert catalog.has_node("settings.sound")
    assert catalog.get_node("settings.display")["key"] == "Display"
    assert catalog.get_node("settings.sound")["key"] == "Sound"
    node_ids = {n["id"] for n in catalog.raw_menu()["nodes"]}
    assert "settings.displaysound" not in node_ids
    # The Sound effect toggles must be tagged to the new 'sound' section so the
    # web Sound tab renders them (and they no longer leak into the Display tab).
    for sound_field in (
        "field.sound.enabled",
        "field.sound.piece_events",
        "field.sound.game_events",
        "field.sound.errors",
        "field.sound.key_press",
    ):
        assert catalog.get_node(sound_field)["section"] == "sound"


def test_every_web_settings_tab_has_a_section_to_name_it():
    """Each Settings child the web tabs must have a section supplying its chrome.

    Why this test exists: the web builds a tab for each `settings` child whose id
    matches a section, and takes the tab's label and icon from that section. A
    child with no section is silently not a tab -- which is how Positions is
    correctly left out, and is also how a genuine tab would vanish without any
    error if its section were removed or renamed.

    This asserts the set of sections rather than their order. The docstring here
    used to claim the web derived its tab order from this array; it never did,
    and the order now comes from `settings` children, so pinning the sequence of
    this list would assert something no user can observe. 'accounts' is a section
    without being a Settings child because it renders as a card inside the
    Connectivity panel.

    How a regression manifests: a section is renamed or dropped and its tab
    disappears from the web with nothing else failing.
    """
    catalog = load_catalog()
    section_ids = {s["id"] for s in catalog.sections()}
    # Positions is deliberately absent: the web renders it as its own page.
    tabbed = {"players", "game", "display", "sound", "connectivity", "engines",
              "agents", "system"}

    assert tabbed <= section_ids, f"sections missing for tabs: {tabbed - section_ids}"
    assert "accounts" in section_ids
    assert "positions" not in section_ids


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
        "main.positions",
        "settings.connectivity",
        "connectivity",
        "connectivity.wifi",
        "connectivity.bluetooth",
        "connectivity.usb_gadget",
        "connectivity.chromecast",
        "players.lichess",
        "settings.engines",
        "system.about",
        # System/power actions now have web controls (Settings -> System).
        "system.inactivity",
        "system.reset",
        "system.power",
        "power.shutdown",
        "power.reboot",
        "main.centaur",
    }.issubset(web_enabled)


def test_sleep_timer_option_set_matches_board_choices():
    """The sleep_timer option set must list the exact Sleep Timer choices in seconds.

    Why this test exists: the board Sleep Timer menu and the web Sleep Timer
    select both render this option set, and the saved value is the seconds in
    'value'. If the values drift from seconds (e.g. back to minutes) the board
    would sleep after the wrong interval and the web select would save a bad key.

    How a regression manifests: the (value, label) pairs change, so this exact
    list no longer matches.
    """
    catalog = load_catalog()
    options = [(o["value"], o["label"]) for o in catalog.option_set("sleep_timer")]
    assert options == [
        ("0", "Disabled"),
        ("300", "5 min"),
        ("600", "10 min"),
        ("900", "15 min"),
        ("1800", "30 min"),
        ("3600", "1 hour"),
    ]


def test_usb_gadget_options_match_the_service_modes_and_all_describe_themselves():
    """The four USB gadget options are exactly the modes the service accepts.

    Why: the widget, the service allowlist and the privileged helper's verbs have
    to agree. An option the service rejects gives the user a radio that 400s, and
    a mode missing from the catalog is unreachable from either UI. The
    descriptions are the only place the difference between the modes is explained,
    so an option without one is a radio the user cannot choose between.

    Failure: a mode added to one layer only, or a new option shipped with no
    description -- the web radio then renders a bare label.
    """
    from universalchess.services.usb_gadget_service import MODES

    options = load_catalog().option_set("usb_gadget_mode")
    values = [str(option["value"]) for option in options]
    assert values == ["off", "auto", "client", "shared"]
    assert set(values) == set(MODES)
    for option in options:
        assert option.get("label"), f"{option['value']} has no label"
        assert option.get("description", "").strip(), (
            f"{option['value']} has no description"
        )


def test_usb_gadget_help_says_which_physical_port_the_cable_needs():
    """The field help must name the Pi's data port and rule out the charge port.

    Why: on a Centaur the only socket an owner can see is the one they charge the
    board with, and it carries no data -- the gadget needs the Raspberry Pi Zero's
    own USB port, inside the case. Every mode in this widget is unreachable
    without that cable, so a correctly configured board looks broken: the mode
    applies, the status card reports Disconnected, and nothing in the UI says the
    cable is in the wrong socket. This help is the top of the widget on both the
    board and the web, so it is the one place the requirement is read before the
    choice is made.

    Failure: the help loses the distinction (or names only "USB"), and the next
    person to plug into the charge port has nothing to tell them why the link
    never comes up. Asserted in Spanish too: a translated board must not drop the
    precondition, which is the kind of omission an overlay makes silently.
    """
    from universalchess.menus.catalog.loader import get_localized_catalog

    english = load_catalog().get_node("connectivity.usb_gadget")["help"].lower()
    # "data" is what distinguishes the Pi's port; "charg" covers charge/charging.
    assert "data" in english, f"help does not identify the data port: {english}"
    assert "charg" in english, f"help does not rule out the charge port: {english}"
    assert "centaur" in english, f"help does not say whose charge port: {english}"

    spanish = get_localized_catalog("es").get_node("connectivity.usb_gadget")["help"].lower()
    assert "datos" in spanish, f"Spanish help does not identify the data port: {spanish}"
    assert "carga" in spanish, f"Spanish help does not rule out the charge port: {spanish}"

    french = get_localized_catalog("fr").get_node("connectivity.usb_gadget")["help"].lower()
    assert "données" in french, f"French help does not identify the data port: {french}"
    assert "charge" in french, f"French help does not rule out the charge port: {french}"


def test_usb_gadget_help_says_which_end_of_the_cable_to_reconnect():
    """The field help must say to reconnect at the board, not at a USB-C port.

    Why: measured on a DGT Centaur board, unplugging and reconnecting brings the
    link back at the board's own micro-USB socket and at a USB-A joint, but not at
    the computer's USB-C port -- that one can leave the host seeing no device at
    all. Nothing on the board can repair it: the gadget is armed once at boot and
    deliberately never rebound, and only the host can start enumeration. So the
    one thing that helps is telling the user which end to pull, in the same place
    the widget already tells them which socket to use.

    Failure: the help stops naming USB-C, or stops saying where to unplug, and the
    next person to reconnect at their laptop concludes the board has died. Spanish
    is asserted too, since a translation that drops the instruction leaves those
    users with only the failure.
    """
    from universalchess.menus.catalog.loader import get_localized_catalog

    english = load_catalog().get_node("connectivity.usb_gadget")["help"].lower()
    assert "usb-c" in english, f"help does not name the USB-C end: {english}"
    assert "unplug" in english, f"help does not say where to unplug: {english}"

    spanish = get_localized_catalog("es").get_node("connectivity.usb_gadget")["help"].lower()
    assert "usb-c" in spanish, f"Spanish help does not name the USB-C end: {spanish}"
    assert "desconéct" in spanish, f"Spanish help does not say where to unplug: {spanish}"

    french = get_localized_catalog("fr").get_node("connectivity.usb_gadget")["help"].lower()
    assert "usb-c" in french, f"French help does not name the USB-C end: {french}"
    assert "débranch" in french, f"French help does not say where to unplug: {french}"


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


def test_web_settings_field_ids_resolve_with_labels():
    """Every field the web Settings page renders must exist with a label.

    Why this test exists: the React Settings page (web-app/src/pages/Settings.tsx)
    renders these field labels and help strings directly from the catalog with no
    hardcoded fallback text. The fallback constants that previously mirrored
    menu.json were removed to keep the catalog the single source of truth, so the
    catalog is now a hard dependency for correct labels.

    How a regression manifests: one of these ids is removed or renamed in
    menu.json, so it is absent from the node index and this assertion fails naming
    the id -- instead of the live page rendering the raw id string (e.g.
    'field.player.type') as a label. The label presence check guards the same
    contract for a field that exists but loses its label.
    """
    catalog = load_catalog()
    nodes_by_id = {n["id"]: n for n in catalog.raw_menu()["nodes"]}
    # The exact field ids Settings.tsx looks up via fieldLabel/fieldHelp. Kept in
    # sync with that page; adding a catalog-driven field there means adding it
    # here so its absence fails this test rather than the rendered UI.
    required_field_ids = [
        "field.player.type",
        "field.player.name",
        "field.player.engine",
        "field.player.elo",
        "field.player.hand_brain_mode",
        "settings.timecontrol",
        "analysis.enabled",
        "analysis.engine",
        "field.display.show_board",
        "field.display.show_clock",
        "field.display.show_analysis",
        "field.display.show_graph",
        "field.display.sprites",
        "field.display.led_brightness",
        "field.display.pegasus_override_brightness",
        "field.sound.enabled",
        "field.sound.piece_events",
        "field.sound.game_events",
        "field.sound.errors",
        "field.sound.key_press",
        # Sleep Timer/Timezone/Language moved off per-field fieldLabel lookups onto
        # <MenuContainer> (group.system.device -> the shared system.* nodes), so
        # they are no longer looked up here. The update Channel + Auto Download
        # settings are now the SHARED board nodes (updates.channel/updates.auto),
        # rendered by UpdateManager via renderCatalogRow (no web-only duplicate);
        # the Database URI stays a bespoke card lookup.
        "updates.channel",
        "updates.auto",
        "field.system.database_uri",
    ]
    missing = [fid for fid in required_field_ids if fid not in nodes_by_id]
    assert not missing, f"web Settings references field ids absent from catalog: {missing}"
    unlabeled = [fid for fid in required_field_ids if not nodes_by_id[fid].get("label")]
    assert not unlabeled, f"web Settings field ids missing a label: {unlabeled}"


def test_web_settings_option_sets_present_and_non_empty():
    """Option sets the web Settings selects render must exist and be non-empty.

    Why this test exists: the player-type/hand-brain/time-control/sleep-timer/
    update-channel selects render these option sets from the catalog with no
    hardcoded fallback list (the FALLBACK_* arrays were removed). A missing or
    empty set renders an empty dropdown on the web with no values to choose.

    How a regression manifests: one of these names is removed from optionSets, so
    option_set returns an empty list and this fails naming the set -- instead of
    shipping an empty select.
    """
    catalog = load_catalog()
    required_option_sets = [
        "player_type",
        "hand_brain_mode",
        "time_control",
        "sleep_timer",
        "update_channel",
    ]
    empty = [name for name in required_option_sets if not catalog.option_set(name)]
    assert not empty, f"web Settings selects reference empty/missing option sets: {empty}"


def test_engine_pickers_are_provider_backed_selects():
    """Engine/ELO/analysis-engine nodes are provider-backed selects on both platforms.

    Why this test exists: these pickers were migrated from imperative board
    ``action`` sub-flows to ``select`` nodes whose runtime options come from a
    named provider and whose pick is written to the bound store. The board engine
    opens the provider list through the shared select path; the web renders the
    same provider-backed dropdown. A single source (type + provider + bind) now
    drives both platforms, so the redundant ``webType`` hint is gone.

    How a regression manifests: a node reverts to an ``action`` (the board would
    need the deleted handler and the web would render a blank gap), loses its
    ``provider`` (no named runtime source for the options), or loses its ``bind``
    (the pick has nowhere to persist) -- breaking the picker on one or both
    platforms.
    """
    catalog = load_catalog()
    expected_providers = {
        "field.player.engine": "installed_engines",
        "field.player.elo": "engine_levels",
        "analysis.engine": "installed_engines",
    }
    for node_id, provider in expected_providers.items():
        node = catalog.get_node(node_id)
        assert node["type"] == "select", f"{node_id} must be a select for both platforms"
        assert node.get("provider") == provider, f"{node_id} missing provider '{provider}'"
        assert "bind" in node, f"{node_id} must bind a store/key to persist the pick"


def test_sleep_timer_node_is_select_bound_to_seconds():
    """system.inactivity is a select over sleep_timer bound to system.sleep_seconds.

    Why this test exists: the Sleep Timer row was migrated from an imperative
    action to a data-driven ``select`` so the board changes the inactivity timeout
    through the shared ``sleep_timer`` option set (values in seconds), the same
    way the web does. How a regression manifests: the node reverts to an action or
    loses its optionSet/bind, so the board can no longer set the timeout via the
    engine and the saved value drifts from seconds.
    """
    catalog = load_catalog()
    node = catalog.get_node("system.inactivity")
    assert node["type"] == "select"
    assert node["optionSet"] == "sleep_timer"
    assert node["bind"] == {"store": "system", "key": "sleep_seconds"}


def test_sprites_node_is_one_cross_platform_radio_set():
    """Sprites is a single cross-platform dynamic node with an itemBind radio set.

    Why this test exists: the board's inline sprite list and the web's sprite
    radiogroup are the same setting (game.chess_sprites), so they share one
    catalog node rather than the old duplicate pair (board ``field.display.sprites``
    + web ``field.display.chess_sprites``). The node drives the runtime sheet list
    via the ``sprite_sheets`` provider and persists the picked sheet through
    ``itemBind``; both platforms read its label/help. How a regression manifests:
    the duplicate web node returns (two sources for one setting drift), the node
    loses its provider/itemBind (no list, or nothing persists), or it drops a
    platform so one UI stops rendering the picker.
    """
    catalog = load_catalog()
    assert not catalog.has_node("field.display.chess_sprites"), "duplicate web sprite node must be removed"
    node = catalog.get_node("field.display.sprites")
    assert node["type"] == "dynamic"
    assert node["provider"] == "sprite_sheets"
    assert node["itemBind"] == {"store": "game", "key": "chess_sprites"}
    assert set(node["platforms"]) == {"board", "web"}
    assert node.get("label"), "web renders the node's label/help, so it must have a label"


def test_option_label_resolves_value_to_catalog_label():
    """option_label must map a stored value to its catalog label from one source.

    Why this test exists: the board menus were migrated to resolve choice text
    (player type, time control, hand-brain mode, update channel) through this
    helper instead of private value->label maps, so the board and web render
    identical labels. This pins the contract those board builders depend on.

    How a regression manifests: a label drifts in menu.json (or the lookup
    breaks), so the wrong text is returned here -- which is exactly what would
    appear on the board. Int-valued lookups (time control) must match the
    string-authored values, the absent-value path must use the supplied default,
    and with no default the value itself must come back (never blank).
    """
    catalog = load_catalog()
    # Player type label is sourced here (board Players menu + summary).
    assert catalog.option_label("player_type", "hand_brain") == "Hand + Brain"
    # Time control values are held as ints by the board but authored as strings.
    assert catalog.option_label("time_control", 5) == "5 min (Blitz)"
    assert catalog.option_label("time_control", "10") == "10 min (Rapid)"
    # Absent value uses the explicit default; without one the value is echoed.
    assert catalog.option_label("player_type", "android", default="Android") == "Android"
    assert catalog.option_label("player_type", "android") == "android"


def test_packaged_catalog_exposes_lichess_account_type():
    """The shipped catalog must declare the Lichess online account type.

    Why this test exists: the multi-account system generates the Add Account form
    and enforces per-account storage from the catalog's ``accountTypes`` block,
    which is the single source of truth for what an online player type collects.
    This pins the Lichess entry (fields, secret flag, identity) the web/board and
    the account store depend on.

    How a regression manifests: the accountTypes block is dropped or the Lichess
    entry loses a field/flag, so account_type('lichess') is missing or has the
    wrong shape and the Add Account form/store can no longer be built.
    """
    catalog = load_catalog()
    assert catalog.has_account_type("lichess")
    assert not catalog.has_account_type("lichess_dev")
    lichess = catalog.account_type("lichess")
    assert lichess["label"] == "Lichess"
    assert lichess["icon"] == "lichess"
    # Identity is the account username, resolved by authenticating the token
    # (not typed by the user). Uniqueness is host+username inside the plugin.
    assert lichess["identityField"] == "username"
    assert lichess["identitySource"] == "resolved"
    fields = {f["key"]: f for f in lichess["fields"]}
    assert set(fields) == {"api_token", "range"}
    assert fields["api_token"]["secret"] is True
    assert fields["api_token"]["required"] is True
    assert fields["api_token"]["type"] == "password"
    # Listing GET /api/challenge needs challenge:read; omitting it mints a
    # token that authenticates and seeks but 403s on Challenges.
    help_text = fields["api_token"]["help"]
    assert "board:play" in help_text
    assert "challenge:read" in help_text
    assert "challenge:write" in help_text
    assert fields["range"].get("secret", False) is False
    assert fields["range"]["type"] == "text"
    hosts = {h["id"]: h for h in lichess["hosts"]}
    assert set(hosts) == {"org", "dev"}
    assert hosts["org"]["baseUrl"] == "https://lichess.org"
    assert hosts["dev"]["baseUrl"] == "https://lichess.dev"


def test_lichess_lobby_catalog_children_match_board_hierarchy():
    """players.lichess children: Account, Ongoing, Challenges, Seek New Game.

    Why: the web lobby tab walks this list so it stays in lockstep with the
    board lobby. Accounts is nested under Account, not a sibling. Rated,
    Clock, Color, and Seek sit under Seek New Game -- they govern every seek,
    including one from a pairing no Lichess slot describes, so a player
    slot could not hold them. Clock is the Board API list (Rapid, Classical,
    None) rather than the Game clock, which still offers Blitz. Color is White,
    Black, or Random rather than the Players colour control. Regression: a row
    drops, Play returns as a wrapper, Accounts sits on the lobby itself, or
    Rated, Clock, or Color goes back to the player card or the lobby root.
    """
    from universalchess.players.lichess.lobby import (
        build_lichess_menu_entries,
        build_lichess_seek_menu_entries,
    )

    catalog = load_catalog()
    node = catalog.get_node("players.lichess")
    assert node["label"] == "Lichess Lobby"
    assert node["boardLabel"] == "Lichess"
    assert node["visibleWhen"] == {
        "store": "main",
        "key": "lichess_available",
        "equals": True,
    }
    assert catalog.child_ids("players.lichess") == [
        "lichess.account",
        "lichess.ongoing",
        "lichess.challenges",
        "lichess.new_game",
    ]
    assert catalog.child_ids("lichess.account") == ["players.accounts"]
    assert catalog.child_ids("lichess.new_game") == [
        "field.lichess.rated",
        "field.lichess.clock",
        "field.lichess.color",
        "lichess.seek",
    ]
    rated = catalog.get_node("field.lichess.rated")
    assert rated["type"] == "toggle"
    assert rated["bind"] == {"store": "game", "key": "lichess_rated"}
    clock = catalog.get_node("field.lichess.clock")
    assert clock["type"] == "select"
    assert clock["bind"] == {"store": "game", "key": "lichess_clock"}
    assert clock["optionSet"] == "lichess_clock"
    color = catalog.get_node("field.lichess.color")
    assert color["type"] == "select"
    assert color["bind"] == {"store": "game", "key": "lichess_color"}
    assert color["optionSet"] == "lichess_color"
    from universalchess.players.lichess.match import LICHESS_CLOCKS, LICHESS_COLORS

    assert [o["value"] for o in catalog.option_set("lichess_clock")] == list(LICHESS_CLOCKS)
    assert "blitz_5_0" not in {o["value"] for o in catalog.option_set("lichess_clock")}
    assert [o["value"] for o in catalog.option_set("lichess_color")] == list(LICHESS_COLORS)
    # No player-scoped visibility: the lobby has no slot in context, and the
    # setting applies to every seek the board posts.
    assert "visibleWhen" not in rated
    assert "visibleWhen" not in clock
    assert "visibleWhen" not in color
    # The board lobby draws these very nodes, so a row's label and help are the
    # catalog's rather than a second copy of them that can drift or stay English.
    rows = {entry.key: entry for entry in build_lichess_menu_entries("alice")}
    for key, node_id in (
        ("Ongoing", "lichess.ongoing"),
        ("Challenges", "lichess.challenges"),
        ("NewGame", "lichess.new_game"),
    ):
        assert rows[key].label == catalog.get_node(node_id)["boardLabel"]
    assert rows["Ongoing"].help == catalog.get_node("lichess.ongoing")["help"]
    assert rows["Challenges"].help == catalog.get_node("lichess.challenges")["help"]
    seek_rows = {entry.key: entry for entry in build_lichess_seek_menu_entries()}
    assert seek_rows["Seek"].label == catalog.get_node("lichess.seek")["boardLabel"]
    assert catalog.get_node("lichess.account")["label"] == "Account"
    assert catalog.get_node("players.accounts")["label"] == "Accounts"
    assert "web" in catalog.get_node("players.accounts").get("platforms", ["board", "web"])


def test_lichess_catalog_hosts_match_plugin():
    """Catalog host URLs must match the Lichess plugin's host list.

    Why: the web form reads hosts from the catalog; berserk uses Python. Drift
    would send a token to the wrong server. Failure: an id or URL differs.
    """
    from universalchess.players.lichess.hosts import LICHESS_HOSTS

    catalog = load_catalog()
    catalog_hosts = [
        (h["id"], h["baseUrl"]) for h in catalog.account_type("lichess")["hosts"]
    ]
    plugin_hosts = [(h.id, h.base_url) for h in LICHESS_HOSTS]
    assert catalog_hosts == plugin_hosts


def test_lichess_is_an_account_type_not_a_player_type():
    """Lichess credentials are an account type; slots cannot be set to Lichess.

    Why this test exists: a Type picker option of Lichess left PLAY seeking and
    Positions blocked whenever a leftover slot named it. Online play starts from
    the lobby, which substitutes Human vs Lichess at start without writing slots.
    The account type remains so tokens can be stored.

    How a regression manifests: ``lichess`` returns to ``player_type``, or the
    account type is dropped so Add Account has nothing to create.
    """
    catalog = load_catalog()
    player_type_values = {o["value"] for o in catalog.option_set("player_type")}
    assert player_type_values == {"human", "engine", "hand_brain"}
    assert catalog.has_account_type("lichess")
    assert "lichess" not in player_type_values


def test_account_type_field_control_types_are_valid():
    """Every account field must use a control type the Add Account form can render.

    Why this test exists: the definition-driven Add Account form renders each
    field from its ``type``; an unsupported control type would render a blank
    input. This asserts the shipped catalog only uses supported controls.

    How a regression manifests: a field is authored with an unknown type (typo),
    which this catches before it ships as an unrenderable form row.
    """
    catalog = load_catalog()
    for entry in catalog.account_types():
        for fld in entry["fields"]:
            assert fld["type"] in {"text", "password"}, (
                f"account type '{entry['id']}' field '{fld['key']}' has "
                f"unsupported control type '{fld['type']}'"
            )


def _account_type_menu(entry: dict) -> dict:
    """Build a minimal valid catalog carrying one accountTypes entry.

    Includes a ``player_type`` option set (so catalogs that name ``playerType``
    can still be validated) and the 'lichess' icon so only the account-type-specific
    check under test can fail.
    """
    return {
        "roots": ["a"],
        "nodes": [{"id": "a", "type": "menu", "children": []}],
        "optionSets": {"player_type": [{"value": entry["id"], "label": entry["id"]}]},
        "accountTypes": [entry],
    }


_ACCOUNT_ICONS = {"version": 1, "icons": {"settings": {"d": "g"}, "lichess": {"d": "l"}}}


def test_unknown_account_field_type_raises(tmp_path):
    """An account field with an unrecognised control type must be rejected.

    The Add Account form renders each field by its control type; an unknown type
    yields a blank row. Validation must name the offending type rather than ship
    an invisible field.
    """
    entry = {
        "id": "lichess",
        "label": "Lichess",
        "icon": "lichess",
        "identityField": "username",
        "identitySource": "resolved",
        "fields": [{"key": "api_token", "label": "Token", "type": "passwrd"}],
    }
    menu_path, icons_path = _write(tmp_path, _account_type_menu(entry), _ACCOUNT_ICONS)
    with pytest.raises(CatalogError, match="unsupported.*type 'passwrd'"):
        load_catalog(menu_path, icons_path)


def test_duplicate_account_type_id_raises(tmp_path):
    """Two account types with the same id must be rejected.

    A duplicate id would shadow one definition in the type index, so the account
    store/form could resolve the wrong type. Validation must name the duplicate.
    """
    entry = {
        "id": "lichess",
        "label": "Lichess",
        "icon": "lichess",
        "identityField": "username",
        "identitySource": "resolved",
        "fields": [{"key": "api_token", "label": "Token", "type": "password"}],
    }
    menu = _account_type_menu(entry)
    menu["accountTypes"].append(dict(entry))
    menu_path, icons_path = _write(tmp_path, menu, _ACCOUNT_ICONS)
    with pytest.raises(CatalogError, match="duplicate account type id: lichess"):
        load_catalog(menu_path, icons_path)


def test_account_type_need_not_be_a_player_type(tmp_path):
    """An account type may exist without a matching player_type option.

    Why this test exists: Lichess is stored as credentials and started from the
    lobby; it is not a Players Type. Requiring every account type to be a slot
    type would block that split. How a regression manifests: load raises because
    ``chess_com`` is not in ``player_type``.
    """
    entry = {
        "id": "chess_com",
        "label": "Chess.com",
        "icon": "lichess",
        "identityField": "username",
        "identitySource": "resolved",
        "fields": [{"key": "api_token", "label": "Token", "type": "password"}],
    }
    menu = _account_type_menu(entry)
    menu["optionSets"]["player_type"] = [{"value": "human", "label": "Human"}]
    menu_path, icons_path = _write(tmp_path, menu, _ACCOUNT_ICONS)
    catalog = load_catalog(menu_path, icons_path)
    assert catalog.has_account_type("chess_com")


def test_account_type_entered_identity_must_be_a_field(tmp_path):
    """An 'entered' identity field must name one of the type's own fields.

    When identity is user-entered (not resolved by auth), the identity key must
    be a real input field or the store has no value to key uniqueness on.
    Validation must reject an identityField that is not among the fields.
    """
    entry = {
        "id": "lichess",
        "label": "Lichess",
        "icon": "lichess",
        "identityField": "handle",
        "identitySource": "entered",
        "fields": [{"key": "api_token", "label": "Token", "type": "password"}],
    }
    menu_path, icons_path = _write(tmp_path, _account_type_menu(entry), _ACCOUNT_ICONS)
    with pytest.raises(CatalogError, match="identityField 'handle'"):
        load_catalog(menu_path, icons_path)


def test_account_type_host_without_https_url_raises(tmp_path):
    """A host whose baseUrl is not https must be rejected.

    Why: the Add Account picker and berserk send the token to this URL. An
    http or missing URL would ship a picker that cannot be used safely.
    Failure: load_catalog accepts the entry.
    """
    entry = {
        "id": "lichess",
        "label": "Lichess",
        "icon": "lichess",
        "identityField": "username",
        "identitySource": "resolved",
        "fields": [{"key": "api_token", "label": "Token", "type": "password"}],
        "hosts": [{"id": "org", "label": "lichess.org", "baseUrl": "http://lichess.org"}],
    }
    menu_path, icons_path = _write(tmp_path, _account_type_menu(entry), _ACCOUNT_ICONS)
    with pytest.raises(CatalogError, match="baseUrl must be an https URL"):
        load_catalog(menu_path, icons_path)


def test_account_type_duplicate_host_id_raises(tmp_path):
    """Two hosts with the same id must be rejected.

    Why: the Server picker keys options by id; a duplicate would shadow a
    server. Failure: load_catalog accepts both rows.
    """
    entry = {
        "id": "lichess",
        "label": "Lichess",
        "icon": "lichess",
        "identityField": "username",
        "identitySource": "resolved",
        "fields": [{"key": "api_token", "label": "Token", "type": "password"}],
        "hosts": [
            {"id": "org", "label": "lichess.org", "baseUrl": "https://lichess.org"},
            {"id": "org", "label": "lichess.dev", "baseUrl": "https://lichess.dev"},
        ],
    }
    menu_path, icons_path = _write(tmp_path, _account_type_menu(entry), _ACCOUNT_ICONS)
    with pytest.raises(CatalogError, match="duplicate host id 'org'"):
        load_catalog(menu_path, icons_path)


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


def test_unknown_web_type_raises(tmp_path):
    """An unrecognised webType must be rejected.

    webType selects the web control for a node whose board ``type`` differs (e.g.
    an action rendered as a select on the web). A typo would make the web's
    CatalogField fall through to rendering nothing, leaving a blank row. Validation
    must name the bad value rather than ship an invisible control.
    """
    menu = {"roots": ["a"], "nodes": [{"id": "a", "type": "action", "webType": "selct"}]}
    menu_path, icons_path = _write(tmp_path, menu)
    with pytest.raises(CatalogError, match="unknown webType 'selct'"):
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
