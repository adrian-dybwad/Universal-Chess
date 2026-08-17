"""Tests for DisplayManager's hint-coach panel logic.

Why these tests exist
---------------------
When a hint is shown, the recommended move's coach statement is displayed in the
board-area coach panel (the board is hidden while it shows). These tests pin that
show/hide swaps the board correctly, that it never disturbs an active analysis
review (which owns the same panel), that a cleared alert (a move played) hides the
panel, and that the board is only restored when the board is enabled. A regression
would leave the board hidden after a hint, blank the board during review, or strand
the coach panel over the board after the hinted position is left.

The DisplayManager is built with `object.__new__` (no `__init__`) and only the
attributes these methods touch, so the test needs no hardware, engine, or widget
construction -- it exercises the pure panel-swap logic in isolation.
"""

import sys
import types
from unittest.mock import MagicMock

# Hardware/Linux-only modules must be mocked before importing display.py.
for _mod in ["spidev", "RPi", "RPi.GPIO", "gpiozero", "smbus", "smbus2", "bluetooth"]:
    sys.modules[_mod] = MagicMock()
for _mod in ["serial", "serial.tools", "serial.tools.list_ports"]:
    sys.modules[_mod] = MagicMock()

_board_pkg = types.ModuleType("DGTCentaurMods.board")
_board_pkg.board = MagicMock()
_board_pkg.centaur = MagicMock()
sys.modules["DGTCentaurMods.board"] = _board_pkg
sys.modules["DGTCentaurMods.board.board"] = _board_pkg.board
sys.modules["DGTCentaurMods.board.centaur"] = _board_pkg.centaur
sys.modules["DGTCentaurMods.board.logging"] = MagicMock()
sys.modules["DGTCentaurMods.board.settings"] = MagicMock()

from universalchess.managers.display import DisplayManager  # noqa: E402


class _FakeWidget:
    """Minimal show/hide + set_text/set_header widget stand-in."""

    def __init__(self):
        self.visible = False
        self.text = None
        self.header = "Coach"
        self.next_page_calls = 0

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def set_text(self, text):
        self.text = text

    def set_header(self, header):
        self.header = header

    def next_page(self):
        self.next_page_calls += 1
        return True


class _FakeAnalysis:
    """Analysis widget stand-in reporting a fixed selected ply."""

    def __init__(self, ply=None):
        self._ply = ply

    def selected_ply(self):
        return self._ply


def _manager(*, show_board=True, review_ply=None):
    """Bare DisplayManager with only the attributes the hint-coach methods use."""
    manager = object.__new__(DisplayManager)
    manager.coach_text_widget = _FakeWidget()
    manager.chess_board_widget = _FakeWidget()
    manager.chess_board_widget.visible = True  # board is up during play
    manager.analysis_widget = None
    manager.move_list_widget = _FakeAnalysis(review_ply)
    manager._show_board = show_board
    manager._hint_coach_active = False
    manager._review_coach_text = ""
    return manager


def test_show_then_hide_restores_board():
    # Showing a hint's coach statement hides the board and shows the panel under
    # the tip header; hiding it restores the board and the review header.
    # Regression: a missed restore would leave the e-paper board blank after the
    # hint is dismissed, or the panel would keep the tip header for the next review.
    manager = _manager()
    manager.show_hint_coach("Develops toward the center.")
    assert manager._hint_coach_active is True
    assert manager.coach_text_widget.visible is True
    assert manager.coach_text_widget.text == "Develops toward the center."
    assert manager.coach_text_widget.header == "Coach's Tip"
    assert manager.chess_board_widget.visible is False

    manager.hide_hint_coach()
    assert manager._hint_coach_active is False
    assert manager.coach_text_widget.visible is False
    assert manager.coach_text_widget.header == "Coach"
    assert manager.chess_board_widget.visible is True


def test_show_over_review_marks_tip_then_restores_review_on_hide():
    # A hint pressed while a move-review comment is showing must still show the tip
    # (clearly marked with the tip header), then restore the review comment (under
    # the review header, board still hidden) when dismissed. Regression: the hint
    # would be silently dropped, or the review comment would be lost after the tip.
    manager = _manager(review_ply=3)
    manager.coach_text_widget.visible = True  # review owns the panel
    manager.chess_board_widget.visible = False
    manager.set_coach_text("Review: this move overextends.")

    manager.show_hint_coach("Tip: castle to safety.")
    assert manager._hint_coach_active is True
    assert manager.coach_text_widget.visible is True
    assert manager.coach_text_widget.text == "Tip: castle to safety."
    assert manager.coach_text_widget.header == "Coach's Tip"
    assert manager.chess_board_widget.visible is False

    manager.hide_hint_coach()
    assert manager._hint_coach_active is False
    assert manager.coach_text_widget.visible is True
    assert manager.coach_text_widget.text == "Review: this move overextends."
    assert manager.coach_text_widget.header == "Coach"
    assert manager.chess_board_widget.visible is False


