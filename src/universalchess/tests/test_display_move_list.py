"""Tests that DisplayManager always surfaces the move list on UP/DOWN.

Why these tests exist
---------------------
The move list is its own widget, independent of GameAnalysisWidget. Show
Analysis off hid the analysis widget; Live Analysis off never created it;
either way UP/DOWN was dropped (`step_analysis_selection` returned False)
and the list never appeared. These tests pin that the arrows always step
the move-list widget, show it while a ply is highlighted, and hide it when
wrapping back to the board -- whether analysis is hidden, absent, or shown.

How a regression manifests
--------------------------
step_analysis_selection returns False when analysis is off (arrows fall
through), the move list stays hidden after a step, wrapping back leaves it
on screen, or is_move_review_active still keys off analysis_widget so
long-press OK cannot open takeback with analysis off.
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
    """Minimal show/hide widget stand-in."""

    def __init__(self, *, visible=True):
        self.visible = visible

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False


class _FakeMoveList:
    """Move-list stand-in with the selection API DisplayManager drives."""

    def __init__(self, plies=4):
        self.visible = False
        self._selection = 0
        self._plies = plies
        self._callback = None

    def set_selection_change_callback(self, callback):
        self._callback = callback

    def step_selection(self, direction):
        total = 1 + self._plies
        new = (self._selection + direction) % total
        if new != self._selection:
            self._selection = new
            if self._callback is not None:
                self._callback(self._selection)

    def select_ply(self, ply):
        target = 0 if ply <= 0 else min(ply, self._plies)
        if target != self._selection:
            self._selection = target
            if self._callback is not None:
                self._callback(self._selection)

    def selected_ply(self):
        return None if self._selection == 0 else self._selection

    def num_plies(self):
        return self._plies

    @property
    def selection(self):
        return self._selection

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False


def _bare_display_manager(*, show_analysis=True, analysis_present=True, plies=4):
    """DisplayManager with only the attributes the move-list path uses."""
    manager = object.__new__(DisplayManager)
    manager._show_board = True
    manager._show_analysis = show_analysis
    manager._coach_selection_callback = None
    manager._apply_compact_layout = MagicMock()
    manager.chess_board_widget = _FakeWidget(visible=True)
    manager.coach_text_widget = _FakeWidget(visible=False)
    manager.clock_widget = None
    manager.analysis_widget = _FakeWidget(visible=show_analysis) if analysis_present else None
    manager.move_list_widget = _FakeMoveList(plies=plies)
    manager.move_list_widget.set_selection_change_callback(
        manager._on_analysis_selection_change
    )
    return manager


def test_step_shows_move_list_when_analysis_widget_is_hidden():
    """UP/DOWN must surface the move list when Show Analysis is off.

    Why: the previous gate dropped the key when analysis_widget was hidden, so
    arrows did nothing with the eval panel off. How a regression manifests:
    step_analysis_selection returns False, or the move list stays hidden after
    stepping to a ply.
    """
    manager = _bare_display_manager(show_analysis=False, analysis_present=True)
    assert manager.analysis_widget.visible is False

    assert manager.step_analysis_selection(1) is True

    assert manager.move_list_widget.visible is True
    assert manager.move_list_widget.selected_ply() == 1
    assert manager.analysis_widget.visible is False
    assert manager.chess_board_widget.visible is False
    assert manager.coach_text_widget.visible is True
    assert manager.is_move_review_active() is True
    manager._apply_compact_layout.assert_called_with(True)


def test_step_shows_move_list_when_analysis_widget_is_absent():
    """UP/DOWN must surface the move list when Live Analysis never created one.

    Why: analysis_mode=False used to skip creating the widget that owned the
    list. The list is independent and must still exist. How a regression
    manifests: step_analysis_selection returns False, or AttributeError on a
    missing move_list_widget.
    """
    manager = _bare_display_manager(show_analysis=False, analysis_present=False)
    assert manager.analysis_widget is None

    assert manager.step_analysis_selection(1) is True

    assert manager.move_list_widget.visible is True
    assert manager.move_list_widget.selected_ply() == 1
    assert manager.chess_board_widget.visible is False
    assert manager.coach_text_widget.visible is True
    assert manager.is_move_review_active() is True


def test_step_shows_move_list_when_analysis_is_visible():
    """With analysis on, UP/DOWN still replaces the eval panel with the list.

    Why: the split must not break the existing review path. How a regression
    manifests: the list stays hidden while analysis remains on screen, so the
    eval graph covers the highlighted moves.
    """
    manager = _bare_display_manager(show_analysis=True, analysis_present=True)

    assert manager.step_analysis_selection(1) is True

    assert manager.move_list_widget.visible is True
    assert manager.analysis_widget.visible is False
    assert manager.is_move_review_active() is True


def test_wrapping_home_hides_move_list_and_restores_analysis_when_shown():
    """Stepping past the last ply hides the list and restores the eval panel.

    Why: wrapping back is how the board returns. With Show Analysis on, the
    eval panel must come back; the list must not stay covering it. How a
    regression manifests: the list remains visible at selection 0, or analysis
    stays hidden after wrapping home.
    """
    manager = _bare_display_manager(show_analysis=True, analysis_present=True, plies=2)
    manager.step_analysis_selection(1)
    manager.step_analysis_selection(1)
    assert manager.move_list_widget.selected_ply() == 2

    assert manager.step_analysis_selection(1) is True

    assert manager.move_list_widget.selected_ply() is None
    assert manager.move_list_widget.visible is False
    assert manager.analysis_widget.visible is True
    assert manager.chess_board_widget.visible is True
    assert manager.coach_text_widget.visible is False
    assert manager.is_move_review_active() is False
    manager._apply_compact_layout.assert_called_with(False)


def test_wrapping_home_keeps_analysis_hidden_when_show_analysis_is_off():
    """Wrapping home with Show Analysis off must not force the eval panel on.

    Why: the user turned the panel off; returning from the list is a return to
    the board, not a way to override that setting. How a regression manifests:
    analysis_widget.visible is True after wrapping home.
    """
    manager = _bare_display_manager(show_analysis=False, analysis_present=True, plies=1)
    manager.step_analysis_selection(1)
    assert manager.move_list_widget.visible is True

    manager.step_analysis_selection(1)  # wrap home

    assert manager.move_list_widget.visible is False
    assert manager.analysis_widget.visible is False
    assert manager.chess_board_widget.visible is True
    assert manager.is_move_review_active() is False


def test_step_is_noop_without_move_list_widget():
    """No move-list widget means UP/DOWN is not consumed here.

    Why: a layout that has not built the list (pre-game, torn down) must fall
    through rather than raise. How a regression manifests: AttributeError, or
    True returned so the key never reaches the game manager.
    """
    manager = _bare_display_manager()
    manager.move_list_widget = None

    assert manager.step_analysis_selection(1) is False
    assert manager.is_move_review_active() is False


def test_review_mode_keys_off_move_list_not_analysis():
    """Long-press OK review detection must read the move-list widget.

    Why: is_move_review_active used to ask analysis_widget.selected_ply(), which
    is None (or the widget is missing) when analysis is off, so LONG_TICK never
    opened takeback. How a regression manifests: False while a ply is
    highlighted on the move list, or True when only the analysis widget is set.
    """
    manager = _bare_display_manager(show_analysis=False, analysis_present=False)
    manager.move_list_widget._selection = 3
    assert manager.is_move_review_active() is True

    manager.move_list_widget._selection = 0
    assert manager.is_move_review_active() is False


def test_current_selection_and_select_ply_use_move_list():
    """Session restore of a reviewed ply must talk to the move-list widget.

    Why: current_analysis_selection / select_analysis_ply used to read and write
    analysis_widget, so a restart with analysis off could not reopen the list.
    How a regression manifests: current_analysis_selection is 0 while a ply is
    selected, or select_analysis_ply is a no-op when analysis_widget is None.
    """
    manager = _bare_display_manager(analysis_present=False, plies=8)

    assert manager.select_analysis_ply(5) is True
    assert manager.current_analysis_selection() == 5
    assert manager.move_list_widget.selected_ply() == 5
    assert manager.move_list_widget.visible is True

    assert manager.select_analysis_ply(0) is True
    assert manager.current_analysis_selection() == 0
    assert manager.move_list_widget.visible is False
