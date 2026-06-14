"""Tests for the e-paper menu help dialog flow.

Pressing HELP in a menu shows the focused entry's catalog help tip as a modal,
then re-displays the menu instead of exiting. These tests cover the three parts
that make that work: the focused entry's help is reachable (get_selected_help),
MenuManager.show_menu routes HELP to the injected presenter and keeps looping,
and the dialog widget wraps/renders its text without error.
"""

import threading

from PIL import Image

from universalchess.epaper.icon_menu import IconMenuEntry, IconMenuWidget
from universalchess.epaper.help_dialog import HelpDialogWidget
from universalchess.managers.menu import MenuManager


def _entries():
    return [
        IconMenuEntry(key="Players", label="Players", icon_name="players", help="Configure players."),
        IconMenuEntry(key="Positions", label="Positions", icon_name="positions", help="Set up a position."),
    ]


def test_get_selected_help_returns_focused_entry_help():
    """The widget must expose the focused entry's help text.

    The help dialog shows whatever get_selected_help returns for the current
    selection. If it returned the wrong entry's help (or None), the dialog would
    show the wrong/empty tip. Focus index 1 and assert its help.
    """
    widget = IconMenuWidget(0, 0, 128, 280, lambda *a, **k: None, entries=_entries(), selected_index=1)
    assert widget.get_selected_help() == "Set up a position."


class _FakeWidget:
    """Stand-in for IconMenuWidget that returns scripted results immediately.

    Each activate() pops the next (selected_index, result_key) from the shared
    script and signals selection, so show_menu's wait returns without real input.
    """

    def __init__(self, *args, entries=None, selected_index=0, **kwargs):
        self.entries = entries or []
        self.selected_index = selected_index
        self._selection_event = threading.Event()
        self._selection_result = None
        self._script = _FakeWidget.script

    def activate(self):
        idx, result = self._script.pop(0)
        self.selected_index = idx
        self._selection_result = result
        self._selection_event.set()

    def deactivate(self):
        pass

    def get_selected_help(self):
        if self.entries and self.selected_index < len(self.entries):
            return self.entries[self.selected_index].help
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


def test_show_menu_routes_help_to_presenter_then_returns_selection(monkeypatch):
    """HELP must invoke the presenter with the focused tip, then keep looping.

    Scripts two displays: first HELP at index 1, then a real "Positions"
    selection. The presenter must be called once with the focused entry's label
    and help, and show_menu must ultimately return the post-help selection (not
    HELP). A regression where HELP exits the menu would return HELP here instead.
    """
    monkeypatch.setattr("universalchess.managers.menu.IconMenuWidget", _FakeWidget)
    _FakeWidget.script = [(1, "HELP"), (1, "Positions")]

    calls = []
    manager = MenuManager()
    manager.set_board(_FakeBoard())
    manager.set_help_presenter(lambda title, body: calls.append((title, body)))

    result = manager.show_menu(_entries(), initial_index=0)

    assert calls == [("Positions", "Set up a position.")]
    assert result.key == "Positions"
    assert result.result_type is None


def test_show_menu_without_presenter_returns_help(monkeypatch):
    """Without a presenter, HELP must propagate to the caller (legacy behavior).

    The manager stays usable without help wiring (e.g. tests). A single HELP
    result must surface as a HELP MenuSelection rather than looping forever.
    """
    monkeypatch.setattr("universalchess.managers.menu.IconMenuWidget", _FakeWidget)
    _FakeWidget.script = [(0, "HELP")]

    manager = MenuManager()
    manager.set_board(_FakeBoard())

    result = manager.show_menu(_entries(), initial_index=0)
    assert result.key == "HELP"


def test_help_dialog_renders_and_wraps_long_text():
    """The dialog must render long help text without raising.

    Help tips can exceed one line; the widget word-wraps to the display width.
    A wrapping bug (e.g. a word wider than the line) would raise during render.
    Renders a long tip onto a real sprite to exercise the wrap path.
    """
    long_text = "This is a fairly long help tip that must wrap across multiple lines on the narrow e-paper display without raising."
    widget = HelpDialogWidget(lambda *a, **k: None, title="Display\n& Sound", body=long_text)
    sprite = Image.new("L", (128, 296), 255)
    widget.render(sprite)  # Should not raise.
    # Dismiss signaling works for the events-thread path.
    assert widget.wait_for_dismiss(timeout=0.01) is False
    widget.dismiss()
    assert widget.wait_for_dismiss(timeout=0.01) is True
