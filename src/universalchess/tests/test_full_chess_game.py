#!/usr/bin/env python3
"""Integration tests for a complete chess game flow.

Tests simulate a full chess game from start to finish, including:
1. Game setup with players
2. Move execution via piece events (lift/place)
3. Game over detection (checkmate)
4. New game reset and widget clearing

These tests verify the integration between:
- ChessGameState (board position, game over detection)
- PlayerManager (routing events to players)
- HumanPlayer (forming moves from piece events)
- DisplayManager (game over widget lifecycle)

The "Scholar's Mate" (4-move checkmate) is used as a quick test game:
1. e4 e5
2. Bc4 Nc6
3. Qh5 Nf6??
4. Qxf7# (checkmate)
"""

import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call

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

# numpy is a real dependency (used by the e-paper drivers via np.packbits) and
# is left unmocked, exactly like PIL: replacing either in sys.modules here
# leaked a MagicMock to every later-collected test (e-paper packing tests and
# icon/help-dialog rendering then broke), so both are intentionally left real.

# Create proper package mocks for DGTCentaurMods.board
board_package = types.ModuleType('DGTCentaurMods.board')
mock_board = MagicMock()
mock_board.display_manager = MagicMock()
mock_board.display_manager.add_widget = MagicMock(return_value=MagicMock())
mock_board.display_manager.remove_widget = MagicMock()
mock_centaur = MagicMock()
board_package.board = mock_board
board_package.centaur = mock_centaur
sys.modules['DGTCentaurMods.board'] = board_package
sys.modules['DGTCentaurMods.board.board'] = mock_board
sys.modules['DGTCentaurMods.board.centaur'] = mock_centaur
sys.modules['DGTCentaurMods.board.logging'] = MagicMock()
sys.modules['DGTCentaurMods.board.settings'] = MagicMock()

# Now import chess (real module, needed for game logic)
import chess


