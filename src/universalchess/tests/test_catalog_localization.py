"""Tests for menu-catalog localization (menus/catalog/loader.py).

The English ``menu.json`` is the authored source; each other locale ships a
sidecar overlay (``translations/<locale>.json``) keyed by node id / optionSet
name / section id. ``localize_catalog`` applies an overlay over a deep copy of
the English catalog, translating only the keys the overlay provides and leaving
everything else in English. ``get_localized_catalog`` caches a validated,
localized ``MenuCatalog`` per locale, and ``get_catalog`` returns the one for the
device's active UI language.

Each test states the regression it guards and how it would surface.
"""

import pytest

from universalchess.menus.catalog import loader
from universalchess.menus.catalog.loader import (
    get_localized_catalog,
    load_catalog,
    localize_catalog,
    load_overlay,
)


@pytest.fixture
def base_menu():
    """The parsed English catalog menu dict (validated on load)."""
    return load_catalog().raw_menu()


@pytest.fixture(autouse=True)
def reset_active_locale():
    """Clear the module's cached active locale around each test.

    ``get_catalog`` memoises the resolved device locale in a module global; left
    set, it would leak the locale chosen by one test into another (and into the
    other endpoint tests that expect English). Reset before and after so each
    test starts from an unresolved state and the default (English) is restored.
    """
    loader._active_locale = None
    yield
    loader._active_locale = None


# -- localize_catalog: overlay merge -----------------------------------------


def test_english_returns_source_unchanged(base_menu):
    """localize_catalog(menu, "en") returns the source object untouched.

    Why: English is authored directly in menu.json, so localizing to it must be
    a no-op with no copy (the shared cached catalog must not be duplicated or
    mutated). How a regression manifests: a returned copy (identity differs) or
    altered strings, wasting memory or diverging the English view.
    """
    assert localize_catalog(base_menu, "en") is base_menu


def test_overlay_translates_only_provided_node_keys(base_menu):
    """A node overlay overwrites only its listed keys; others stay English.

    Why: the overlay is sparse by design (missing keys fall back to English), so
    a node that translates only ``label`` must keep its English ``help``. How a
    regression manifests: an omitted key is blanked or the whole node is skipped,
    dropping either the translation or the English fallback.
    """
    overlay = {
        "nodes": {
            "main.play": {"label": "JUGAR", "label_in_progress": "REANUDAR"}
        }
    }
    localized = localize_catalog(base_menu, "es", overlay=overlay)
    by_id = {n["id"]: n for n in localized["nodes"]}
    play = by_id["main.play"]
    assert play["label"] == "JUGAR"
    assert play["label_in_progress"] == "REANUDAR"
    # help was not in the overlay, so it stays exactly as authored in English.
    source_play = {n["id"]: n for n in base_menu["nodes"]}["main.play"]
    assert play["help"] == source_play["help"]

    # The English source must be untouched (deep copy, not in-place mutation).
    assert source_play["label"] == "PLAY"


def test_overlay_translates_option_labels_by_value(base_menu):
    """An optionSet overlay maps value->label; unlisted values stay English.

    Why: options are matched by their stable ``value`` (not position), and only
    listed values are translated. How a regression manifests: a value keyed by
    the wrong field (label instead of value) translates nothing, or an unlisted
    value is blanked instead of kept in English.
    """
    overlay = {"optionSets": {"color": {"white": "Blancas"}}}
    localized = localize_catalog(base_menu, "es", overlay=overlay)
    color = {o["value"]: o["label"] for o in localized["optionSets"]["color"]}
    assert color["white"] == "Blancas"
    # black was not listed -> keeps its English label.
    assert color["black"] == "Black"


def test_overlay_option_object_can_carry_description(base_menu):
    """An option overlay may be {label, description} for per-mode help text.

    Why: USB gadget Off/Auto/Client/Shared show a long description under the select;
    a string-only overlay cannot translate those without dropping them. How a
    regression manifests: description stays English while the label is Spanish,
    or an object overlay is ignored and the label stays English.
    """
    overlay = {
        "optionSets": {
            "usb_gadget_mode": {
                "client": {
                    "label": "Cliente",
                    "description": "La Pi toma DHCP del host.",
                }
            }
        }
    }
    localized = localize_catalog(base_menu, "es", overlay=overlay)
    by_value = {o["value"]: o for o in localized["optionSets"]["usb_gadget_mode"]}
    assert by_value["client"]["label"] == "Cliente"
    assert by_value["client"]["description"] == "La Pi toma DHCP del host."
    # Unlisted mode keeps its English label and description (token still raw
    # until fill_option_runtime_placeholders runs at serve time).
    assert by_value["off"]["label"] == "Off"
    assert "USB Ethernet is off" in by_value["off"]["description"]


