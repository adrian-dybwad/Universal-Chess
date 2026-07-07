"""Tests for selecting a menu entry whose key is the empty string.

An entry key of ``""`` is a legitimate selectable value -- the Basic time-control
preset persists ``game.time_control_preset == ""`` -- not a "no selection"
sentinel. Two places used to coerce a falsy selected key to ``"BACK"``
(``icon_menu`` TICK handling and ``MenuManager.show_menu``), which silently
turned a Basic pick into a back-out so the value never persisted.

These tests pin that an empty-string selection is delivered as an empty-string
result at both layers. Only a truly absent selection (``None``) means back.
"""

import threading

from universalchess.epaper.icon_menu import IconMenuEntry, IconMenuWidget
from universalchess.managers.menu import MenuManager


def _entries():
    """A list whose first entry has an empty key (the Basic preset analog)."""
    return [
        IconMenuEntry(key="", label="Basic", icon_name="timer_checked"),
        IconMenuEntry(key="blitz_5_3", label="5|3 Blitz", icon_name="timer_checked"),
    ]


def test_widget_tick_on_empty_key_entry_reports_empty_not_back():
    """Confirming an empty-key entry must record "" as the result, not "BACK".

    Why: the Basic preset row's key is "", and TICK on it must select it (persist
    "") like any other row. How a regression manifests: if the TICK handler treats
    an empty key as falsy and stores "BACK", selecting Basic backs out of the list
    and the preset is never cleared.
    """
    from universalchess.board import board

    widget = IconMenuWidget(0, 0, 128, 280, lambda *a, **k: None,
                            entries=_entries(), selected_index=0)
    widget._active = True

    widget.handle_key(board.Key.TICK)

    assert widget._selection_result == ""  # the empty key, not "BACK"


class _FakeWidget:
    """Stand-in for IconMenuWidget that returns a scripted result immediately."""

    result = None

    def __init__(self, *args, entries=None, selected_index=0, **kwargs):
        self.entries = entries or []
        self.selected_index = selected_index
        self._selection_event = threading.Event()
        self._selection_result = None

    def activate(self):
        self._selection_result = _FakeWidget.result
        self._selection_event.set()

    def deactivate(self):
        pass

    def get_selected_help(self):
        return None


class _FakeDisplayManager:
    def clear_widgets(self, addStatusBar=True):
        return None

    def add_widget(self, widget):
        return None

    def remove_widget(self, widget):
        return None

    def update(self, full=False, immediate=False):
        return None


class _FakeBoard:
    display_manager = _FakeDisplayManager()


def test_show_menu_returns_empty_key_selection_not_back(monkeypatch):
    """show_menu must surface an empty-string selection as key "", not "BACK".

    Why: the board select loop persists MenuSelection.key; an empty key (Basic)
    must reach handle_selection so it writes "". How a regression manifests: if
    show_menu coerces the widget's "" result to "BACK", run_menu_loop returns
    before handle_selection runs and the preset is never set to "".
    """
    monkeypatch.setattr("universalchess.managers.menu.IconMenuWidget", _FakeWidget)
    _FakeWidget.result = ""

    manager = MenuManager()
    manager.set_board(_FakeBoard())

    result = manager.show_menu(_entries(), initial_index=0)

    assert result.key == ""  # empty selection preserved
    assert result.result_type is None  # not a BACK/exit result


def test_show_menu_returns_back_when_no_selection(monkeypatch):
    """A truly absent selection (None) must still resolve to BACK.

    Why: guards that distinguishing None from "" did not break the genuine
    no-selection case. How a regression manifests: if None stopped mapping to
    BACK, an interrupted wait would return a bogus empty selection.
    """
    monkeypatch.setattr("universalchess.managers.menu.IconMenuWidget", _FakeWidget)
    _FakeWidget.result = None

    manager = MenuManager()
    manager.set_board(_FakeBoard())

    result = manager.show_menu(_entries(), initial_index=0)

    assert result.key == "BACK"
