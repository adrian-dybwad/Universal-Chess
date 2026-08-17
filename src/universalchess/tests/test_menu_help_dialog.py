"""Tests for the e-paper menu help dialog flow.

Pressing HELP in a menu shows the focused entry's catalog help tip as a modal,
then re-displays the menu instead of exiting. These tests cover the three parts
that make that work: the focused entry's help is reachable (get_selected_help),
MenuManager.show_menu routes HELP to the injected presenter and keeps looping,
and the dialog widget wraps/renders its text without error.
"""

import sys
import threading
import time
from unittest.mock import MagicMock

import pytest
from PIL import Image

# The dialog resolves the board's Key constants lazily (as icon_menu does) when a
# key is handled, so the serial stack is stubbed to let that import succeed off
# hardware. PIL is deliberately not mocked: these tests read rendered output.
for _mod in ("serial", "serial.tools", "serial.tools.list_ports"):
    sys.modules.setdefault(_mod, MagicMock())

from universalchess.epaper.help_dialog import (  # noqa: E402 - after the serial stub above
    HelpDialogWidget,
)
from universalchess.i18n import t  # noqa: E402 - after the serial stub above
from universalchess.epaper.icon_menu import (  # noqa: E402 - after the serial stub above
    IconMenuEntry,
    IconMenuWidget,
)
from universalchess.managers.menu import MenuManager  # noqa: E402 - after the serial stub above
from universalchess.menus.catalog.loader import (  # noqa: E402 - after the serial stub above
    get_localized_catalog,
    load_catalog,
)


def _board():
    """The board module, for its Key constants."""
    from universalchess.board import board  # noqa: PLC0415 - deferred so the stub above is in place

    return board


def _entries():
    return [
        IconMenuEntry(key="Players", label="Players", icon_name="players",
                      help="Configure players."),
        IconMenuEntry(key="Positions", label="Positions", icon_name="positions",
                      help="Set up a position."),
    ]


def test_get_selected_help_returns_focused_entry_help():
    """The widget must expose the focused entry's help text.

    The help dialog shows whatever get_selected_help returns for the current
    selection. If it returned the wrong entry's help (or None), the dialog would
    show the wrong/empty tip. Focus index 1 and assert its help.
    """
    widget = IconMenuWidget(0, 0, 128, 280, lambda *_a, **_k: None,
                            entries=_entries(), selected_index=1)
    assert widget.get_selected_help() == "Set up a position."


class _FakeWidget:
    """Stand-in for IconMenuWidget that returns scripted results immediately.

    Each activate() pops the next (selected_index, result_key) from the shared
    script and signals selection, so show_menu's wait returns without real input.
    """

    def __init__(self, *_args, entries=None, selected_index=0, **_kwargs):
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
    """Stand-in for the framework display manager, mirroring its call signatures."""

    def clear_widgets(self, addStatusBar=True):  # noqa: FBT002, N803, ARG002 - mirrors the framework's own signature
        return None

    def add_widget(self, _widget):
        return None

    def remove_widget(self, _widget):
        return None

    def update(self, *_args, **_kwargs):
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
    long_text = ("This is a fairly long help tip that must wrap across multiple lines on "
                 "the narrow e-paper display without raising.")
    widget = HelpDialogWidget(lambda *_a, **_k: None, title="Display\n& Sound", body=long_text)
    sprite = Image.new("L", (128, 296), 255)
    widget.render(sprite)  # Should not raise.
    # Dismiss signaling works for the events-thread path.
    assert widget.wait_for_dismiss(timeout=0.01) is False
    widget.dismiss()
    assert widget.wait_for_dismiss(timeout=0.01) is True


def _dialog(body: str) -> HelpDialogWidget:
    """A help dialog holding ``body``, with a no-op update callback."""
    return HelpDialogWidget(lambda *_a, **_k: None, title="USB Gadget", body=body)


def _canvas(widget: HelpDialogWidget) -> Image.Image:
    """The dialog rendered through the path the display manager uses."""
    canvas = Image.new("1", (128, 296), 255)
    widget.draw_on(canvas, 0, 0)
    return canvas


def _long_tip() -> str:
    """A tip that certainly spans more than one page of the dialog."""
    return " ".join(f"word{index}" for index in range(120))


def test_a_short_tip_is_one_page_and_says_any_button_dismisses():
    """A tip that fits must not acquire a paging affordance.

    Why this test exists: paging is for the tips that need it. If a one-page tip
    reported two pages, the reader would be sent to a blank second page; if the
    instruction line changed, it would name buttons that do nothing here.

    Failure: page_count is not 1, or the instruction is the paged wording.
    """
    widget = _dialog("Set up a position.")

    assert widget.page_count == 1
    assert widget.instruction == t("about.press_any_button")


def test_a_long_tip_pages_instead_of_being_cut_off():
    """A tip longer than the panel must split into pages that cover all of it.

    Why this test exists: this is the defect. The dialog used to draw every
    wrapped line from the top, so a long tip ran over the instruction line and
    then off the bottom of the screen, where PIL clips it silently -- the reader
    saw a tip that stopped mid-sentence with no sign there was more. The USB
    Gadget Shared and Auto descriptions are 25 and 23 lines against a panel that
    holds 13.

    Failure: one page (the tail is lost again), or pages that do not reconstruct
    the tip, which is a dropped or duplicated boundary line.
    """
    tip = _long_tip()
    widget = _dialog(tip)

    assert widget.page_count > 1, "the fixture must exceed one page"
    seen = [widget.page_text]
    while widget.handle_key(_board().Key.DOWN) and widget.current_page != 1:
        seen.append(widget.page_text)

    assert " ".join(page.replace("\n", " ") for page in seen) == tip
    assert widget.instruction == t("help.multi_page_instruction")


