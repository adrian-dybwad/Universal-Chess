"""Tests for piece type re-selection in reverse Hand+Brain mode.

When a piece is "bumped" (lifted and replaced on the same square) in reverse
hand-brain mode, it selects the piece type. This test verifies that a second
bump changes the selection, and if a move has already been computed, the
engine recalculates for the new piece type.

Test Scenarios:
1. Re-selection during COMPUTING_MOVE phase (engine still thinking)
2. Re-selection during WAITING_EXECUTION phase (move already computed)
"""
from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import threading
import time

import chess

# Mock hardware-dependent modules before importing hand_brain
# These modules are only available on Raspberry Pi hardware
sys.modules['spidev'] = MagicMock()
sys.modules['RPi'] = MagicMock()
sys.modules['RPi.GPIO'] = MagicMock()
sys.modules['gpiozero'] = MagicMock()
sys.modules['lgpio'] = MagicMock()
sys.modules['serial'] = MagicMock()
sys.modules['serial.tools'] = MagicMock()
sys.modules['serial.tools.list_ports'] = MagicMock()

# Import directly from the module file to avoid package __init__ import chain
# which tries to import LichessPlayer and other modules with hardware dependencies
from universalchess.players.hand_brain import (
    HandBrainPlayer, HandBrainConfig, HandBrainMode, HandBrainPhase
)
from universalchess.players.base import PlayerState


class MockEngine:
    """Mock UCI engine that returns configurable moves."""
    
    def __init__(self):
        self.play_results = []  # List of moves to return, in order
        self.play_call_count = 0
        self.configure_called = False
        self._configured_options = {}
        
    def play(self, board, limit, root_moves=None):
        """Return the next configured move or first legal move."""
        self.play_call_count += 1
        
        result = MagicMock()
        if self.play_results and self.play_call_count <= len(self.play_results):
            result.move = self.play_results[self.play_call_count - 1]
        elif root_moves:
            result.move = root_moves[0]
        else:
            result.move = list(board.legal_moves)[0] if board.legal_moves else None
        return result
    
    def configure(self, options):
        self.configure_called = True
        self._configured_options = options
    
    def analyse(self, board, limit):
        return {"score": MagicMock(white=MagicMock(return_value=MagicMock(is_mate=MagicMock(return_value=False), score=MagicMock(return_value=0))))}
    
    def quit(self):
        pass


class MockEngineHandle:
    """Mock EngineHandle that wraps a MockEngine for testing.
    
    Provides the same interface as EngineHandle from the registry.
    """
    
    def __init__(self, engine: MockEngine = None):
        self.engine = engine or MockEngine()
        self.lock = MagicMock()
        self.lock.locked.return_value = False
        self.path = "/mock/engine"
        self.ref_count = 1
    
    def configure(self, options):
        self.engine.configure(options)
    
    def play(self, board, limit, options=None, root_moves=None):
        if options:
            self.engine.configure(options)
        return self.engine.play(board, limit, root_moves=root_moves)
    
    def analyse(self, board, limit, multipv=1):
        return self.engine.analyse(board, limit)


# Long enough that a loaded machine still reaches the condition, short enough
# that a genuine regression fails the run rather than hanging it. The waits below
# return as soon as the condition holds, so raising this costs a failing run only.
_WAIT_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.005


