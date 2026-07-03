#!/usr/bin/env python3
"""Tests for the end-of-game display on menu-driven game endings (resign/draw).

Regression under test
----------------------
A natural game ending (checkmate) leaves the board on screen, so the
subscribed GameOverWidget catches the game_over event and shows the result
("Black wins" / "Resignation", etc.). A menu-driven ending (BACK menu resign /
draw, or the king-lift resign gesture) first replaces the board with a menu,
which destroys the GameOverWidget. The old code only rebuilt the board for the
"cancel" outcome and navigated straight to the root menu on resign/draw, so the
end-of-game screen never appeared and the position could not be pondered.

The fix funnels every non-blocking game menu through
``DisplayManager._finalize_menu_selection``, which rebuilds the board (and its
GameOverWidget) for every on-board outcome BEFORE invoking the result callback.
A game-ending callback (resign/draw) then sets the result while a live, freshly
subscribed GameOverWidget exists, so the end screen displays over the board -
matching the checkmate flow. Only a shutdown ("exit") skips the rebuild.
"""

import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# =============================================================================
# Mock hardware/Linux-specific modules BEFORE importing application code.
# Mirrors test_game_over_widget_clear.py: psutil, numpy and PIL are real
# dependencies and intentionally left unmocked.
# =============================================================================
for mod in ['spidev', 'RPi', 'RPi.GPIO', 'gpiozero', 'smbus', 'smbus2', 'bluetooth']:
    sys.modules[mod] = MagicMock()

dbus_mock = types.ModuleType('dbus')
dbus_mock.mainloop = MagicMock()
dbus_mock.service = MagicMock()
dbus_mock.Interface = MagicMock()
dbus_mock.SystemBus = MagicMock()
dbus_mock.SessionBus = MagicMock()
dbus_mock.String = str
dbus_mock.Byte = int
dbus_mock.Array = list
dbus_mock.Dictionary = dict
sys.modules['dbus'] = dbus_mock
sys.modules['dbus.mainloop'] = MagicMock()
sys.modules['dbus.mainloop.glib'] = MagicMock()
sys.modules['dbus.service'] = MagicMock()

sys.modules['gi'] = MagicMock()
sys.modules['gi.repository'] = MagicMock()

sys.modules['serial'] = MagicMock()
sys.modules['serial.tools'] = MagicMock()
sys.modules['serial.tools.list_ports'] = MagicMock()

board_package = types.ModuleType('DGTCentaurMods.board')
mock_board = MagicMock()
mock_board.display_manager = MagicMock()
mock_board.display_manager.add_widget = MagicMock(return_value=MagicMock())
mock_board.display_manager.remove_widget = MagicMock()
mock_board.ledsOff = MagicMock()
mock_centaur = MagicMock()
board_package.board = mock_board
board_package.centaur = mock_centaur
sys.modules['DGTCentaurMods.board'] = board_package
sys.modules['DGTCentaurMods.board.board'] = mock_board
sys.modules['DGTCentaurMods.board.centaur'] = mock_centaur
sys.modules['DGTCentaurMods.board.logging'] = MagicMock()
sys.modules['DGTCentaurMods.board.settings'] = MagicMock()


class _FakeMenu:
    """Minimal stand-in for a non-blocking menu widget.

    ``_finalize_menu_selection`` only reads ``_selection_result`` and calls
    ``deactivate()`` on the menu, so a real IconMenuWidget (which renders icons
    and fonts) is unnecessary here.
    """

    def __init__(self, selection_result: str):
        self._selection_result = selection_result
        self.deactivated = False

    def deactivate(self) -> None:
        self.deactivated = True


class _DisplayManagerTestBase(unittest.TestCase):
    """Builds a DisplayManager with hardware/widget creation stubbed out."""

    def setUp(self):
        from universalchess.state.chess_game import reset_chess_game
        self.game_state = reset_chess_game()

        from universalchess.managers.display import DisplayManager
        import universalchess.managers.display as display_module

        # _init_widgets touches the e-paper hardware; stub it during construction
        # so the manager builds without a real display. Individual tests install
        # their own _init_widgets behaviour afterwards.
        with patch.object(DisplayManager, '_init_widgets'):
            display_module.get_chess_game = MagicMock(return_value=self.game_state)
            display_module._load_widgets = MagicMock()
            display_module.get_chess_clock_service = MagicMock(return_value=MagicMock())
            display_module.get_clock_state = MagicMock(return_value=MagicMock())

            self.dm = DisplayManager()
            self.dm._clock = MagicMock()
            self.dm.clock_widget = MagicMock()
            self.dm._time_control = 0