def test_fill_runtime_placeholders_names_this_boards_mdns_url():
    """``{mdns_url}`` becomes ``http://<hostname>.local/``, not a stock example.

    Why: Client-mode USB gadget copy must name the board the user is looking at.
    Failure: token left unsubstituted, or a hardcoded ``dgt.local`` slips back in.
    """
    from universalchess.menus.catalog.loader import fill_runtime_placeholders

    filled = fill_runtime_placeholders(
        "Reach the board at {mdns_url}.", mdns_name="dgt-cm5-64.local"
    )
    assert filled == "Reach the board at http://dgt-cm5-64.local/."
    assert fill_runtime_placeholders("no token") == "no token"
    # Hostnames from the OS can be mixed-case; URLs are shown lowercased.
    assert (
        fill_runtime_placeholders("at {mdns_url}", mdns_name="DGT-CM5-64.local")
        == "at http://dgt-cm5-64.local/"
    )


def test_fill_runtime_placeholders_wifi_reach_clause_gated_on_has_wifi():
    """Off-mode USB copy mentions Wi-Fi only when the board has Wi-Fi.

    Why: a plain Pi Zero has no wireless die; promising "reach over Wi-Fi" is a
    lie. How a regression manifests: the reach clause stays on ``has_wifi=False``,
    or the raw ``{wifi_or_ethernet_reach:...}`` token leaks into the UI.
    """
    from universalchess.menus.catalog.loader import fill_runtime_placeholders

    template = (
        "USB Ethernet is off."
        "{wifi_or_ethernet_reach: Reach the chess board only over Wi-Fi or Ethernet.}"
    )
    assert fill_runtime_placeholders(template, has_wifi=True) == (
        "USB Ethernet is off. Reach the chess board only over Wi-Fi or Ethernet."
    )
    assert fill_runtime_placeholders(template, has_wifi=False) == "USB Ethernet is off."
    # Unknown capability fails open (keep the clause).
    assert "Wi-Fi" in fill_runtime_placeholders(template, has_wifi=None)


def test_overlay_translates_section_labels(base_menu):
    """A sections overlay translates the web tab labels by id.

    Why: the web Settings tabs render from the section list; their labels must
    localize. How a regression manifests: the tab label stays English or a
    mismatched id leaves it untranslated.
    """
    overlay = {"sections": {"players": "Jugadores"}}
    localized = localize_catalog(base_menu, "es", overlay=overlay)
    labels = {s["id"]: s["label"] for s in localized["sections"]}
    assert labels["players"] == "Jugadores"
    assert labels["game"] == "Game"  # not overlaid -> English


def test_unknown_node_id_in_overlay_is_ignored(base_menu):
    """An overlay entry for a nonexistent node changes nothing and does not raise.

    Why: overlays are authored by hand and can reference a removed id; that must
    degrade gracefully rather than crash the menu. How a regression manifests: a
    KeyError/AttributeError, or a phantom node created from the overlay.
    """
    overlay = {"nodes": {"does.not.exist": {"label": "X"}}}
    localized = localize_catalog(base_menu, "es", overlay=overlay)
    assert [n["id"] for n in localized["nodes"]] == [n["id"] for n in base_menu["nodes"]]


def test_missing_overlay_falls_back_to_english(base_menu):
    """A locale with no overlay file returns the English catalog unchanged.

    Why: an untranslated (but supported) locale must render English rather than
    blank. How a regression manifests: an exception, or a returned copy that
    diverges from the source.
    """
    # "zz" has no translations/zz.json, so the file load returns None.
    assert load_overlay("zz") is None
    assert localize_catalog(base_menu, "zz") is base_menu


# -- real overlays: drift guard ----------------------------------------------