class TestFullChessGame(unittest.TestCase):
    """Integration tests for complete chess game scenarios."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Reset game state singleton for each test
        from universalchess.state.chess_game import reset_chess_game
        self.game_state = reset_chess_game()
        
        # Track callbacks
        self.moves_made = []
        self.game_over_events = []
        self.position_changes = []
        
        # Register observers
        self.game_state.on_position_change(lambda: self.position_changes.append(self.game_state.fen))
        self.game_state.on_game_over(lambda r, t: self.game_over_events.append((r, t)))
    
    def _make_move_via_piece_events(self, player_manager, from_sq: str, to_sq: str, is_capture: bool = False):
        """Simulate a move via piece events.
        
        Args:
            player_manager: The PlayerManager to route events through
            from_sq: Source square in algebraic notation (e.g., 'e2')
            to_sq: Target square in algebraic notation (e.g., 'e4')
            is_capture: Whether this is a capture move (two pieces lifted)
        """
        from_idx = chess.parse_square(from_sq)
        to_idx = chess.parse_square(to_sq)
        board = self.game_state.board
        
        if is_capture:
            # For captures, lift both pieces (order doesn't matter)
            player_manager.on_piece_event("lift", from_idx, board)
            player_manager.on_piece_event("lift", to_idx, board)
            player_manager.on_piece_event("place", to_idx, board)
        else:
            # Normal move: lift from source, place on target
            player_manager.on_piece_event("lift", from_idx, board)
            player_manager.on_piece_event("place", to_idx, board)
    
    def test_scholars_mate_game_flow(self):
        """Test a complete Scholar's Mate game (4-move checkmate).
        
        This is a quick checkmate sequence:
        1. e4 e5
        2. Bc4 Nc6  
        3. Qh5 Nf6??
        4. Qxf7# (checkmate)
        
        Expected: Game ends with checkmate, result is 1-0.
        Failure: Moves not executed correctly or checkmate not detected.
        """
        from universalchess.players.human import HumanPlayer
        from universalchess.players.manager import PlayerManager
        
        # Create two human players
        white_player = HumanPlayer()
        black_player = HumanPlayer()
        
        # Track submitted moves
        submitted_moves = []
        
        def on_move_submitted(move: chess.Move) -> bool:
            """Handle move submission from players."""
            submitted_moves.append(move)
            # Execute the move on game state
            self.game_state.push_move(move)
            return True
        
        # Create player manager
        manager = PlayerManager(
            white_player=white_player,
            black_player=black_player,
            move_callback=on_move_submitted
        )
        manager.start()
        
        # Verify both players are ready
        self.assertTrue(manager.is_ready, "Both players should be ready")
        self.assertTrue(manager.is_two_human, "Should be two human players")
        
        # === Move 1: e4 ===
        manager.request_move(self.game_state.board)
        self._make_move_via_piece_events(manager, 'e2', 'e4')
        self.assertEqual(len(submitted_moves), 1)
        self.assertEqual(submitted_moves[-1].uci(), 'e2e4')
        
        # === Move 1: ...e5 ===
        manager.request_move(self.game_state.board)
        self._make_move_via_piece_events(manager, 'e7', 'e5')
        self.assertEqual(len(submitted_moves), 2)
        self.assertEqual(submitted_moves[-1].uci(), 'e7e5')
        
        # === Move 2: Bc4 ===
        manager.request_move(self.game_state.board)
        self._make_move_via_piece_events(manager, 'f1', 'c4')
        self.assertEqual(len(submitted_moves), 3)
        self.assertEqual(submitted_moves[-1].uci(), 'f1c4')
        
        # === Move 2: ...Nc6 ===
        manager.request_move(self.game_state.board)
        self._make_move_via_piece_events(manager, 'b8', 'c6')
        self.assertEqual(len(submitted_moves), 4)
        self.assertEqual(submitted_moves[-1].uci(), 'b8c6')
        
        # === Move 3: Qh5 ===
        manager.request_move(self.game_state.board)
        self._make_move_via_piece_events(manager, 'd1', 'h5')
        self.assertEqual(len(submitted_moves), 5)
        self.assertEqual(submitted_moves[-1].uci(), 'd1h5')
        
        # === Move 3: ...Nf6?? (blunder) ===
        manager.request_move(self.game_state.board)
        self._make_move_via_piece_events(manager, 'g8', 'f6')
        self.assertEqual(len(submitted_moves), 6)
        self.assertEqual(submitted_moves[-1].uci(), 'g8f6')
        
        # Game should not be over yet
        self.assertFalse(self.game_state.is_game_over, "Game should not be over before Qxf7#")
        
        # === Move 4: Qxf7# (checkmate!) ===
        manager.request_move(self.game_state.board)
        self._make_move_via_piece_events(manager, 'h5', 'f7', is_capture=True)
        self.assertEqual(len(submitted_moves), 7)
        self.assertEqual(submitted_moves[-1].uci(), 'h5f7')
        
        # Verify game over
        self.assertTrue(self.game_state.is_game_over, "Game should be over after Qxf7#")
        self.assertEqual(self.game_state.result, '1-0', "White should win")
        self.assertEqual(self.game_state.termination, 'checkmate', "Should be checkmate")
        
        # Verify game over was notified
        self.assertEqual(len(self.game_over_events), 1)
        self.assertEqual(self.game_over_events[0], ('1-0', 'checkmate'))
        
        # Cleanup
        manager.stop()
    
    def test_game_reset_clears_state(self):
        """Test that resetting a game clears all state.
        
        After a game ends, resetting should:
        1. Clear the result and termination
        2. Reset the board to starting position
        3. Notify position change observers
        
        Expected: All state is reset to initial values.
        Failure: State persists after reset.
        """
        # Play a quick checkmate (fool's mate for speed)
        # 1. f3 e5 2. g4 Qh4#
        self.game_state.push_uci('f2f3')
        self.game_state.push_uci('e7e5')
        self.game_state.push_uci('g2g4')
        self.game_state.push_uci('d8h4')  # Qh4#
        
        # Verify game is over
        self.assertTrue(self.game_state.is_game_over)
        self.assertEqual(self.game_state.result, '0-1')
        self.assertEqual(self.game_state.termination, 'checkmate')
        
        # Reset the game
        initial_position_count = len(self.position_changes)
        self.game_state.reset()
        
        # Verify state is cleared
        self.assertFalse(self.game_state.is_game_over, "Game should not be over after reset")
        self.assertIsNone(self.game_state.result, "Result should be None after reset")
        self.assertIsNone(self.game_state.termination, "Termination should be None after reset")
        
        # Verify board is at starting position
        self.assertEqual(
            self.game_state.fen,
            chess.STARTING_FEN,
            "Board should be at starting position"
        )
        
        # Verify position change was notified
        self.assertEqual(
            len(self.position_changes), 
            initial_position_count + 1,
            "Position change should be notified on reset"
        )
    
    def test_new_game_after_checkmate(self):
        """Test starting a new game after checkmate.
        
        After a game ends with checkmate:
        1. Reset the game state
        2. Start new players
        3. Play moves normally
        
        Expected: New game works correctly after previous game ended.
        Failure: State leaks from previous game.
        """
        from universalchess.players.human import HumanPlayer
        from universalchess.players.manager import PlayerManager
        
        # === Game 1: Play to checkmate ===
        self.game_state.push_uci('f2f3')
        self.game_state.push_uci('e7e5')
        self.game_state.push_uci('g2g4')
        self.game_state.push_uci('d8h4')  # Qh4#
        
        self.assertTrue(self.game_state.is_game_over)
        game1_events = len(self.game_over_events)
        
        # === Reset for Game 2 ===
        self.game_state.reset()
        
        # Create new players for game 2
        white_player = HumanPlayer()
        black_player = HumanPlayer()
        
        moves_game2 = []
        def on_move(move: chess.Move) -> bool:
            moves_game2.append(move)
            self.game_state.push_move(move)
            return True
        
        manager = PlayerManager(
            white_player=white_player,
            black_player=black_player,
            move_callback=on_move
        )
        manager.start()
        
        # Play a few moves in game 2
        manager.request_move(self.game_state.board)
        self._make_move_via_piece_events(manager, 'e2', 'e4')
        
        manager.request_move(self.game_state.board)
        self._make_move_via_piece_events(manager, 'e7', 'e5')
        
        # Verify game 2 is in progress
        self.assertEqual(len(moves_game2), 2)
        self.assertFalse(self.game_state.is_game_over)
        self.assertTrue(self.game_state.is_game_in_progress)
        
        # No new game over events should have fired
        self.assertEqual(len(self.game_over_events), game1_events)
        
        manager.stop()
    
    def test_piece_returned_to_same_square(self):
        """Test that returning a piece to its original square doesn't make a move.
        
        When a player lifts a piece and places it back on the same square,
        no move should be submitted.
        
        Expected: No move submitted, error callback fired.
        Failure: Move is incorrectly submitted or callback not fired.
        """
        from universalchess.players.human import HumanPlayer
        from universalchess.players.manager import PlayerManager
        
        white_player = HumanPlayer()
        black_player = HumanPlayer()
        
        moves = []
        errors = []
        
        def on_move(move: chess.Move) -> bool:
            moves.append(move)
            return True
        
        def on_error(error_type: str) -> None:
            errors.append(error_type)
        
        manager = PlayerManager(
            white_player=white_player,
            black_player=black_player,
            move_callback=on_move,
            error_callback=on_error
        )
        manager.start()
        
        # Request a move
        manager.request_move(self.game_state.board)
        
        # Lift a piece and place it back
        e2_square = chess.parse_square('e2')
        manager.on_piece_event("lift", e2_square, self.game_state.board)
        manager.on_piece_event("place", e2_square, self.game_state.board)
        
        # No move should be submitted
        self.assertEqual(len(moves), 0, "No move should be submitted when piece returned to same square")
        
        # Error callback should be fired
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0], 'piece_returned')
        
        manager.stop()
    
    def test_capture_move_via_piece_events(self):
        """Test that capture moves work correctly with two pieces lifted.
        
        For captures, two pieces are lifted:
        1. The moving piece (from source)
        2. The captured piece (from target)
        
        Order doesn't matter - the place event determines the target.
        
        Expected: Capture move is correctly formed.
        Failure: Move formed incorrectly or not at all.
        """
        from universalchess.players.human import HumanPlayer
        from universalchess.players.manager import PlayerManager
        
        # Set up a position where a capture is possible
        # After 1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7#
        # Let's just do 1. e4 d5 2. exd5 (pawn capture)
        self.game_state.push_uci('e2e4')
        self.game_state.push_uci('d7d5')
        
        white_player = HumanPlayer()
        black_player = HumanPlayer()
        
        moves = []
        def on_move(move: chess.Move) -> bool:
            moves.append(move)
            self.game_state.push_move(move)
            return True
        
        manager = PlayerManager(
            white_player=white_player,
            black_player=black_player,
            move_callback=on_move
        )
        manager.start()
        
        # It's white's turn - exd5 is a capture
        manager.request_move(self.game_state.board)
        
        e4_square = chess.parse_square('e4')
        d5_square = chess.parse_square('d5')
        
        # Lift capturing pawn first
        manager.on_piece_event("lift", e4_square, self.game_state.board)
        # Lift captured pawn
        manager.on_piece_event("lift", d5_square, self.game_state.board)
        # Place on target (d5)
        manager.on_piece_event("place", d5_square, self.game_state.board)
        
        # Verify the capture was formed correctly
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].uci(), 'e4d5')
        
        # Verify the pawn on d5 is now white's
        piece_on_d5 = self.game_state.board.piece_at(d5_square)
        self.assertIsNotNone(piece_on_d5)
        self.assertEqual(piece_on_d5.piece_type, chess.PAWN)
        self.assertEqual(piece_on_d5.color, chess.WHITE)
        
        manager.stop()
    
    def test_player_manager_notifies_both_players_on_move(self):
        """Test that both players are notified when a move is made.
        
        PlayerManager.on_move_made() should notify both white and black
        players, regardless of whose turn it was.
        
        Expected: Both players receive on_move_made callback.
        Failure: One or both players not notified.
        """
        from universalchess.players.human import HumanPlayer
        from universalchess.players.manager import PlayerManager
        
        white_player = HumanPlayer()
        black_player = HumanPlayer()
        
        # Track on_move_made calls
        white_moves_notified = []
        black_moves_notified = []
        
        original_white_on_move = white_player.on_move_made
        original_black_on_move = black_player.on_move_made
        
        def white_on_move_made(move, board):
            white_moves_notified.append(move)
            original_white_on_move(move, board)
        
        def black_on_move_made(move, board):
            black_moves_notified.append(move)
            original_black_on_move(move, board)
        
        white_player.on_move_made = white_on_move_made
        black_player.on_move_made = black_on_move_made
        
        manager = PlayerManager(
            white_player=white_player,
            black_player=black_player
        )
        manager.start()
        
        # Make a move and notify
        move = chess.Move.from_uci('e2e4')
        self.game_state.push_move(move)
        manager.on_move_made(move, self.game_state.board)
        
        # Both players should be notified
        self.assertEqual(len(white_moves_notified), 1)
        self.assertEqual(len(black_moves_notified), 1)
        self.assertEqual(white_moves_notified[0], move)
        self.assertEqual(black_moves_notified[0], move)
        
        manager.stop()


class TestGameOverWidgetIntegration(unittest.TestCase):
    """Test game over widget lifecycle in a full game scenario."""
    
    def setUp(self):
        """Set up test fixtures."""
        from universalchess.state.chess_game import reset_chess_game
        self.game_state = reset_chess_game()
        # Fully reset the mock board for isolation
        mock_board.reset_mock()
        mock_board.display_manager = MagicMock()
        mock_board.display_manager.add_widget = MagicMock(return_value=MagicMock())
        mock_board.display_manager.remove_widget = MagicMock()
    
    def test_game_over_widget_shown_on_checkmate(self):
        """Test that game over notification fires on checkmate.
        
        After a checkmate:
        1. ChessGameState.notify_game_over() is called
        2. Observers receive the (result, termination) callback
        
        Expected: Game over observers are notified with correct result.
        Failure: Observers not notified.
        """
        from universalchess.managers.display import DisplayManager
        
        # Create display manager with mocked init
        with patch.object(DisplayManager, '_init_widgets'):
            import universalchess.managers.display as display_module
            display_module.get_chess_game = MagicMock(return_value=self.game_state)
            display_module._load_widgets = MagicMock()
            display_module.get_chess_clock_service = MagicMock(return_value=MagicMock())
            display_module.get_clock_state = MagicMock(return_value=MagicMock())
            
            dm = DisplayManager()
            dm._clock = MagicMock()
            dm.clock_widget = MagicMock()
            dm._time_control = 0
        
        # Register display manager as game over observer
        game_over_called = []
        def on_game_over(result, termination):
            game_over_called.append((result, termination))
        
        self.game_state.on_game_over(on_game_over)
        
        # Play to checkmate
        self.game_state.push_uci('f2f3')
        self.game_state.push_uci('e7e5')
        self.game_state.push_uci('g2g4')
        self.game_state.push_uci('d8h4')  # Qh4#
        
        # Verify game over was triggered
        self.assertEqual(len(game_over_called), 1)
        self.assertEqual(game_over_called[0], ('0-1', 'checkmate'))
    
    def test_game_over_widget_hides_on_new_game(self):
        """Test that game over widget hides itself when a new game starts.
        
        The GameOverWidget uses the observer pattern:
        1. It subscribes to ChessGameState.on_game_over() and on_position_change()
        2. It shows itself when game_over is triggered
        3. It hides itself when position_change is triggered and is_game_over is False
        
        This test verifies the widget's behavior with the actual ChessGameState.
        
        Note: Clock widget manages its own visibility via game_over observer,
        not via GameOverWidget. See test_chess_clock_widget_visibility.py.
        
        Expected: Widget hides itself on game reset.
        Failure: Widget remains visible after reset.
        """
        from universalchess.epaper.game_over import GameOverWidget
        
        # Create real game over widget with our test game state
        widget = GameOverWidget(
            0, 144, 128, 72, 
            update_callback=MagicMock(),
            game_state=self.game_state
        )
        
        # Verify widget starts hidden
        self.assertFalse(widget.visible, "Widget should start hidden")
        
        # Play to checkmate (fool's mate)
        self.game_state.push_uci('f2f3')
        self.game_state.push_uci('e7e5')
        self.game_state.push_uci('g2g4')
        self.game_state.push_uci('d8h4')  # Qh4#
        
        # Verify widget is now visible (shown by game_over observer)
        self.assertTrue(widget.visible, "Widget should be visible after checkmate")
        self.assertEqual(widget.result, '0-1')
        self.assertEqual(widget.winner, 'Black wins')
        
        # Reset the game (new game)
        self.game_state.reset()
        
        # Verify widget is now hidden (hidden by position_change observer)
        self.assertFalse(widget.visible, "Widget should be hidden after game reset")
        
        # Verify widget state was cleared
        self.assertEqual(widget.result, "")
        self.assertEqual(widget.winner, "")
        
        # Cleanup
        widget.cleanup()


if __name__ == "__main__":
    unittest.main()