class TestFinalizeMenuSelectionRebuild(_DisplayManagerTestBase):
    """The board must be rebuilt before the callback for every on-board outcome."""

    def _run(self, selection_result: str, shutdown_result: str = "exit"):
        """Drive _finalize_menu_selection and record call ordering.

        Returns (events, callback_results) where events is the interleaved order
        of "init"/"callback" so a test can assert the rebuild precedes the
        callback, and callback_results is the list of results the callback saw.
        """
        events = []
        callback_results = []

        self.dm._init_widgets = lambda: events.append("init")

        def _callback(result):
            events.append("callback")
            callback_results.append(result)

        self.dm._menu_result_callback = _callback
        menu = _FakeMenu(selection_result)
        self.dm._menu_active = True
        self.dm._current_menu = menu

        self.dm._finalize_menu_selection(menu, shutdown_result=shutdown_result)

        return events, callback_results, menu

    def test_resign_rebuilds_board_before_callback(self):
        """Resign must rebuild the board before the resign callback runs.

        Guards the core regression: if the board (and its GameOverWidget) is not
        rebuilt before handle_resign sets the result, the end screen never shows.
        Failure manifests as the "init" event being absent or ordered after
        "callback".
        """
        events, results, _ = self._run("resign")

        self.assertIn("init", events, "Board was not rebuilt for a resign outcome")
        self.assertEqual(events, ["init", "callback"],
                         "Board rebuild must happen before the result callback")
        self.assertEqual(results, ["resign"])

    def test_two_player_resign_keys_rebuild_board(self):
        """Both per-side resign keys (2-player mode) must rebuild the board.

        Failure manifests as a missing "init" event for resign_white/resign_black,
        meaning a 2-player resignation would skip the end-of-game screen.
        """
        for key in ("resign_white", "resign_black"):
            with self.subTest(result=key):
                events, results, _ = self._run(key)
                self.assertEqual(events, ["init", "callback"])
                self.assertEqual(results, [key])

    def test_draw_rebuilds_board_before_callback(self):
        """Draw must rebuild the board before the draw callback runs.

        A draw ends the game like resign, so the GameOverWidget must be present
        to show "Draw". Failure: missing/late "init" event.
        """
        events, results, _ = self._run("draw")

        self.assertEqual(events, ["init", "callback"])
        self.assertEqual(results, ["draw"])

    def test_cancel_rebuilds_board(self):
        """Cancel still rebuilds the board (pre-existing behaviour, preserved).

        BACK maps to "cancel". Failure: cancel no longer restores the board,
        leaving the game un-resumable behind a stale menu.
        """
        events, results, menu = self._run("BACK")

        self.assertEqual(events, ["init", "callback"])
        self.assertEqual(results, ["cancel"])
        self.assertTrue(menu.deactivated, "Menu should be deactivated on completion")

    def test_exit_does_not_rebuild_board(self):
        """Shutdown ("exit") must NOT rebuild the board.

        The device is powering off, so rebuilding the board would be wasted work
        and could race with teardown. Failure: an "init" event appears for exit.
        """
        events, results, _ = self._run("SHUTDOWN", shutdown_result="exit")

        self.assertNotIn("init", events, "Board must not be rebuilt when exiting")
        self.assertEqual(events, ["callback"])
        self.assertEqual(results, ["exit"])

    def test_king_lift_shutdown_maps_to_cancel_and_rebuilds(self):
        """King-lift menu must treat SHUTDOWN as cancel and rebuild the board.

        The king-lift resign menu must never power the device off, so it passes
        shutdown_result="cancel". Failure: the device exits, or the board is not
        restored after dismissing the gesture menu.
        """
        events, results, _ = self._run("SHUTDOWN", shutdown_result="cancel")

        self.assertEqual(events, ["init", "callback"])
        self.assertEqual(results, ["cancel"])

    def test_menu_state_cleared_after_completion(self):
        """Menu bookkeeping is cleared so input routing returns to the game.

        Failure: _menu_active stays True / _current_menu lingers, so subsequent
        key presses would still be routed to the dismissed menu.
        """
        self._run("resign")

        self.assertFalse(self.dm._menu_active)
        self.assertIsNone(self.dm._current_menu)


