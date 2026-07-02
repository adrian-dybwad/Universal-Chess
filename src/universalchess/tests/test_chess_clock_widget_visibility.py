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

    def test_hide_turn_indicator_omits_circle_in_compact_mode(self):
        """set_hide_turn_indicator(True) removes the compact turn-indicator circle.

        Compact (untimed) mode draws a large color circle for the side to move.
        When paging the board's move history the circle is hidden to shrink the
        indicator. This renders the same position with the circle shown and
        hidden and asserts the flag toggles and the circle's black pixels go away.

        Expected: hidden render has strictly fewer black pixels than shown (the
        filled/outlined circle is gone) and the flag reports the new state.
        Failure: identical renders (flag ignored) or hidden >= shown black pixels
        would mean the circle was still drawn.
        """
        from PIL import Image

        widget = self._create_widget(timed_mode=False)
        # Push a move so it is Black to move -> a filled circle, giving a large,
        # unambiguous black-pixel delta when it is removed.
        self.game_state.push_uci('e2e4')

        def black_pixels() -> int:
            img = Image.new('1', (widget.width, widget.height), 255)
            widget.render(img)
            return sum(1 for p in img.getdata() if p == 0)

        self.assertFalse(widget.hide_turn_indicator)
        shown = black_pixels()

        widget.set_hide_turn_indicator(True)
        self.assertTrue(widget.hide_turn_indicator)
        hidden = black_pixels()

        self.assertLess(hidden, shown,
                        "Hiding the turn indicator should remove the circle's black pixels")

        widget.stop()

    def tearDown(self):
        """Reset the players singleton so name state doesn't leak between tests."""
        from universalchess.state.players import reset_players_state
        reset_players_state()

    def _indicator_column_ink(self, img, y0, y1):
        """Count black pixels in the left turn-indicator column over rows [y0, y1)."""
        pixels = img.load()
        return sum(
            1
            for y in range(y0, y1)
            for x in range(4, 17)
            if pixels[x, y] == 0
        )

    def _create_sized_widget(self, height):
        """Create a timed clock widget of a given height (compact vs normal)."""
        from universalchess.epaper.chess_clock import ChessClockWidget
        return ChessClockWidget(
            0, 144, 128, height, update_callback=MagicMock(), timed_mode=True
        )

    def test_timed_mode_keeps_turn_circles_at_compact_height(self):
        """Shrinking the timed clock must keep a turn-indicator circle per section.

        The compact move-history layout shrinks the whole clock widget rather
        than removing the circles (removing them is not useful in timed mode).
        Rendered at a short height, both the top and bottom sections must still
        draw their indicator circle in the left column.

        Expected: ink present in the left column of both the upper and lower
        halves at the compact height.
        Failure: a missing region would mean a section's circle was dropped when
        the clock shrank, losing the turn cue next to a clock.
        """
        from PIL import Image

        widget = self._create_sized_widget(52)
        self.game_state.push_uci('e2e4')

        img = Image.new('1', (widget.width, widget.height), 255)
        widget.render(img)

        mid = widget.height // 2
        self.assertGreater(self._indicator_column_ink(img, 2, mid), 0,
                           "Top section should draw a turn-indicator circle when compact")
        self.assertGreater(self._indicator_column_ink(img, mid, widget.height - 1), 0,
                           "Bottom section should draw a turn-indicator circle when compact")

        widget.stop()

    def test_timed_mode_drops_player_names_when_sections_are_short(self):
        """Player names are drawn only when a section is tall enough for them.

        The compact layout shrinks each clock section; there is no room for the
        name line under the color label, so names are dropped. A tall (normal)
        clock still shows them. Asserts via the name text widgets whether their
        text was set during render.

        Expected: name widgets populated at the tall height, empty at the short.
        Failure: names set at the short height would overflow into the clock/
        separator; names missing at the tall height would be a regression in the
        normal layout.
        """
        from PIL import Image
        from universalchess.state.players import get_players_state

        get_players_state().set_player_names("Alice", "Bob")

        def render_name_texts(height):
            widget = self._create_sized_widget(height)
            img = Image.new('1', (widget.width, widget.height), 255)
            widget.render(img)
            texts = (widget._white_name_text.text, widget._black_name_text.text)
            widget.stop()
            return texts

        tall_white, tall_black = render_name_texts(100)
        self.assertEqual((tall_white, tall_black), ("Alice", "Bob"),
                         "Tall (normal) timed clock should draw both player names")

        short_white, short_black = render_name_texts(52)
        self.assertEqual((short_white, short_black), ("", ""),
                         "Short (compact) timed clock should drop player names")

    def test_set_hide_turn_indicator_is_noop_when_unchanged(self):
        """Setting the same hide state must not request a redraw.

        Expected: update_callback not invoked when the value is unchanged.
        Failure: a redraw on every page render would flicker the clock and waste
        e-paper refreshes.
        """
        widget = self._create_widget(timed_mode=False)
        # A move makes the untimed clock visible so request_update is not
        # suppressed (hidden widgets ignore redraw requests).
        self.game_state.push_uci('e2e4')
        self.assertTrue(widget.visible)
        widget._update_callback.reset_mock()

        widget.set_hide_turn_indicator(False)  # already False
        widget._update_callback.assert_not_called()

        widget.set_hide_turn_indicator(True)   # changed -> redraw
        self.assertTrue(widget._update_callback.called)

        widget.stop()

    def test_render_setting_child_text_does_not_request_a_refresh(self):
        """Rendering the clock must not trigger an extra display refresh.

        The clock's time/label/name TextWidgets are render-only helpers: their
        set_text() is called inside the clock's own render(), which already draws
        the new text. TextWidget.set_text() calls request_update(); if those
        children forwarded that to the Manager it would fire a second, redundant
        full-screen refresh per tick (Manager defers the re-entrant update and
        replays it). On the slow e-paper panel that doubled the refresh rate and
        made the once-per-second clock drift/beat -- the erratic cadence.

        How the regression manifests: a child's set_text() during render() calls
        the clock's update_callback (Manager.update), so this assertion sees the
        callback invoked purely as a side effect of rendering a changed time.
        """
        from PIL import Image

        widget = self._create_widget(timed_mode=True)
        # A move makes the timed clock visible, so the child request_update is not
        # suppressed by a hidden-widget check (which would pass trivially).
        self.game_state.push_uci('e2e4')
        self.assertTrue(widget.visible)

        img = Image.new('1', (widget.width, widget.height), 255)
        widget.render(img)  # first render populates the time text ("05:00")

        # Change both clocks so the next render's set_text() genuinely changes the
        # text (set_text is a no-op when unchanged, which would hide the bug).
        self.clock_state.set_times(299, 298)
        widget._update_callback.reset_mock()

        widget.render(img)
        widget._update_callback.assert_not_called()

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

