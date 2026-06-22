#!/usr/bin/env python3
"""Tests for ChessClockWidget self-managing visibility based on game state.

The ChessClockWidget should manage its own visibility based on a simple rule:
- Visible when: timed game is in progress AND game is not over
- Hidden when: game is over OR not in timed mode

The clock should NOT be managed by external widgets (like GameOverWidget).
Each widget should observe game state directly and manage its own lifecycle.

Test scenarios:
1. Clock is visible during a timed game in progress
2. Clock hides itself when game ends (checkmate, resignation, flag, etc.)
3. Clock shows itself again when a new game starts (position reset)
4. Clock stays hidden when not in timed mode regardless of game state
"""

import sys
import types
import unittest
from unittest.mock import MagicMock

# =============================================================================
# Mock all hardware-specific and Linux-specific modules BEFORE any imports
# =============================================================================

# Mock hardware and Linux-specific modules
# psutil is intentionally NOT stubbed: it is a real, required dependency
# (universalchess.main imports it unconditionally). Replacing it with a MagicMock
# here leaked globally via sys.modules and broke other tests that need the real
# library (e.g. the system_info real-collection smoke test).
for mod in ['spidev', 'RPi', 'RPi.GPIO', 'gpiozero', 'smbus', 'smbus2', 'bluetooth']:
    sys.modules[mod] = MagicMock()

# Mock dbus with submodules
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

# numpy is a real dependency (used by the e-paper drivers via np.packbits) and
# is left unmocked, exactly like PIL: replacing it in sys.modules here leaked a
# MagicMock to every later-collected test module, breaking real numpy/PIL
# consumers (e.g. the e-paper packing tests and icon/help-dialog rendering).

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


class TestChessClockWidgetVisibility(unittest.TestCase):
    """Test ChessClockWidget self-managing visibility based on game state.
    
    The widget should:
    1. Be visible during timed game in progress
    2. Hide itself when game ends
    3. Show itself when new game starts (reset)
    4. Respect timed_mode flag
    """

    def setUp(self):
        """Set up test fixtures with fresh game and clock state."""
        from universalchess.state.chess_game import reset_chess_game
        from universalchess.state.chess_clock import reset_chess_clock
        
        self.game_state = reset_chess_game()
        self.clock_state = reset_chess_clock()
        # Configure as timed mode
        self.clock_state.set_timed_mode(True)
        self.clock_state.set_times(300, 300)  # 5 minutes each

    def _create_widget(self, timed_mode=True):
        """Create a ChessClockWidget with test state.
        
        Args:
            timed_mode: Whether to create in timed mode.
            
        Returns:
            ChessClockWidget instance.
        """
        from universalchess.epaper.chess_clock import ChessClockWidget
        
        widget = ChessClockWidget(
            0, 144, 128, 72,
            update_callback=MagicMock(),
            timed_mode=timed_mode
        )
        return widget

    def test_clock_visible_during_timed_game(self):
        """Clock should be visible during an active timed game.
        
        Expected: visible=True when timed_mode and game in progress.
        Failure: Clock hidden during active timed game.
        """
        widget = self._create_widget(timed_mode=True)
        
        # Start a game
        self.game_state.push_uci('e2e4')
        
        self.assertTrue(widget.visible, 
                       "Clock should be visible during active timed game")
        
        widget.stop()

    def test_clock_hides_on_game_over(self):
        """Clock should hide itself when game ends (checkmate).
        
        Expected: visible=False after checkmate.
        Failure: Clock remains visible after game over, obscuring game result.
        """
        widget = self._create_widget(timed_mode=True)
        
        # Play fool's mate (quickest checkmate)
        self.game_state.push_uci('f2f3')
        self.game_state.push_uci('e7e5')
        self.game_state.push_uci('g2g4')
        self.game_state.push_uci('d8h4')  # Qh4#
        
        self.assertFalse(widget.visible, 
                        "Clock should hide after game over (checkmate)")
        
        widget.stop()

    def test_clock_shows_on_new_game(self):
        """Clock should show itself when a new timed game starts after game over.
        
        Expected: visible=True after reset when timed mode.
        Failure: Clock remains hidden after starting new game.
        """
        widget = self._create_widget(timed_mode=True)
        
        # Play to checkmate
        self.game_state.push_uci('f2f3')
        self.game_state.push_uci('e7e5')
        self.game_state.push_uci('g2g4')
        self.game_state.push_uci('d8h4')  # Qh4#
        
        # Verify hidden after checkmate
        self.assertFalse(widget.visible)
        
        # Reset game (simulates setting up pieces for new game)
        self.game_state.reset()
        
        # Clock should be visible again for the new game
        self.assertTrue(widget.visible, 
                       "Clock should show after game reset in timed mode")
        
        widget.stop()

    def test_clock_hides_on_resignation(self):
        """Clock should hide when game ends by resignation.
        
        Expected: visible=False after resignation.
        Failure: Clock visible after resignation, blocking game result display.
        """
        widget = self._create_widget(timed_mode=True)
        
        # Start a game
        self.game_state.push_uci('e2e4')
        self.game_state.push_uci('e7e5')
        
        # Resign (external game over)
        self.game_state.set_result('1-0', 'resignation')
        
        self.assertFalse(widget.visible, 
                        "Clock should hide after resignation")
        
        widget.stop()

    def test_clock_hidden_in_untimed_mode_during_game_over(self):
        """Untimed mode clock should also hide on game over.
        
        Even in untimed mode, the turn indicator should hide when game ends.
        
        Expected: visible=False after game over in untimed mode.
        Failure: Turn indicator remains visible after game over.
        """
        widget = self._create_widget(timed_mode=False)
        
        # Play to checkmate
        self.game_state.push_uci('f2f3')
        self.game_state.push_uci('e7e5')
        self.game_state.push_uci('g2g4')
        self.game_state.push_uci('d8h4')  # Qh4#
        
        self.assertFalse(widget.visible, 
                        "Untimed clock should hide after game over")
        
        widget.stop()

    def test_cleanup_unsubscribes_from_game_over(self):
        """stop() should unsubscribe from game_over observer.
        
        Expected: After stop(), game over events don't affect widget.
        Failure: Widget continues responding to game events after stop.
        """
        widget = self._create_widget(timed_mode=True)
        
        # Start a game (clock visible)
        self.game_state.push_uci('e2e4')
        self.assertTrue(widget.visible)
        
        # Stop widget
        widget.stop()
        
        # Reset and play to checkmate - widget should not respond
        self.game_state.reset()
        self.game_state.push_uci('f2f3')
        self.game_state.push_uci('e7e5')
        self.game_state.push_uci('g2g4')
        self.game_state.push_uci('d8h4')  # Qh4#
        
        # Widget should NOT have changed (not subscribed anymore)
        # It was visible before stop, should still be visible
        self.assertTrue(widget.visible, 
                       "Widget should not respond to game events after stop")


if __name__ == "__main__":
    unittest.main()

