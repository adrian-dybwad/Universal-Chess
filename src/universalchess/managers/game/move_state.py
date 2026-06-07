"""Move-state tracking for physical-board interaction.

Tracks in-progress move information, including special handling for:
- castling rook-follow (after the king's two-square move, the rook must be
  physically moved to its castling square)
- king lift resign gestures (timer-based)
"""

import threading
from typing import Optional

import chess

# Board constants
BOARD_WIDTH = 8
PROMOTION_ROW_WHITE = 7
PROMOTION_ROW_BLACK = 0
INVALID_SQUARE = -1

# Move constants
MIN_UCI_MOVE_LENGTH = 4

# Kings-in-center resign/draw detection center squares: d4, d5, e4, e5
CENTER_SQUARES = {chess.D4, chess.D5, chess.E4, chess.E5}


class MoveState:
    """Tracks the state of a move in progress.

    Castling is always the king moving two squares (e1->g1 / e1->c1, etc.). Once
    that move is applied, the rook must still be physically moved to its castling
    square; `castling_rook_pending` records the expected rook (from, to) so that
    follow-up event is recognised as the completion of the castle rather than an
    illegal interaction.
    """

    def __init__(self):
        self.source_square = INVALID_SQUARE
        self.opponent_source_square = INVALID_SQUARE
        self.legal_destination_squares = []
        self.computer_move_uci = ""
        self.is_forced_move = False
        self.source_piece_color = None  # piece color when lifted (for captures)

        # Expected rook move (from_square, to_square) after a castling king move,
        # or None when no castle is awaiting its rook-follow.
        self.castling_rook_pending = None

        # King lift resign tracking
        self.king_lifted_square = INVALID_SQUARE
        self.king_lifted_color = None
        self.king_lift_timer: Optional[threading.Timer] = None
        
        # Capture square event tracking for pending moves
        # Tracks which capture squares have had events (LIFT or PLACE)
        self._capture_square_events: set = set()
        
        # Pending move source lifted tracking
        # Set to the source square when the correct piece is lifted for a pending move.
        # This allows subsequent bumps/adjustments without triggering "wrong piece" errors.
        self.pending_move_source_lifted: int = INVALID_SQUARE

    def reset(self):
        """Reset all move state variables.
        
        Also clears any pending move broadcast to the web interface.
        """
        self.source_square = INVALID_SQUARE
        self.opponent_source_square = INVALID_SQUARE
        self.legal_destination_squares = []
        self.computer_move_uci = ""
        self.is_forced_move = False
        self.source_piece_color = None
        self.castling_rook_pending = None
        self._cancel_king_lift_timer()
        self.king_lifted_square = INVALID_SQUARE
        self.king_lifted_color = None
        self._capture_square_events = set()
        self.pending_move_source_lifted = INVALID_SQUARE
        
        # Clear pending move from web broadcast
        from universalchess.services.game_broadcast import set_pending_move
        set_pending_move(None)

    def set_computer_move(self, uci_move: str, forced: bool = True):
        """Set the computer move that the player is expected to make."""
        if len(uci_move) < MIN_UCI_MOVE_LENGTH:
            return False
        self.computer_move_uci = uci_move
        self.is_forced_move = forced
        return True

    def _cancel_king_lift_timer(self):
        """Cancel any active king lift resign timer."""
        if self.king_lift_timer is not None:
            self.king_lift_timer.cancel()
            self.king_lift_timer = None
    
    def has_seen_capture_square_event(self, square: int) -> bool:
        """Check if we've seen any event (LIFT or PLACE) on a capture square.
        
        For pending capture moves, we require at least one event on the capture
        square before using the board state shortcut. This ensures the user has
        interacted with the captured piece.
        
        Args:
            square: The capture destination square to check.
            
        Returns:
            True if an event has been recorded for this square.
        """
        return square in self._capture_square_events
    
    def record_capture_square_event(self, square: int) -> None:
        """Record that an event occurred on a capture square.
        
        Args:
            square: The square where the event occurred.
        """
        self._capture_square_events.add(square)
    
    def clear_capture_square_events(self) -> None:
        """Clear all recorded capture square events."""
        self._capture_square_events.clear()


__all__ = [
    "MoveState",
    "BOARD_WIDTH",
    "PROMOTION_ROW_WHITE",
    "PROMOTION_ROW_BLACK",
    "INVALID_SQUARE",
    "MIN_UCI_MOVE_LENGTH",
    "CENTER_SQUARES",
]