@pytest.mark.parametrize("locale", ["es", "fr", "de"])
def test_overlay_keys_reference_real_catalog_entries(base_menu, locale):
    """Every id/name in translations/<locale>.json resolves to a real catalog entry.

    Why: overlays are keyed by id and drift silently when the catalog is renamed
    or a node is removed -- the stale key then translates nothing and no error is
    raised. This pins each overlay to the catalog so a rename breaks the build
    here (a dead key surfaces as a failing assertion) instead of shipping a
    half-translated menu. How a regression manifests: a listed node id, optionSet
    name + value, or section id that no longer exists in menu.json.
    """
    overlay = load_overlay(locale)
    assert overlay is not None, f"{locale} overlay must exist"

    node_ids = {n["id"] for n in base_menu["nodes"]}
    for node_id in overlay.get("nodes", {}):
        assert node_id in node_ids, f"{locale}.json node id not in catalog: {node_id}"

    option_sets = base_menu["optionSets"]
    for name, value_labels in overlay.get("optionSets", {}).items():
        assert name in option_sets, f"{locale}.json optionSet not in catalog: {name}"
        catalog_values = {str(o["value"]) for o in option_sets[name]}
        for value in value_labels:
            assert value in catalog_values, f"{locale}.json {name} value not in catalog: {value}"

    section_ids = {s["id"] for s in base_menu["sections"]}
    for section_id in overlay.get("sections", {}):
        assert section_id in section_ids, f"{locale}.json section id not in catalog: {section_id}"

    account_ids = {a["id"] for a in base_menu.get("accountTypes", [])}
    for account_id in overlay.get("accountTypes", {}):
        assert account_id in account_ids, f"{locale}.json accountType id not in catalog: {account_id}"


# -- get_localized_catalog / get_catalog -------------------------------------


def test_get_localized_catalog_spanish_reflects_overlay_without_mutating_base():
    """get_localized_catalog("es") is Spanish; the English base is unaffected.

    Why: the localized catalog is derived from the shared English base; deriving
    it must not mutate that base (other callers, and English rendering, depend on
    it). How a regression manifests: the English base's labels turn Spanish
    (in-place mutation) or the Spanish catalog reads English (overlay not applied).
    """
    english = load_catalog()
    english_players_before = english.option_label("player_type", "human")

    spanish = get_localized_catalog("es")
    # A representative catalog string is Spanish...
    assert spanish.get_node("power.shutdown")["label"] == "Apagar"
    assert spanish.option_label("player_type", "human") == "Humano"

    # ...while the shared English base is untouched.
    assert get_localized_catalog("en").get_node("power.shutdown")["label"] == "Shutdown"
    assert english.option_label("player_type", "human") == english_players_before == "Human"


def test_get_localized_catalog_french_reflects_overlay_without_mutating_base():
    """get_localized_catalog("fr") is French; the English base is unaffected.

    Why: French is a shipped UI locale. A regression that applied only the
    Spanish overlay, or mutated the English base while deriving French, would
    show English on a French device or Spanish labels in English.
    """
    english = load_catalog()
    english_players_before = english.option_label("player_type", "human")

    french = get_localized_catalog("fr")
    assert french.get_node("power.shutdown")["label"] == "Éteindre"
    assert french.option_label("player_type", "human") == "Humain"

    assert get_localized_catalog("en").get_node("power.shutdown")["label"] == "Shutdown"
    assert english.option_label("player_type", "human") == english_players_before == "Human"


def test_get_localized_catalog_german_reflects_overlay_without_mutating_base():
    """get_localized_catalog("de") is German; the English base is unaffected.

    Why: German is a shipped UI locale added after Spanish and French. A
    regression that applied only the older overlays, or mutated the English base
    while deriving German, would show English on a German device.
    """
    english = load_catalog()
    english_players_before = english.option_label("player_type", "human")

    german = get_localized_catalog("de")
    assert german.get_node("power.shutdown")["label"] == "Herunterfahren"
    assert german.option_label("player_type", "human") == "Mensch"

    assert get_localized_catalog("en").get_node("power.shutdown")["label"] == "Shutdown"
    assert english.option_label("player_type", "human") == english_players_before == "Human"


def test_get_catalog_follows_active_device_language(monkeypatch):
    """get_catalog() returns the catalog for the device's active UI language.

    Why: board rendering reads the catalog through get_catalog(), which must
    reflect the persisted ui_language so the e-paper menu is drawn in the chosen
    language. How a regression manifests: get_catalog ignores the language and
    always returns English, so changing the language never affects the board.
    """
    from universalchess.services import language_service

    monkeypatch.setattr(language_service, "get_language", lambda: "es")
    loader.refresh_active_language()
    assert loader.get_catalog().get_node("power.shutdown")["label"] == "Apagar"

    monkeypatch.setattr(language_service, "get_language", lambda: "en")
    loader.refresh_active_language()
    assert loader.get_catalog().get_node("power.shutdown")["label"] == "Shutdown"
