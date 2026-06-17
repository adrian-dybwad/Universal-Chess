#!/usr/bin/env python3
"""Tests for game over widget observer pattern behavior.

The GameOverWidget uses the observer pattern to manage its own visibility:
1. Subscribes to ChessGameState.on_game_over() to show itself
2. Subscribes to ChessGameState.on_position_change() to hide itself on new game

Test case:
1. Game ends (game over widget shows itself via game_over observer)
2. User resets pieces to starting position
3. ChessGameState.reset() is called
4. Widget hides itself via position_change observer (is_game_over becomes False)

This is the proper observer pattern - the widget manages its own lifecycle
based on state changes, rather than being externally managed.

Note: ChessClockWidget manages its own visibility via game_over observer.
GameOverWidget does NOT manage clock widget visibility.
"""

import sys
import types
import unittest
from unittest.mock import MagicMock, patch
import importlib.util

# =============================================================================
# Mock all hardware-specific and Linux-specific modules BEFORE any imports
# =============================================================================

# Mock hardware and Linux-specific modules. psutil is intentionally NOT stubbed:
# it is a real, required dependency (universalchess.main imports it
# unconditionally), and replacing it with a MagicMock here leaked globally via
# sys.modules and broke tests that need the real library.
for mod in ['spidev', 'RPi', 'RPi.GPIO', 'gpiozero', 'smbus', 'smbus2', 'bluetooth']:
    sys.modules[mod] = MagicMock()

# Mock dbus with submodules (dbus is a package, not just a module)
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

# Mock gi (GObject Introspection)
sys.modules['gi'] = MagicMock()
sys.modules['gi.repository'] = MagicMock()

# Mock serial
sys.modules['serial'] = MagicMock()
sys.modules['serial.tools'] = MagicMock()
sys.modules['serial.tools.list_ports'] = MagicMock()

# Mock numpy (not an installed dependency in the test env). PIL is real and is
# left unmocked: replacing it in sys.modules here leaked a MagicMock to every
# later-collected test module, breaking real PIL rendering (icons/help dialog).
sys.modules['numpy'] = MagicMock()

# Create proper package mocks for DGTCentaurMods.board
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


