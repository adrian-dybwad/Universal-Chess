"""Tests for the e-paper Chromecast menu.

The Chromecast menu owns both stream device selection and the display-source
toggle used by /video. These tests pin the source toggle because the same option
also appears on the web Connectivity page; if the e-paper row disappears or
falls through into discovery, the two UIs drift.
"""

import sys
import types
from dataclasses import dataclass
from unittest.mock import MagicMock


@dataclass
class _StubIconMenuEntry:
    key: str
    label: str
    icon_name: str
    enabled: bool = True
    selectable: bool = True
    height_ratio: float = 1.0
    max_height: int | None = None
    icon_size: int | None = None
    layout: str = "horizontal"
    font_size: int = 16
    bold: bool = False
    help: str | None = None


epaper_module = types.ModuleType("universalchess.epaper")
icon_menu_module = types.ModuleType("universalchess.epaper.icon_menu")
icon_menu_module.IconMenuEntry = _StubIconMenuEntry
icon_menu_module.IconMenuWidget = MagicMock()
epaper_module.IconMenuEntry = _StubIconMenuEntry
epaper_module.IconMenuWidget = MagicMock()
epaper_module.SplashScreen = MagicMock()
sys.modules.setdefault("universalchess.epaper", epaper_module)
sys.modules.setdefault("universalchess.epaper.icon_menu", icon_menu_module)

from universalchess.menus.chromecast_menu import handle_chromecast_menu


class _FakeCastService:
    active_devices = []


class _FakeDisplay:
    def clear_widgets(self):
        pass

    def add_widget(self, widget):
        return None

    def update(self):
        pass


class _FakeBoard:
    SOUND_GENERAL = "general"
    display_manager = _FakeDisplay()

    def beep(self, sound):
        pass


def test_source_toggle_is_shown_and_toggles_without_discovery(monkeypatch):
    """Selecting the Chromecast source row toggles the persisted source setting.

    Regression manifestation: the e-paper menu only lists device actions, so the
    web has a Chromecast source checkbox but the board menu cannot change it; or
    selecting the row continues into discovery instead of saving the setting.
    """
    shown_entries = []
    saved_values = []

    def show_menu(entries):
        shown_entries.append(entries)
        return "SOURCE"

    def fail_discovery(*args, **kwargs):
        raise AssertionError("source toggle must not start Chromecast discovery")

    monkeypatch.setitem(
        sys.modules,
        "pychromecast",
        types.SimpleNamespace(get_chromecasts=fail_discovery),
    )

    handle_chromecast_menu(
        show_menu=show_menu,
        board=_FakeBoard(),
        log=MagicMock(),
        get_chromecast_service=lambda: _FakeCastService(),
        get_use_live_board=lambda: True,
        set_use_live_board=lambda value: saved_values.append(value),
    )

    assert shown_entries, "the Chromecast menu should render an action list"
    assert shown_entries[0][0].key == "SOURCE"
    assert shown_entries[0][0].label == "Stream Board Only"
    assert shown_entries[0][0].icon_name == "checkbox_checked"
    assert saved_values == [False]