class TestOverlayMenuPausesClock(_DisplayManagerTestBase):
    """A running clock is paused for the resign/draw overlay and resumed on cancel.

    Regression: in a timed game the clock kept counting while the BACK
    (resign/draw) menu was shown. A counting clock puts the Manager in
    clock-driven refresh mode, but the overlay tears the clock widget down, so no
    tick fired to flush the menu's selection-highlight redraws - arrow-key
    navigation appeared frozen. Pausing the clock (which _sync_clock_refresh_mode
    turns into immediate refreshes) fixes both the time accounting and the
    responsiveness. Cancel returns to play and must resume; resign/draw/exit end
    or leave the game and must not.
    """

    def _finalize(self, selection_result, shutdown_result="exit"):
        self.dm._init_widgets = lambda: None
        self.dm._menu_result_callback = lambda result: None
        menu = _FakeMenu(selection_result)
        self.dm._menu_active = True
        self.dm._current_menu = menu
        self.dm._finalize_menu_selection(menu, shutdown_result=shutdown_result)

    def test_pause_only_when_clock_is_counting(self):
        """_pause_clock_for_menu pauses and flags only a running, unpaused clock.

        Failure manifests as either a not-yet-started clock being paused/flagged
        (which would then be spuriously resumed on cancel, starting the clock) or
        a counting clock not being paused (the original frozen-menu bug).
        """
        self.dm._clock.is_running = False
        self.dm._clock.is_paused = False
        self.dm._pause_clock_for_menu()
        self.dm._clock.pause.assert_not_called()
        self.assertFalse(self.dm._clock_paused_for_menu)

        self.dm._clock.reset_mock()
        self.dm._clock.is_running = True
        self.dm._clock.is_paused = True  # already paused (e.g. full-menu suspend)
        self.dm._pause_clock_for_menu()
        self.dm._clock.pause.assert_not_called()
        self.assertFalse(self.dm._clock_paused_for_menu)

        self.dm._clock.reset_mock()
        self.dm._clock.is_running = True
        self.dm._clock.is_paused = False
        self.dm._pause_clock_for_menu()
        self.dm._clock.pause.assert_called_once()
        self.assertTrue(self.dm._clock_paused_for_menu)

    def test_cancel_resumes_clock_paused_for_menu(self):
        """Cancelling the overlay resumes the clock this manager paused.

        Failure manifests as the clock staying paused after returning to play,
        so the player's time would never resume counting.
        """
        self.dm._clock_paused_for_menu = True
        self._finalize("BACK")  # BACK maps to cancel
        self.dm._clock.resume.assert_called_once()
        self.assertFalse(self.dm._clock_paused_for_menu)

    def test_cancel_does_not_resume_when_not_paused_for_menu(self):
        """Cancel must not start a clock this manager did not pause for the menu.

        Guards the not-yet-started case: resume() on a stopped clock would START
        it. Failure manifests as clock.resume being called with the guard flag
        clear.
        """
        self.dm._clock_paused_for_menu = False
        self._finalize("BACK")
        self.dm._clock.resume.assert_not_called()

    def test_game_ending_outcomes_do_not_resume(self):
        """Resign/draw end the game, so the clock must not be resumed.

        The game-over handler stops the clock; resuming here would restart a
        countdown behind the end screen. Failure manifests as clock.resume being
        called for a resign/draw, and the guard flag left set.
        """
        for result in ("resign", "resign_white", "resign_black", "draw"):
            with self.subTest(result=result):
                self.dm._clock.reset_mock()
                self.dm._clock_paused_for_menu = True
                self._finalize(result)
                self.dm._clock.resume.assert_not_called()
                self.assertFalse(self.dm._clock_paused_for_menu)

    def test_exit_does_not_resume(self):
        """Shutdown must not resume the clock (device is powering off).

        Failure manifests as clock.resume being called during teardown.
        """
        self.dm._clock_paused_for_menu = True
        self._finalize("SHUTDOWN", shutdown_result="exit")
        self.dm._clock.resume.assert_not_called()
        self.assertFalse(self.dm._clock_paused_for_menu)


class TestResignDisplaysGameOverWidget(_DisplayManagerTestBase):
    """End-to-end: a resign selection results in a visible game-over screen."""

    def test_resign_shows_game_over_widget_with_result(self):
        """After a resign selection the rebuilt GameOverWidget shows the result.

        Simulates the real flow: the board was cleared while the menu was shown
        (no GameOverWidget subscribed). _finalize_menu_selection rebuilds the
        board - here that creates a real GameOverWidget subscribed to the shared
        game state - and only then runs the resign callback, which sets the game
        result (mirroring GameManager.handle_resign). The widget must catch the
        game_over event and display "Black wins" / "Resignation".

        Failure manifests as the widget staying hidden (no result shown), which
        is exactly the bug: the end screen never appears for a resignation.
        """
        from universalchess.epaper.game_over import GameOverWidget

        created = []

        def _fake_init_widgets():
            # Mirror _init_widgets creating and subscribing a GameOverWidget.
            widget = GameOverWidget(
                0, 144, 128, 72,
                update_callback=MagicMock(),
                game_state=self.game_state,
                led_off_callback=MagicMock(),
            )
            created.append(widget)

        self.dm._init_widgets = _fake_init_widgets

        def _on_resign(result):
            # Mirror GameManager.handle_resign: White resigns, Black wins.
            self.game_state.set_result("0-1", "Termination.RESIGN")

        self.dm._menu_result_callback = _on_resign
        menu = _FakeMenu("resign")
        self.dm._menu_active = True
        self.dm._current_menu = menu

        self.dm._finalize_menu_selection(menu, shutdown_result="exit")

        self.assertEqual(len(created), 1, "Exactly one GameOverWidget should be rebuilt")
        widget = created[0]
        self.assertTrue(widget.visible,
                        "GameOverWidget must be visible after a resignation")
        self.assertEqual(widget.result, "0-1")
        self.assertEqual(widget.winner, "Black wins")
        self.assertIn("Resignation", widget.termination)

        widget.cleanup()


if __name__ == "__main__":
    unittest.main()