class TestGameOverWidgetObserver(unittest.TestCase):
    """Test GameOverWidget observer pattern behavior.
    
    The widget should:
    1. Start hidden
    2. Show itself when game_over event fires
    3. Hide itself when position_change fires and game is no longer over
    
    Note: Clock widget visibility is managed by ChessClockWidget itself,
    not by GameOverWidget. See test_chess_clock_widget_visibility.py.
    """

    def setUp(self):
        """Set up test fixtures with fresh game state."""
        from universalchess.state.chess_game import reset_chess_game
        self.game_state = reset_chess_game()
        mock_board.ledsOff.reset_mock()

    def _create_widget(self, led_off_callback=None):
        """Create a GameOverWidget with test game state.
            
        Args:
            led_off_callback: Optional LED off callback. If None, uses mock_board.ledsOff.
            
        Returns:
            GameOverWidget instance subscribed to test game state.
        """
        from universalchess.epaper.game_over import GameOverWidget
        
        return GameOverWidget(
            0, 144, 128, 72,
            update_callback=MagicMock(),
            game_state=self.game_state,
            led_off_callback=led_off_callback or mock_board.ledsOff
        )

    def test_widget_starts_hidden(self):
        """Widget should start hidden (not visible).
        
        Expected: visible=False on initialization.
        Failure: Widget starts visible before any game has ended.
        """
        widget = self._create_widget()
        
        self.assertFalse(widget.visible, "GameOverWidget should start hidden")
        
        widget.cleanup()

    def test_widget_shows_on_game_over_event(self):
        """Widget should show itself when game_over event fires.
        
        Expected: visible=True after checkmate.
        Failure: Widget does not respond to game_over observer.
        """
        widget = self._create_widget()
        
        # Play fool's mate (quickest checkmate)
        self.game_state.push_uci('f2f3')
        self.game_state.push_uci('e7e5')
        self.game_state.push_uci('g2g4')
        self.game_state.push_uci('d8h4')  # Qh4#
        
        # Widget should now be visible
        self.assertTrue(widget.visible, "Widget should be visible after checkmate")
        
        # Widget should have the correct result
        self.assertEqual(widget.result, '0-1')
        self.assertEqual(widget.winner, 'Black wins')
        self.assertIn('Checkmate', widget.termination)
        
        widget.cleanup()

    def test_widget_hides_on_game_reset(self):
        """Widget should hide itself when game is reset (new game).
        
        After game over, resetting the game state should:
        1. Trigger position_change observer
        2. is_game_over becomes False
        3. Widget hides itself
        
        Expected: visible=False after reset.
        Failure: Widget remains visible on new game.
        """
        widget = self._create_widget()
        
        # Play to checkmate
        self.game_state.push_uci('f2f3')
        self.game_state.push_uci('e7e5')
        self.game_state.push_uci('g2g4')
        self.game_state.push_uci('d8h4')  # Qh4#
        
        # Widget is visible
        self.assertTrue(widget.visible)
        
        # Reset game (simulates placing pieces in starting position)
        self.game_state.reset()
        
        # Widget should now be hidden
        self.assertFalse(widget.visible, "Widget should hide after game reset")
        
        widget.cleanup()

    def test_widget_clears_state_on_reset(self):
        """Widget should clear its display state when game resets.
        
        Result, winner, termination, etc. should be cleared for next game.
        
        Expected: All display fields reset to empty/zero.
        Failure: Previous game's result persists.
        """
        widget = self._create_widget()
        
        # Play to checkmate
        self.game_state.push_uci('f2f3')
        self.game_state.push_uci('e7e5')
        self.game_state.push_uci('g2g4')
        self.game_state.push_uci('d8h4')  # Qh4#
        
        # Verify data is set
        self.assertEqual(widget.result, '0-1')
        self.assertEqual(widget.winner, 'Black wins')
        
        # Reset game
        self.game_state.reset()
        
        # Verify data is cleared
        self.assertEqual(widget.result, "")
        self.assertEqual(widget.winner, "")
        self.assertEqual(widget.termination, "")
        self.assertEqual(widget.move_count, 0)
        
        widget.cleanup()

    def test_widget_turns_off_leds_on_show(self):
        """Widget should turn off LEDs when showing (game ended).
        
        Expected: board.ledsOff() called when widget shows.
        Failure: LEDs remain on after game over.
        """
        widget = self._create_widget()
        mock_board.ledsOff.reset_mock()
        
        # Play to checkmate
        self.game_state.push_uci('f2f3')
        self.game_state.push_uci('e7e5')
        self.game_state.push_uci('g2g4')
        self.game_state.push_uci('d8h4')  # Qh4#
        
        # LEDs should be off
        mock_board.ledsOff.assert_called()
        
        widget.cleanup()

    def test_cleanup_unsubscribes_from_game_state(self):
        """cleanup() should unsubscribe widget from game state observers.
        
        Expected: After cleanup, game events don't affect widget.
        Failure: Widget continues responding to events after cleanup.
        """
        widget = self._create_widget()
        
        # Cleanup
        widget.cleanup()
        
        # Now play to checkmate - widget should NOT respond
        self.game_state.push_uci('f2f3')
        self.game_state.push_uci('e7e5')
        self.game_state.push_uci('g2g4')
        self.game_state.push_uci('d8h4')  # Qh4#
        
        # Widget should still be hidden (not subscribed anymore)
        self.assertFalse(widget.visible, "Widget should not respond after cleanup")

    def test_widget_ignores_position_change_while_game_in_progress(self):
        """Widget should not hide on position changes during active game.
        
        Only hide when is_game_over becomes False (new game after game over).
        Normal moves should not affect visibility.
        
        Expected: Widget remains hidden during normal play.
        Failure: Widget incorrectly shows/hides during normal game.
        """
        widget = self._create_widget()
        
        # Play some moves (not to checkmate)
        self.game_state.push_uci('e2e4')
        self.game_state.push_uci('e7e5')
        
        # Widget should still be hidden
        self.assertFalse(widget.visible, "Widget should remain hidden during normal play")
        
        widget.cleanup()


if __name__ == "__main__":
    unittest.main()