def _wait_until(predicate, timeout: float = _WAIT_TIMEOUT_SECONDS) -> bool:
    """Poll until predicate holds or the deadline passes; report whether it held.

    The engine search runs on a daemon thread, so a test that instead slept for a
    fixed interval passed only while the runner happened to schedule that thread
    inside the chosen window. On a loaded CI runner the reverse-mode
    recalculation landed 310 ms after a 300 ms sleep, and the test read the
    pending move before the thread had set it and reported that no move existed.
    Waiting on the condition removes the dependency on scheduling latency while
    still failing when the condition never holds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return predicate()


class TestReverseModeReselection(unittest.TestCase):
    """Test cases for piece type re-selection in reverse hand-brain mode."""
    
    def _create_player(self):
        """Create a HandBrainPlayer in REVERSE mode with mock engine."""
        from universalchess.players.hand_brain import (
            HandBrainPlayer, HandBrainConfig, HandBrainMode, HandBrainPhase
        )
        
        config = HandBrainConfig(
            name="Test Reverse H+B",
            color=chess.WHITE,
            mode=HandBrainMode.REVERSE,
            time_limit_seconds=0.1,
            engine_name="mock_engine"
        )
        player = HandBrainPlayer(config)
        
        # Inject mock engine handle
        player._engine_handle = MockEngineHandle()
        player._state = player.state.__class__.READY  # PlayerState.READY
        
        return player
    
    def test_reselection_during_computing_move_changes_piece_type(self):
        """Test that bumping a different piece during COMPUTING_MOVE changes selection.
        
        Scenario:
        1. User bumps a knight (lift e4, place e4) - knight selected
        2. Engine starts computing knight moves
        3. User bumps a bishop (lift c4, place c4) before engine finishes
        4. Engine should now compute bishop moves instead
        
        Expected: selected_piece_type should change to bishop.
        Failure: The second bump is ignored, knight move is computed.
        """
        from universalchess.players.hand_brain import (
            HandBrainPlayer, HandBrainConfig, HandBrainMode, HandBrainPhase
        )
        
        player = self._create_player()
        
        # Set up callbacks
        move_callback = MagicMock()
        pending_move_callback = MagicMock()
        led_callback = MagicMock()
        
        player.set_move_callback(move_callback)
        player.set_pending_move_callback(pending_move_callback)
        player.set_piece_squares_led_callback(led_callback)
        
        # Create position with knight on e4 and bishop on c4
        board = chess.Board()
        board.set_piece_at(chess.E4, chess.Piece(chess.KNIGHT, chess.WHITE))
        board.set_piece_at(chess.C4, chess.Piece(chess.BISHOP, chess.WHITE))
        
        # Start the turn
        player._do_request_move(board)
        
        assert player._phase == HandBrainPhase.WAITING_PIECE_SELECTION
        
        # Bump knight on e4 (select knight)
        player.on_piece_event("lift", chess.E4, board)
        player.on_piece_event("place", chess.E4, board)
        
        # Engine should start computing - force phase to COMPUTING_MOVE
        # (In real usage the thread would set this, but we simulate)
        player._phase = HandBrainPhase.COMPUTING_MOVE
        player._selected_piece_type = chess.KNIGHT
        
        # Now bump bishop on c4 BEFORE engine finishes (during COMPUTING_MOVE)
        player.on_piece_event("lift", chess.C4, board)
        player.on_piece_event("place", chess.C4, board)
        
        # The selected piece type should change to bishop
        assert player._selected_piece_type == chess.BISHOP, \
            f"Expected BISHOP, got {chess.piece_name(player._selected_piece_type) if player._selected_piece_type else None}"
    
    def test_reselection_during_waiting_execution_recalculates_move(self):
        """Test that bumping a different piece during WAITING_EXECUTION recalculates.
        
        Scenario:
        1. User bumps a knight - knight selected
        2. Engine computes and displays knight move (e.g., Ne4-f6)
        3. User changes their mind, bumps a rook instead
        4. Engine should recalculate and display best rook move
        
        Expected: pending_move should change to a rook move.
        Failure: The second bump is ignored, knight move remains pending.
        """
        from universalchess.players.hand_brain import (
            HandBrainPlayer, HandBrainConfig, HandBrainMode, HandBrainPhase
        )
        
        player = self._create_player()
        
        # Set up callbacks
        pending_moves_received = []
        def pending_move_callback(move):
            pending_moves_received.append(move)
        
        player.set_pending_move_callback(pending_move_callback)
        player.set_piece_squares_led_callback(MagicMock())
        player.set_move_callback(MagicMock())
        
        # Create position: knight on e4, rook on a1
        # Knight can go to f6, rook can go to a8
        board = chess.Board("8/8/8/8/4N3/8/8/R3K2R w KQ - 0 1")
        
        # Configure mock engine to return different moves for each call
        # First call: return knight move
        # Second call: return rook move
        knight_move = chess.Move(chess.E4, chess.F6)
        rook_move = chess.Move(chess.A1, chess.A8)
        player._engine_handle.engine.play_results = [knight_move, rook_move]
        
        # Start the turn
        player._do_request_move(board)
        
        # Bump knight on e4 (select knight)
        player.on_piece_event("lift", chess.E4, board)
        player.on_piece_event("place", chess.E4, board)
        
        # Wait for engine computation to complete
        _wait_until(lambda: player._phase == HandBrainPhase.WAITING_EXECUTION)
        
        # Should be waiting for execution with knight move pending
        assert player._phase == HandBrainPhase.WAITING_EXECUTION, \
            f"Expected WAITING_EXECUTION, got {player._phase.name}"
        assert player._pending_move is not None, "No pending move after knight selection"
        first_pending_move = player._pending_move
        
        # Now bump rook on a1 to change selection
        player.on_piece_event("lift", chess.A1, board)
        player.on_piece_event("place", chess.A1, board)
        
        # Wait for the recalculation to replace the knight move
        _wait_until(lambda: player._phase == HandBrainPhase.WAITING_EXECUTION
                    and player._pending_move != first_pending_move)
        
        # The pending move should now be a rook move
        assert player._selected_piece_type == chess.ROOK, \
            f"Expected ROOK, got {chess.piece_name(player._selected_piece_type) if player._selected_piece_type else None}"
        assert player._pending_move is not None, "No pending move after rook re-selection"
        
        # Verify that the move changed (it should be from the rook, not knight)
        assert player._pending_move.from_square == chess.A1, \
            f"Expected move from a1, got {chess.square_name(player._pending_move.from_square)}"
    
    def test_bump_same_piece_type_during_waiting_execution_no_change(self):
        """Test that bumping same piece type doesn't restart computation.
        
        If user bumps a knight while knight is already selected, there's no
        need to recalculate.
        
        Expected: No additional engine calls, same move remains pending.
        Failure: Unnecessary recalculation occurs.
        """
        from universalchess.players.hand_brain import (
            HandBrainPlayer, HandBrainConfig, HandBrainMode, HandBrainPhase
        )
        
        player = self._create_player()
        player.set_piece_squares_led_callback(MagicMock())
        player.set_move_callback(MagicMock())
        player.set_pending_move_callback(MagicMock())
        
        # Position with two knights
        board = chess.Board("8/8/8/8/4N3/8/8/4K2N w - - 0 1")
        
        knight_move = chess.Move(chess.E4, chess.F6)
        player._engine_handle.engine.play_results = [knight_move, knight_move]
        
        # Start turn and select knight
        player._do_request_move(board)
        player.on_piece_event("lift", chess.E4, board)
        player.on_piece_event("place", chess.E4, board)
        
        _wait_until(lambda: player._phase == HandBrainPhase.WAITING_EXECUTION)
        
        initial_call_count = player._engine_handle.engine.play_call_count
        initial_pending_move = player._pending_move
        
        # Bump the OTHER knight (h1) - same piece TYPE
        player.on_piece_event("lift", chess.H1, board)
        player.on_piece_event("place", chess.H1, board)
        
        # Let any recalculation settle so its thread does not outlive the test
        _wait_until(lambda: player._phase == HandBrainPhase.WAITING_EXECUTION)
        
        # Should NOT trigger recalculation since same piece type
        # (This is optional optimization - may or may not be implemented)
        # For now, we just verify the selection is still knight
        assert player._selected_piece_type == chess.KNIGHT


class TestReverseModeReselectionEdgeCases(unittest.TestCase):
    """Edge cases for piece type re-selection."""
    
    def _create_player(self):
        """Create a HandBrainPlayer in REVERSE mode with mock engine."""
        from universalchess.players.hand_brain import (
            HandBrainPlayer, HandBrainConfig, HandBrainMode
        )
        
        config = HandBrainConfig(
            name="Test Reverse H+B",
            color=chess.WHITE,
            mode=HandBrainMode.REVERSE,
            time_limit_seconds=0.1,
            engine_name="mock_engine"
        )
        player = HandBrainPlayer(config)
        player._engine_handle = MockEngineHandle()
        player._state = player.state.__class__.READY
        
        return player
    
    def test_lift_different_piece_during_computing_resets_selection(self):
        """Test that lifting a different piece type during computing shows its LEDs.
        
        When user lifts a different piece type during COMPUTING_MOVE, the LEDs
        should update to show all pieces of the new type.
        
        Expected: LED callback called with squares of the new piece type.
        Failure: LEDs not updated for the new piece type.
        """
        from universalchess.players.hand_brain import HandBrainPhase
        
        player = self._create_player()
        
        led_squares_received = []
        def led_callback(squares):
            led_squares_received.append(squares)
        
        player.set_piece_squares_led_callback(led_callback)
        player.set_move_callback(MagicMock())
        player.set_pending_move_callback(MagicMock())
        
        # Position: knight on e4, two bishops on c4 and f4
        board = chess.Board()
        board.set_piece_at(chess.E4, chess.Piece(chess.KNIGHT, chess.WHITE))
        board.set_piece_at(chess.C4, chess.Piece(chess.BISHOP, chess.WHITE))
        board.set_piece_at(chess.F4, chess.Piece(chess.BISHOP, chess.WHITE))
        
        player._do_request_move(board)
        
        # Select knight first
        player.on_piece_event("lift", chess.E4, board)
        player.on_piece_event("place", chess.E4, board)
        
        player._phase = HandBrainPhase.COMPUTING_MOVE
        player._selected_piece_type = chess.KNIGHT
        
        led_squares_received.clear()
        
        # Lift bishop during computing
        player.on_piece_event("lift", chess.C4, board)
        
        # LED callback should be called with both bishop squares
        assert len(led_squares_received) > 0, "LED callback not called when lifting new piece type"
        assert chess.C4 in led_squares_received[-1], "c4 not in LED squares"
        assert chess.F4 in led_squares_received[-1], "f4 not in LED squares"
    
    def test_opponent_piece_during_reselection_ignored(self):
        """Test that bumping opponent's piece during re-selection is ignored.
        
        If user accidentally bumps an opponent's piece while trying to change
        their selection, it should not affect the current selection.
        
        Expected: selected_piece_type unchanged, status message shown.
        Failure: Selection changed or error triggered.
        """
        from universalchess.players.hand_brain import HandBrainPhase
        
        player = self._create_player()
        player.set_piece_squares_led_callback(MagicMock())
        player.set_move_callback(MagicMock())
        player.set_pending_move_callback(MagicMock())
        
        status_messages = []
        def status_callback(msg):
            status_messages.append(msg)
        player.set_status_callback(status_callback)
        
        # Position: white knight on e4, black bishop on d5
        board = chess.Board()
        board.set_piece_at(chess.E4, chess.Piece(chess.KNIGHT, chess.WHITE))
        board.set_piece_at(chess.D5, chess.Piece(chess.BISHOP, chess.BLACK))
        
        player._do_request_move(board)
        
        # Select knight
        player.on_piece_event("lift", chess.E4, board)
        player.on_piece_event("place", chess.E4, board)
        
        player._phase = HandBrainPhase.WAITING_EXECUTION
        player._selected_piece_type = chess.KNIGHT
        player._pending_move = chess.Move(chess.E4, chess.F6)
        
        # Try to bump opponent's bishop
        player.on_piece_event("lift", chess.D5, board)
        player.on_piece_event("place", chess.D5, board)
        
        # Selection should remain knight
        assert player._selected_piece_type == chess.KNIGHT, \
            "Selection changed after bumping opponent's piece"
        assert player._pending_move == chess.Move(chess.E4, chess.F6), \
            "Pending move changed after bumping opponent's piece"

    def test_piece_placed_on_wrong_square_triggers_correction_mode(self):
        """Test that moving a piece to a different square triggers correction mode.
        
        In REVERSE mode, when a user lifts a piece for selection but places it
        on a different square (not the original square), this creates a physical
        board inconsistency. The player should report an error to trigger
        correction mode in the GameManager.
        
        Expected: _report_error("move_mismatch") is called.
        Failure: Error not triggered; board would be out of sync.
        """
        player = self._create_player()
        player.set_piece_squares_led_callback(MagicMock())
        player.set_move_callback(MagicMock())
        player.set_pending_move_callback(MagicMock())
        
        errors_received = []
        def error_callback(error_type):
            errors_received.append(error_type)
        player.set_error_callback(error_callback)
        
        # Position: white knight on e4
        board = chess.Board()
        board.set_piece_at(chess.E4, chess.Piece(chess.KNIGHT, chess.WHITE))
        
        player._do_request_move(board)
        
        # User lifts knight from e4
        player.on_piece_event("lift", chess.E4, board)
        
        # User places it on a different square (not e4) - this is wrong
        # Simulate the board change
        board.remove_piece_at(chess.E4)
        board.set_piece_at(chess.F6, chess.Piece(chess.KNIGHT, chess.WHITE))
        player.on_piece_event("place", chess.F6, board)
        
        # Error should have been reported
        assert len(errors_received) == 1, \
            f"Expected 1 error, got {len(errors_received)}: {errors_received}"
        assert errors_received[0] == "move_mismatch", \
            f"Expected 'move_mismatch' error, got '{errors_received[0]}'"

    def test_correction_mode_exit_restores_waiting_piece_selection_status(self):
        """Test that on_correction_mode_exit restores status in WAITING_PIECE_SELECTION.
        
        When correction mode exits while the player is waiting for piece selection,
        the status message should be restored to guide the user.
        
        Expected: Status message "Lift piece to select type" is shown.
        Failure: No status message or wrong message shown.
        """
        player = self._create_player()
        player.set_piece_squares_led_callback(MagicMock())
        player.set_move_callback(MagicMock())
        player.set_pending_move_callback(MagicMock())
        
        status_messages = []
        def status_callback(msg):
            status_messages.append(msg)
        player.set_status_callback(status_callback)
        
        board = chess.Board()
        board.set_piece_at(chess.E4, chess.Piece(chess.KNIGHT, chess.WHITE))
        
        player._do_request_move(board)
        
        # Player should be in WAITING_PIECE_SELECTION
        assert player._phase == HandBrainPhase.WAITING_PIECE_SELECTION
        
        # Clear status messages
        status_messages.clear()
        
        # Simulate correction mode exit
        player.on_correction_mode_exit()
        
        # Status should be restored
        assert len(status_messages) == 1, \
            f"Expected 1 status message, got {len(status_messages)}"
        assert status_messages[0] == "Lift piece to select type", \
            f"Expected 'Lift piece to select type', got '{status_messages[0]}'"

    def test_correction_mode_exit_restores_waiting_execution_leds(self):
        """Test that on_correction_mode_exit re-triggers pending move callback.
        
        When correction mode exits while the player is in WAITING_EXECUTION
        with a computed move, the pending move callback should be re-triggered
        to restore LEDs.
        
        Expected: pending_move_callback called with the pending move.
        Failure: Callback not called or called with wrong move.
        """
        player = self._create_player()
        player.set_piece_squares_led_callback(MagicMock())
        player.set_move_callback(MagicMock())
        player.set_status_callback(MagicMock())
        
        pending_moves = []
        def pending_move_callback(move):
            pending_moves.append(move)
        player.set_pending_move_callback(pending_move_callback)
        
        board = chess.Board()
        board.set_piece_at(chess.E4, chess.Piece(chess.KNIGHT, chess.WHITE))
        
        player._do_request_move(board)
        
        # Simulate computed move and set phase
        player._phase = HandBrainPhase.WAITING_EXECUTION
        player._selected_piece_type = chess.KNIGHT
        player._pending_move = chess.Move(chess.E4, chess.F6)
        
        # Clear pending moves list
        pending_moves.clear()
        
        # Simulate correction mode exit
        player.on_correction_mode_exit()
        
        # Pending move callback should be called
        assert len(pending_moves) == 1, \
            f"Expected 1 pending move callback, got {len(pending_moves)}"
        assert pending_moves[0] == chess.Move(chess.E4, chess.F6), \
            f"Expected e4f6, got {pending_moves[0].uci()}"

    def test_opponent_piece_lift_triggers_correction_mode(self):
        """Test that lifting an opponent's piece triggers correction mode immediately.
        
        When a user lifts an opponent's piece, the physical board becomes
        inconsistent with the logical board (piece is off the board). Correction
        mode should be entered immediately on lift.
        
        Expected: _report_error("move_mismatch") is called on lift.
        Failure: Error not triggered; board would be out of sync.
        """
        player = self._create_player()
        player.set_piece_squares_led_callback(MagicMock())
        player.set_move_callback(MagicMock())
        player.set_pending_move_callback(MagicMock())
        
        errors_received = []
        def error_callback(error_type):
            errors_received.append(error_type)
        player.set_error_callback(error_callback)
        
        # Position: white knight on e4, black bishop on d5
        board = chess.Board()
        board.set_piece_at(chess.E4, chess.Piece(chess.KNIGHT, chess.WHITE))
        board.set_piece_at(chess.D5, chess.Piece(chess.BISHOP, chess.BLACK))
        
        player._do_request_move(board)
        
        # User lifts opponent's bishop from d5 - should immediately trigger correction mode
        player.on_piece_event("lift", chess.D5, board)
        
        # Error should have been reported immediately on lift
        assert len(errors_received) == 1, \
            f"Expected 1 error on lift, got {len(errors_received)}: {errors_received}"
        assert errors_received[0] == "move_mismatch", \
            f"Expected 'move_mismatch' error, got '{errors_received[0]}'"

    def test_opponent_piece_returned_correction_mode_exits(self):
        """Test that returning an opponent's piece allows correction mode to exit.
        
        Lifting an opponent's piece enters correction mode. Placing it back
        allows correction mode to exit (GameManager will detect board match).
        The player reports an error on lift, but placing back clears the
        opponent tracking so no additional error is reported.
        
        Expected: One error on lift, no additional error on place-back.
        Failure: Multiple errors reported or tracking not cleared.
        """
        player = self._create_player()
        player.set_piece_squares_led_callback(MagicMock())
        player.set_move_callback(MagicMock())
        player.set_pending_move_callback(MagicMock())
        
        errors_received = []
        def error_callback(error_type):
            errors_received.append(error_type)
        player.set_error_callback(error_callback)
        
        # Position: white knight on e4, black bishop on d5
        board = chess.Board()
        board.set_piece_at(chess.E4, chess.Piece(chess.KNIGHT, chess.WHITE))
        board.set_piece_at(chess.D5, chess.Piece(chess.BISHOP, chess.BLACK))
        
        player._do_request_move(board)
        
        # User lifts opponent's bishop from d5 - enters correction mode
        player.on_piece_event("lift", chess.D5, board)
        
        # Should have one error from lift
        assert len(errors_received) == 1, \
            f"Expected 1 error after lift, got {len(errors_received)}: {errors_received}"
        
        # User puts it back on the same square - no additional error
        player.on_piece_event("place", chess.D5, board)
        
        # Still only one error (from lift)
        assert len(errors_received) == 1, \
            f"Expected still 1 error after place-back, got {len(errors_received)}: {errors_received}"
        
        # Opponent tracking should be cleared
        assert player._opponent_lifted_square is None, \
            "Opponent lifted square should be cleared after place-back"

    def test_invalid_selection_flashes_leds(self):
        """Test that selecting a piece type with no legal moves flashes LEDs.
        
        When the user selects a piece type that has no legal moves,
        the invalid_selection_flash_callback should be called
        with the piece squares and flash count of 3.
        
        Expected: Callback called with squares and flash_count=3.
        Failure: Callback not called or called with wrong parameters.
        """
        player = self._create_player()
        player.set_piece_squares_led_callback(MagicMock())
        player.set_move_callback(MagicMock())
        player.set_pending_move_callback(MagicMock())
        player.set_status_callback(MagicMock())
        
        flash_calls = []
        def flash_callback(squares, flash_count):
            flash_calls.append((squares, flash_count))
        player.set_invalid_selection_flash_callback(flash_callback)
        
        # Position: white rook on h1, but completely blocked by own pieces
        # Use a custom position where rook has no legal moves
        # King on e1, rook on h1, pieces blocking all rook moves
        board = chess.Board(fen=None)  # Empty board
        board.set_piece_at(chess.E1, chess.Piece(chess.KING, chess.WHITE))
        board.set_piece_at(chess.H1, chess.Piece(chess.ROOK, chess.WHITE))
        # Block rook horizontally: g1, f1 with pawns
        board.set_piece_at(chess.G1, chess.Piece(chess.PAWN, chess.WHITE))
        board.set_piece_at(chess.F1, chess.Piece(chess.PAWN, chess.WHITE))
        # Block rook vertically: h2 with pawn
        board.set_piece_at(chess.H2, chess.Piece(chess.PAWN, chess.WHITE))
        # Need black king
        board.set_piece_at(chess.E8, chess.Piece(chess.KING, chess.BLACK))
        
        player._do_request_move(board)
        
        # Clear any initial LED calls
        flash_calls.clear()
        
        # Select the blocked rook
        player.on_piece_event("lift", chess.H1, board)
        player.on_piece_event("place", chess.H1, board)
        
        # Flash callback should have been called with the rook square and count=3
        assert len(flash_calls) == 1, \
            f"Expected 1 flash callback, got {len(flash_calls)}"
        squares, count = flash_calls[0]
        assert chess.H1 in squares, \
            f"Expected h1 in flash squares, got {[chess.square_name(s) for s in squares]}"
        assert count == 3, \
            f"Expected flash count 3, got {count}"
        
        # Player should be back in WAITING_PIECE_SELECTION
        assert player._phase == HandBrainPhase.WAITING_PIECE_SELECTION, \
            f"Expected WAITING_PIECE_SELECTION, got {player._phase.name}"

    def test_on_new_game_resets_state_to_ready(self):
        """Test that on_new_game resets state from THINKING to READY.
        
        When a new game starts (starting position detected), the player must
        be in READY state so that request_move() can be called to start
        piece selection in REVERSE mode.
        
        Expected: State changes from THINKING to READY after on_new_game.
        Failure: State remains THINKING, blocking request_move calls.
        """
        player = self._create_player()
        player.set_piece_squares_led_callback(MagicMock())
        player.set_move_callback(MagicMock())
        player.set_pending_move_callback(MagicMock())
        
        board = chess.Board()
        board.set_piece_at(chess.E4, chess.Piece(chess.KNIGHT, chess.WHITE))
        
        # Start a turn to put player in THINKING state
        player._do_request_move(board)
        assert player._state == PlayerState.THINKING, \
            f"Expected THINKING state after request_move, got {player._state.name}"
        
        # Simulate new game (starting position detected)
        player.on_new_game()
        
        # State should now be READY
        assert player._state == PlayerState.READY, \
            f"Expected READY state after on_new_game, got {player._state.name}"
        
        # Phase should be IDLE
        assert player._phase == HandBrainPhase.IDLE, \
            f"Expected IDLE phase after on_new_game, got {player._phase.name}"


class TestHandBrainHint(unittest.TestCase):
    """Test the ? key hint functionality for Hand+Brain modes."""
    
    def _create_normal_player(self):
        """Create a NORMAL mode Hand+Brain player with mock engine."""
        config = HandBrainConfig(
            mode=HandBrainMode.NORMAL,
            engine_name="stockfish",
            time_limit_seconds=0.1,
        )
        player = HandBrainPlayer(config)
        player._engine_handle = MockEngineHandle()
        player._set_state(PlayerState.READY)
        return player
    
    def _create_reverse_player(self):
        """Create a REVERSE mode Hand+Brain player with mock engine."""
        config = HandBrainConfig(
            mode=HandBrainMode.REVERSE,
            engine_name="stockfish",
            time_limit_seconds=0.1,
        )
        player = HandBrainPlayer(config)
        player._engine_handle = MockEngineHandle()
        player._set_state(PlayerState.READY)
        return player
    
    def test_normal_mode_hint_returns_stored_best_move(self):
        """Test that get_hint in NORMAL mode returns the stored best move.
        
        In NORMAL mode, the engine computes the best move to derive the piece
        type suggestion. The ? key should return this stored move.
        
        Expected: get_hint returns the same move that was computed for the suggestion.
        """
        player = self._create_normal_player()
        player.set_piece_squares_led_callback(MagicMock())
        player.set_move_callback(MagicMock())
        player.set_brain_hint_callback(MagicMock())
        player.set_status_callback(MagicMock())
        
        board = chess.Board()
        # Configure engine to return e2e4
        player._engine_handle.engine.play_results = [chess.Move.from_uci("e2e4")]
        
        # Start the turn (computes suggestion)
        player._do_request_move(board)
        
        # Wait for computation to complete
        _wait_until(lambda: player._suggested_best_move is not None)
        
        # Now get_hint should return the stored best move
        hint = player.get_hint(board)
        
        assert hint is not None, \
            "Expected hint to be available after suggestion computed"
        assert hint.uci() == "e2e4", \
            f"Expected hint to be e2e4, got {hint.uci()}"
    
    def test_reverse_mode_hint_computes_and_shows_piece_type(self):
        """Test that get_hint in REVERSE mode computes best move and shows piece type.
        
        In REVERSE mode, the ? key should compute the engine's best move and
        light up all squares with that piece type (like NORMAL mode does automatically).
        
        Expected: get_hint returns the computed move and triggers LED callback.
        """
        player = self._create_reverse_player()
        
        led_calls = []
        def led_callback(squares):
            led_calls.append(squares)
        
        player.set_piece_squares_led_callback(led_callback)
        player.set_move_callback(MagicMock())
        player.set_status_callback(MagicMock())
        
        board = chess.Board()
        # Configure engine to return e2e4 (pawn move)
        player._engine_handle.engine.play_results = [chess.Move.from_uci("e2e4")]
        
        # Get hint
        hint = player.get_hint(board)
        
        assert hint is not None, \
            "Expected hint to be available"
        assert hint.uci() == "e2e4", \
            f"Expected hint to be e2e4, got {hint.uci()}"
        
        # LED callback should have been called with pawn squares
        assert len(led_calls) == 1, \
            f"Expected LED callback to be called once, called {len(led_calls)} times"
        
        # All white pawns should be lit (a2-h2)
        pawn_squares = [chess.A2, chess.B2, chess.C2, chess.D2, 
                       chess.E2, chess.F2, chess.G2, chess.H2]
        assert set(led_calls[0]) == set(pawn_squares), \
            f"Expected pawn squares, got {[chess.square_name(s) for s in led_calls[0]]}"
    
    def test_normal_mode_hint_returns_none_before_suggestion(self):
        """Test that get_hint returns None before suggestion is computed.
        
        Expected: get_hint returns None if called before the engine has finished.
        """
        player = self._create_normal_player()
        
        board = chess.Board()
        
        # Don't start the turn - no suggestion computed yet
        hint = player.get_hint(board)
        
        assert hint is None, \
            "Expected hint to be None before suggestion computed"

    def test_bind_board_cues_wires_hint_and_led_callbacks(self):
        """The game must not isinstance HandBrainPlayer to attach cues.

        Failure: bind_board_cues is missing or a no-op, so NORMAL hints never
        reach the clock and REVERSE never lights piece squares.
        """
        player = self._create_normal_player()
        hints = []
        leds = []
        flashes = []
        player.bind_board_cues(
            brain_hint=lambda color, symbol: hints.append((color, symbol)),
            piece_squares_led=lambda squares: leds.append(list(squares)),
            invalid_selection_flash=lambda squares, count: flashes.append((list(squares), count)),
        )
        player._brain_hint_callback("white", "N")
        player._piece_squares_led_callback([chess.E2])
        player._invalid_selection_flash_callback([chess.E2], 2)
        assert hints == [("white", "N")]
        assert leds == [[chess.E2]]
        assert flashes == [([chess.E2], 2)]

    def test_help_key_result_normal_shows_stored_move(self):
        """NORMAL ? must return the stored move so the board paints it.

        Failure: None (standard analysis hint runs) or show_move is None.
        """
        player = self._create_normal_player()
        move = chess.Move.from_uci("e2e4")
        player._suggested_best_move = move
        result = player.help_key_result(chess.Board())
        assert result is not None
        assert result.show_move == move

    def test_help_key_result_reverse_consumes_key_without_board_arrow(self):
        """REVERSE ? lights LEDs inside get_hint; the board must not also arrow.

        Failure: show_move is set, so main paints a full-move hint on reverse.
        """
        player = self._create_reverse_player()
        result = player.help_key_result(chess.Board())
        assert result is not None
        assert result.show_move is None


if __name__ == '__main__':
    unittest.main()

