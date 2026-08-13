"""Tests for the GET /api/menu-schema endpoint.

The web Settings UI fetches the shared menu catalog from this endpoint to render
tabs, fields, help tips, and option sets. These tests guard that the endpoint
returns the catalog (not an error) and that the payload carries the structures
the React renderer depends on.
"""

import importlib
import json
import sys

import pytest

from universalchess.tests.webapp_fixture import make_test_client

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from PIL import Image

# Mirror test_web_security: the app module builds a DB engine against /opt and
# opens a packaged logo at import time, neither of which exist in a checkout.
import universalchess.db.uri as _uri  # noqa: E402

_uri.get_database_uri = lambda: "sqlite:///:memory:"
_orig_image_open = Image.open
Image.open = lambda *a, **k: Image.new("RGBA", (8, 8))
try:
    if "universalchess.web.app" in sys.modules:
        webapp = importlib.reload(sys.modules["universalchess.web.app"])
    else:
        import universalchess.web.app as webapp  # noqa: E402
finally:
    Image.open = _orig_image_open


@pytest.fixture
def client():
    return make_test_client(webapp)


def test_menu_schema_returns_catalog(client):
    """The endpoint must return the catalog with nodes/sections/optionSets.

    If the loader failed or the route returned an error, the React Settings page
    would have no structure to render. This asserts a 200 with the top-level
    keys the renderer reads.
    """
    resp = client.get("/api/menu-schema")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert isinstance(data.get("nodes"), list) and data["nodes"]
    assert isinstance(data.get("sections"), list) and data["sections"]
    assert "optionSets" in data


def test_menu_schema_serves_generated_time_control_preset_options(client):
    """The payload carries a time_control_presets option set from the registry.

    The board fills the preset selector from a runtime provider, but the web
    renders from option sets. This endpoint injects the preset list (generated
    from the Python preset registry, the single source of truth) so the web
    dropdown stays in lockstep with the board without a second fetch. How a
    regression manifests: dropping the injection leaves the web preset dropdown
    empty; a stale hardcoded list would drift from the board's presets.
    """
    from universalchess.menus.time_control_presets import preset_options

    resp = client.get("/api/menu-schema")
    data = json.loads(resp.data)
    served = data["optionSets"].get("time_control_presets")
    assert served == preset_options()

    # The base catalog file has no such option set; it is generated at serve time
    # only, so the served payload must not have leaked back into the shared cache.
    from universalchess.menus.catalog import get_catalog

    assert "time_control_presets" not in get_catalog().raw_menu().get("optionSets", {})


def test_menu_schema_injects_full_timezone_option_set(client):
    """The payload carries a full IANA timezones option set at serve time.

    field.system.timezone references the "timezones" provider; the web selector
    needs the full zone list, injected here from the stdlib (single source), not
    authored in menu.json. How a regression manifests: dropping the injection
    leaves the web timezone dropdown empty, or the list omits common zones.
    """
    from universalchess.services.timezone_service import list_timezones

    resp = client.get("/api/menu-schema")
    data = json.loads(resp.data)
    served = data["optionSets"].get("timezones")
    assert served == [{"value": tz, "label": tz} for tz in list_timezones()]
    # UTC and a representative zone must be present so the selector is usable.
    values = {o["value"] for o in served}
    assert "UTC" in values and "Europe/Oslo" in values

    # Generated only at serve time; must not leak into the shared cached catalog.
    from universalchess.menus.catalog import get_catalog

    assert "timezones" not in get_catalog().raw_menu().get("optionSets", {})


def test_menu_schema_includes_known_field_help(client):
    """A known field's help text must be present in the payload.

    Help tips were migrated into the catalog; the web UI now reads them from
    here. This checks a representative field (player type) carries its help so a
    regression that drops help strings is caught.
    """
    resp = client.get("/api/menu-schema")
    data = json.loads(resp.data)
    by_id = {n["id"]: n for n in data["nodes"]}
    assert by_id["field.player.type"]["help"] == "Human, Engine, Hand+Brain, or Lichess"


def test_menu_schema_is_localized_to_device_language(client, monkeypatch):
    """The payload is localized to the device UI language.

    Why: the web Settings fields render their labels/help/option labels from this
    schema, so when the device language is Spanish the schema must arrive in
    Spanish (not English) for the web UI to match the board. How a regression
    manifests: the endpoint ignores the language and always serves English, so
    Settings fields stay English even with the device set to Spanish.
    """
    from universalchess.services import language_service

    monkeypatch.setattr(language_service, "get_language", lambda: "es")
    resp = client.get("/api/menu-schema")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    by_id = {n["id"]: n for n in data["nodes"]}
    # A representative field label and a section label must be Spanish.
    assert by_id["field.player.type"]["label"] == "Tipo de jugador"
    sections = {s["id"]: s["label"] for s in data["sections"]}
    assert sections["players"] == "Jugadores"


def test_menu_schema_fills_mdns_url_in_usb_gadget_client_description(client, monkeypatch):
    """USB Client mode help names this board's http://<hostname>.local/ URL.

    Why: a hardcoded ``http://dgt.local/`` example is wrong on every board that
    is not named ``dgt`` (this board is ``dgt-cm5-64``, and enable_usb_gadget.py
    already prints the card's real hostname). How a regression manifests: the
    Client description still contains ``dgt.local`` or the raw ``{mdns_url}``
    token.
    """
    monkeypatch.setattr(
        "universalchess.tls.current_mdns_name", lambda: "dgt-cm5-64.local"
    )
    monkeypatch.setattr(
        "universalchess.board.wireless_capability.get_wireless_capability",
        lambda: type("C", (), {"has_wifi": True})(),
    )
    resp = client.get("/api/menu-schema")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    by_value = {o["value"]: o for o in data["optionSets"]["usb_gadget_mode"]}
    description = by_value["client"]["description"]
    assert "http://dgt-cm5-64.local/" in description
    assert "dgt.local" not in description
    assert "{mdns_url}" not in description

    # The cached catalog must still hold the token so a rename refreshes correctly.
    from universalchess.menus.catalog import get_catalog
    cached = {
        o["value"]: o
        for o in get_catalog().raw_menu()["optionSets"]["usb_gadget_mode"]
    }
    assert "{mdns_url}" in cached["client"]["description"]


def test_menu_schema_omits_wifi_reach_on_boards_without_wifi(client, monkeypatch):
    """USB Off copy drops the Wi-Fi reach clause when the board has no Wi-Fi.

    Why: a plain Pi Zero has no wireless; the Off radio must not say to reach
    the board over Wi-Fi. How a regression manifests: Off still mentions Wi-Fi
    when ``has_wifi`` is false, or the raw token leaks.
    """
    monkeypatch.setattr(
        "universalchess.board.wireless_capability.get_wireless_capability",
        lambda: type("C", (), {"has_wifi": False})(),
    )
    resp = client.get("/api/menu-schema")
    assert resp.status_code == 200
    by_value = {o["value"]: o for o in resp.get_json()["optionSets"]["usb_gadget_mode"]}
    off = by_value["off"]["description"]
    assert off == "USB Ethernet is off."
    assert "Wi-Fi" not in off
    assert "{wifi_or_ethernet_reach" not in off