def test_late_review_result_does_not_overwrite_visible_tip():
    # A review result arriving while a tip is shown must be recorded but not blitted
    # over the visible tip; it is restored when the tip is hidden. Regression: the
    # tip text would be clobbered mid-display by the async review push.
    manager = _manager(review_ply=3)
    manager.show_hint_coach("Tip: develop the knight.")
    manager.set_coach_text("Review arrived late.")
    assert manager.coach_text_widget.text == "Tip: develop the knight."

    manager.hide_hint_coach()
    assert manager.coach_text_widget.text == "Review arrived late."
    assert manager.coach_text_widget.header == "Coach"


def test_empty_text_is_ignored():
    # An empty statement (nothing to coach) must not blank the board.
    manager = _manager()
    manager.show_hint_coach("")
    assert manager._hint_coach_active is False
    assert manager.chess_board_widget.visible is True


def test_hide_is_noop_when_not_active():
    # hide_hint_coach must not touch the board when no hint panel is active, so it
    # never disturbs an unrelated board/review state.
    manager = _manager()
    manager.chess_board_widget.visible = True
    manager.hide_hint_coach()
    assert manager.chess_board_widget.visible is True
    assert manager.coach_text_widget.visible is False


def test_alert_cleared_hides_panel():
    # A played move clears the alert; the hint coach panel must clear with it so it
    # does not linger over the board in the new position.
    manager = _manager()
    manager.show_hint_coach("Center control.")
    manager._on_hint_alert_cleared()
    assert manager._hint_coach_active is False
    assert manager.coach_text_widget.visible is False
    assert manager.chess_board_widget.visible is True


def test_board_not_restored_when_board_disabled():
    # When the board is disabled in settings, hiding the hint panel must not force
    # the board on (which would override the user's board-off preference).
    manager = _manager(show_board=False)
    manager.show_hint_coach("Center control.")
    manager.hide_hint_coach()
    assert manager.coach_text_widget.visible is False
    assert manager.chess_board_widget.visible is False


def test_review_taking_over_restores_review_comment_on_hide():
    # If an analysis-review selection takes over the panel after a hint was shown,
    # hiding the hint must restore the review comment (review owns the hidden-board
    # layout now) rather than the board.
    manager = _manager()
    manager.show_hint_coach("Center control.")
    manager._review_coach_text = "Review: solid development."
    manager.move_list_widget = _FakeAnalysis(ply=2)  # review selected a ply meanwhile
    manager.hide_hint_coach()
    assert manager._hint_coach_active is False
    assert manager.chess_board_widget.visible is False
    assert manager.coach_text_widget.visible is True
    assert manager.coach_text_widget.text == "Review: solid development."
    assert manager.coach_text_widget.header == "Coach"


def test_page_coach_text_pages_when_statement_visible():
    # OK (checkmark) while a coach statement occupies the panel must page the
    # statement (delegating to the widget's next_page) and report True so the key
    # handler skips the full-screen refresh. A regression returning False would
    # flash a full refresh instead of turning the page.
    manager = _manager()
    manager.coach_text_widget.visible = True
    manager.coach_text_widget.text = "A long coaching remark spanning pages."
    assert manager.page_coach_text() is True
    assert manager.coach_text_widget.next_page_calls == 1


def test_page_coach_text_ignored_when_panel_hidden():
    # With the coach panel hidden (board shown), OK must fall through to the full
    # refresh: page_coach_text returns False and never touches next_page. A
    # regression paging a hidden panel would suppress the intended refresh.
    manager = _manager()
    manager.coach_text_widget.visible = False
    manager.coach_text_widget.text = "Stale text left on a hidden panel."
    assert manager.page_coach_text() is False
    assert manager.coach_text_widget.next_page_calls == 0


def test_page_coach_text_ignored_when_no_statement():
    # A visible-but-empty panel has nothing to page; page_coach_text returns False
    # so OK still forces the ghosting-clearing full refresh. A regression paging an
    # empty panel would swallow that refresh.
    manager = _manager()
    manager.coach_text_widget.visible = True
    manager.coach_text_widget.text = ""
    assert manager.page_coach_text() is False
    assert manager.coach_text_widget.next_page_calls == 0


def test_page_coach_text_ignored_when_no_widget():
    # In non-analysis layouts there is no coach panel at all; page_coach_text must
    # tolerate a None widget and return False rather than raising.
    manager = _manager()
    manager.coach_text_widget = None
    assert manager.page_coach_text() is False