def test_up_and_down_page_the_tip_and_wrap_like_the_rest_of_the_board():
    """UP/DOWN must page, cycling, and must not dismiss.

    Why this test exists: UP/DOWN are what page the menu selection, the keyboard
    layouts and the analysis pages, so the help dialog pages on the same keys and
    wraps as they do -- neither key is ever a dead end. Before this, every key
    dismissed, which is why a long tip could not be read at all.

    Failure: an arrow closes the dialog (the reader loses the tip trying to read
    it), or the cursor stops at an end so the last page has no way back.
    """
    board = _board()
    widget = _dialog(_long_tip())
    last_page = widget.page_count

    assert widget.handle_key(board.Key.DOWN) is True
    assert widget.current_page == 2
    assert widget.handle_key(board.Key.UP) is True
    assert widget.current_page == 1

    # UP from the first page wraps to the last, as the analysis pager does.
    assert widget.handle_key(board.Key.UP) is True
    assert widget.current_page == last_page
    assert widget.wait_for_dismiss(timeout=0.01) is False, "paging must not dismiss"


def test_any_other_button_dismisses():
    """A key that is not UP or DOWN must close the dialog.

    Why this test exists: the dialog is modal and consumes the next key, so the
    only way back to the menu is through it. Paging must not take that away.

    Failure: the dialog stays open and the menu is unreachable until the idle
    timeout.
    """
    board = _board()
    widget = _dialog(_long_tip())

    assert widget.handle_key(board.Key.TICK) is True
    assert widget.wait_for_dismiss(timeout=0.01) is True


def test_a_single_page_tip_dismisses_on_any_key_including_the_arrows():
    """With nothing to page, UP/DOWN must dismiss like every other key.

    Why this test exists: the instruction on a one-page tip says any button, so
    an arrow that silently did nothing would contradict what is on screen.

    Failure: an arrow is swallowed and the dialog appears stuck.
    """
    board = _board()
    widget = _dialog("Set up a position.")

    assert widget.handle_key(board.Key.DOWN) is True
    assert widget.wait_for_dismiss(timeout=0.01) is True


def test_the_idle_timeout_restarts_on_a_page_turn():
    """Turning a page must restart the wait, not run down the original one.

    Why this test exists: the dialog closes itself after an idle period so a
    board left on a help screen returns to the menu. Measured from when the
    dialog opened, that same timer would close it mid-read on the second page of
    a long tip -- the paging would work and the text would still be unreadable.

    The numbers: with a 0.3s idle window and a page turn at 0.15s, a timer that
    restarts returns at ~0.45s and one that does not returns at ~0.3s, so the
    assertion is that it lasted longer than the window it started with.

    Failure: the wait returns after the original window despite the page turn.
    """
    board = _board()
    widget = _dialog(_long_tip())
    idle_seconds = 0.3
    turn_after = 0.15

    turner = threading.Timer(turn_after, lambda: widget.handle_key(board.Key.DOWN))
    started = time.monotonic()
    turner.start()
    try:
        dismissed = widget.wait_for_dismiss(timeout=idle_seconds)
    finally:
        turner.cancel()
    elapsed = time.monotonic() - started

    assert dismissed is False, "nothing dismissed it, so it must time out"
    assert elapsed >= turn_after + idle_seconds, (
        f"closed after {elapsed:.2f}s; the page turn at {turn_after}s must restart "
        f"the {idle_seconds}s idle window"
    )


@pytest.mark.parametrize("language", ["en", "es", "fr", "de"])
def test_the_longest_shipped_help_text_is_readable_page_by_page(language):
    """Every catalog tip and option description must render within the panel.

    Why this test exists: the texts that overflowed are shipped copy, and copy
    grows -- a Spanish or French rendering of a tip that fits in English is routinely two
    lines longer. This walks the real catalog rather than a fixture, so the
    guarantee covers what a board actually shows, and asserts the invariant that
    replaced the overflow: whatever the length, the body is split into pages that
    each fit above the instruction line.

    Failure: a page holds more lines than the panel can draw, which is how text
    silently disappeared off the bottom before.
    """
    catalog = load_catalog() if language == "en" else get_localized_catalog(language)
    texts = [node["help"] for node in catalog.raw_menu()["nodes"] if node.get("help")]
    for options in catalog.raw_menu().get("optionSets", {}).values():
        texts.extend(
            option["description"]
            for option in options
            if isinstance(option, dict) and option.get("description")
        )
    assert texts, "no help text found in the catalog"

    for text in texts:
        widget = _dialog(text)
        for _ in range(widget.page_count):
            lines = widget.page_text.split("\n")
            assert len(lines) <= widget.lines_per_page, (
                f"[{language}] page {widget.current_page} of this text holds "
                f"{len(lines)} lines, more than the {widget.lines_per_page} the "
                f"panel can draw: {text}"
            )
            widget.next_page()
