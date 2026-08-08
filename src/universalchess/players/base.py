# Player Base Class
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Abstract base class for all players. A player is an entity that makes
# moves in a chess game. Each game has two players (White and Black).
#
# All players receive piece events and submit moves via callback:
# - HumanPlayer: Constructs moves from piece events
# - EnginePlayer: Computes moves, piece events confirm execution
# - LichessPlayer: Receives moves from server, piece events confirm execution
#
# The game validates all submitted moves the same way.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Callable, List

import chess


class PlayerState(Enum):
    """State machine states for player lifecycle.
    
    All players follow this state machine:
    - UNINITIALIZED: Not yet started
    - INITIALIZING: Loading resources, connecting, etc.
    - READY: Ready to play, waiting for turn
    - THINKING: Computing/waiting for a move
    - ERROR: Error state, cannot continue
    - STOPPED: Cleanly stopped
    """
    UNINITIALIZED = auto()
    INITIALIZING = auto()
    READY = auto()
    THINKING = auto()
    ERROR = auto()
    STOPPED = auto()


class PlayerType(Enum):
    """Type of player - determines how moves are sourced.
    
    HUMAN: Moves come from physical board interactions.
           The game waits for the human to move pieces.
    
    ENGINE: Moves come from a UCI chess engine.
            The engine computes moves, human executes them on board.
    
    LICHESS: Moves come from the Lichess server.
             For online games, the server determines the move.
    
    REMOTE: Moves come from a remote human (network play).
            Another human provides moves, local human executes them.
    """
    HUMAN = auto()
    ENGINE = auto()
    LICHESS = auto()
    REMOTE = auto()


@dataclass
class PlayerConfig:
    """Base configuration for players.
    
    Subclasses should extend this with player-specific settings.
    
    Attributes:
        name: Human-readable name for display/logging.
        color: The color this player plays (WHITE or BLACK).
               Set when the player is assigned to a game.
    """
    name: str = "Player"
    color: Optional[chess.Color] = None


class Player(ABC):
    """Abstract base class for chess players.
    
    A player is an entity that makes moves in a chess game. The game
    has two players (White and Black), and on each turn the appropriate
    player provides a move.
    
    All players submit moves via the move_callback. The difference is
    how they determine what move to submit:
    
    - HumanPlayer: Constructs a move from piece events (lift A, place B)
    - EnginePlayer: Computes a move, then validates piece events match
    - LichessPlayer: Receives a move from server, validates piece events match
    
    Key Methods:
    - request_move(): Called when it's this player's turn
    - on_piece_event(): Called when a piece is lifted/placed on the board
    - on_move_made(): Notify that a move was made (by either player)
    - can_resign(): Whether this player can be resigned via the board
    
    Move Flow:
    1. request_move() is called when it's this player's turn
    2. on_piece_event() is called for each lift/place event
    3. Player determines a move and calls move_callback
    4. Game validates the move and executes it (or enters correction mode)
    
    Thread Safety:
    Players may be called from multiple threads. Implementations should
    ensure thread safety, especially for request_move() which may spawn
    background threads.
    """
    
    def __init__(self, config: Optional[PlayerConfig] = None):
        """Initialize the player.
        
        Args:
            config: Configuration for this player. If None, uses defaults.
        """
        self._config = config or PlayerConfig()
        self._state = PlayerState.UNINITIALIZED
        self._error_message: Optional[str] = None
        self._color: Optional[chess.Color] = config.color if config else None
        
        # Callbacks set by the game coordinator
        self._move_callback: Optional[Callable[[chess.Move], bool]] = None
        self._pending_move_callback: Optional[Callable[[chess.Move], None]] = None
        self._status_callback: Optional[Callable[[str], None]] = None
        self._error_callback: Optional[Callable[[str], None]] = None
        self._ready_callback: Optional[Callable[[], None]] = None
        
        # Track lifted squares for move formation.
        # For captures, two pieces are lifted (moving piece + captured piece).
        # Order doesn't matter - when placed, we determine source from context.
        self._lifted_squares: List[int] = []
        
        # Queued board position (if request_move called before player ready)
        self._pending_board: Optional[chess.Board] = None
    
    # =========================================================================
    # Properties
    # =========================================================================
    
    @property
    def name(self) -> str:
        """Human-readable name of this player."""
        return self._config.name
    
    @property
    def color(self) -> Optional[chess.Color]:
        """The color this player plays (WHITE or BLACK)."""
        return self._color
    
    @color.setter
    def color(self, value: chess.Color) -> None:
        """Set the color this player plays."""
        self._color = value
    
    @property
    def state(self) -> PlayerState:
        """Current state of the player."""
        return self._state
    
    @property
    def is_ready(self) -> bool:
        """Whether the player is ready to play."""
        return self._state == PlayerState.READY
    
    @property
    def is_thinking(self) -> bool:
        """Whether the player is currently computing/waiting for a move."""
        return self._state == PlayerState.THINKING
    
    @property
    def error_message(self) -> Optional[str]:
        """Error message if state is ERROR, None otherwise."""
        return self._error_message
    
    @property
    @abstractmethod
    def player_type(self) -> PlayerType:
        """The type of this player (HUMAN, ENGINE, etc.)."""
        pass
    
    def can_resign(self) -> bool:
        """Whether this player can resign via the board interface.
        
        Returns True if the player at the physical board can choose to
        resign on behalf of this player. Default is True.
        
        Override in subclasses for players that cannot be resigned
        (e.g., remote players in some online modes).
        """
        return True
    
    @property
    def pending_move(self) -> Optional[chess.Move]:
        """The pending move this player wants executed on the board.
        
        For engine/Lichess players, this is the computed/received move
        waiting for the human to execute on the physical board.
        
        For human players, this is always None (humans decide as they move).
        
        Returns:
            The pending move, or None if no move is pending.
        """
        return None
    
    # =========================================================================
    # Callback Management
    # =========================================================================
    
    def set_move_callback(self, callback: Callable[[chess.Move], bool]) -> None:
        """Set callback for when player submits a move.

        All players use this callback to submit moves to the game.
        The callback is invoked when piece events form a move.

        Args:
            callback: Function(move) -> bool. Returns True if move was
                     accepted (valid), False if rejected (invalid).
                     Players like Lichess need the return value to know
                     if the move should be sent to the server.
        """
        self._move_callback = callback
    
    def set_pending_move_callback(self, callback: Callable[[chess.Move], None]) -> None:
        """Set callback for when player has a pending move to display.
        
        Called when the player has determined what move should be made,
        before piece events confirm execution. Used for LED display.
        
        - Human: Not used (no pending move - they're deciding)
        - Engine: Called when engine finishes computing
        - Lichess: Called when server sends a move
        
        Args:
            callback: Function to call with the pending move (for LEDs).
        """
        self._pending_move_callback = callback
    
    def set_status_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback for status updates.
        
        Used to display status messages during initialization,
        connection, engine thinking, etc.
        
        Args:
            callback: Function to call with status text.
        """
        self._status_callback = callback
    
    def set_error_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback for error conditions that require correction mode.
        
        Called when the player detects an error condition that the game
        should handle, such as:
        - Place event without corresponding lift (extra piece on board)
        - Non-matching piece events for engine/Lichess moves
        
        Args:
            callback: Function to call with error type string.
                     Error types: "place_without_lift", "move_mismatch"
        """
        self._error_callback = callback
    
    def set_ready_callback(self, callback: Callable[[], None]) -> None:
        """Set callback for when player becomes ready.
        
        Called when the player transitions to READY state. Used by
        PlayerManager to know when all players are ready and to
        trigger the first move request for non-human players.
        
        Args:
            callback: Function to call when player becomes ready.
        """
        self._ready_callback = callback
    
    def _report_error(self, error_type: str) -> None:
        """Report an error condition via the callback if set.
        
        Args:
            error_type: Type of error (e.g., "place_without_lift").
        """
        if self._error_callback:
            self._error_callback(error_type)
    
    # =========================================================================
    # State Management (Protected)
    # =========================================================================
    
    def _set_state(self, state: PlayerState, error: Optional[str] = None) -> None:
        """Update the player state.
        
        If transitioning to READY and a move request was queued,
        processes it now.
        
        Args:
            state: New state.
            error: Error message if state is ERROR.
        """
        old_state = self._state
        self._state = state
        if state == PlayerState.ERROR:
            self._error_message = error
        else:
            self._error_message = None
        
        # When becoming ready, fire ready callback and process queued move
        if state == PlayerState.READY and old_state == PlayerState.INITIALIZING:
            # Fire ready callback first
            if self._ready_callback:
                self._ready_callback()
            
            # Process queued move request
            if self._pending_board is not None:
                from universalchess.board.logging import log
                log.info(f"[Player] {self.name} now ready, processing queued move request")
                pending_board = self._pending_board
                self._pending_board = None
                self._do_request_move(pending_board)
    
    def _report_status(self, message: str) -> None:
        """Report a status message via the callback if set.
        
        Args:
            message: Status message to report.
        """
        if self._status_callback:
            self._status_callback(message)
    
    # =========================================================================
    # Abstract Methods - Must be implemented by subclasses
    # =========================================================================
    
    @abstractmethod
    def start(self) -> bool:
        """Initialize and start the player.
        
        Performs any async initialization: loading engine, connecting
        to online service, etc.
        
        Should set state to READY on success, ERROR on failure.
        
        Returns:
            True if started successfully, False on error.
        """
        pass
    
    @abstractmethod
    def stop(self) -> None:
        """Stop the player and release resources.
        
        Clean shutdown: close connections, stop threads, release
        engine processes, etc.
        
        Should set state to STOPPED.
        """
        pass
    
    def request_move(self, board: chess.Board) -> None:
        """Request the player to provide a move.
        
        Called when it's this player's turn. The player should prepare
        to receive piece events and eventually submit a move via callback.
        
        If the player is still initializing, queues the request and
        processes it when the player becomes ready.
        
        Subclasses should override _do_request_move() for their specific logic.
        
        Args:
            board: Current chess position.
        """
        # Queue the request if still initializing
        if self._state == PlayerState.INITIALIZING:
            from universalchess.board.logging import log
            log.info(f"[Player] {self.name} still initializing, queueing move request")
            self._pending_board = board.copy()
            return
        
        # A failed startup is not necessarily permanent: an engine load can fail
        # because the machine was momentarily too busy, not because the engine is
        # unusable. Queue the request before attempting recovery, so a subclass
        # that can come back serves this move on its transition to ready instead
        # of dropping it -- recovering the state alone would leave the board
        # waiting on a player that is idle and healthy.
        if self._state == PlayerState.ERROR:
            self._pending_board = board.copy()
            if self._recover_from_error():
                return
            self._pending_board = None
        
        if self._state != PlayerState.READY:
            from universalchess.board.logging import log
            log.warning(f"[Player] {self.name} request_move called but state is {self._state}")
            return
        
        # Clear queued request since we're processing now
        self._pending_board = None
        
        # Call subclass implementation
        self._do_request_move(board)
    
    def _recover_from_error(self) -> bool:
        """Attempt to leave ERROR and become usable again.

        Called when a move is requested of a player whose startup failed.
        Returns True once recovery is under way, meaning the queued request will
        be served on the transition to ready; False when the player cannot
        recover, leaving the request to be refused as before.

        The base player owns no external resource whose failure could be
        transient, so it has nothing to retry.
        """
        return False
    
    def _do_request_move(self, board: chess.Board) -> None:
        """Subclass-specific move request handling.
        
        Called by request_move() after state validation passes.
        Override this in subclasses, not request_move().
        
        Default implementation resets lifted squares for piece event tracking.
        
        Args:
            board: Current chess position.
        """
        self._lifted_squares = []
    
    def on_piece_event(self, event_type: str, square: int, board: chess.Board) -> None:
        """Handle a piece event from the physical board.

        Called when a piece is lifted or placed on the board.
        Forms a move from lift/place sequence and calls _on_move_formed().
        
        For captures, two pieces are lifted (order doesn't matter):
        - The moving piece (from source square)
        - The captured piece (from target square)
        When the moving piece is placed on the target, we determine which
        lifted square was the source (the one that's not the target).
        
        Error cases (call error_callback):
        - Place without any lifts (extra piece on board)
        - All lifted pieces placed back (no move made)
        
        Success cases (call _on_move_formed):
        - Lift(s) followed by place on a different square

        Args:
            event_type: "lift" or "place"
            square: The square index (0-63) where the event occurred
            board: Current chess position (before the move)
        """
        if event_type == "lift":
            # Track all lifted squares (up to 2 for captures)
            if square not in self._lifted_squares:
                self._lifted_squares.append(square)
        
        elif event_type == "place":
            if not self._lifted_squares:
                # Place without any lifts - likely the lift event was missed.
                # Submit a destination-only move (from_square == to_square signals this).
                # GameManager will find the source by comparing board state to game state.
                from universalchess.board.logging import log
                log.warning(f"[Player.on_piece_event] Place without lift on {chess.square_name(square)} - submitting destination-only move")
                self._on_move_formed(chess.Move(square, square))
                return
            
            if len(self._lifted_squares) == 1:
                # Single piece lifted
                from_sq = self._lifted_squares[0]
                if square == from_sq:
                    # Piece placed back - no move
                    self._lifted_squares = []
                    self._report_error("piece_returned")
                    return
                # Normal move
                self._lifted_squares = []
                self._on_move_formed(chess.Move(from_sq, square))
            else:
                # Multiple pieces lifted (capture or with nudges)
                if square in self._lifted_squares:
                    if len(self._lifted_squares) > 2:
                        # More than 2 pieces lifted - this is likely a nudge being returned.
                        # Remove the placed-back square and continue waiting for the actual move.
                        # E.g., for capture b4xc3 with accidental b3 nudge:
                        #   LIFT(b4), LIFT(c3), LIFT(b3), PLACE(b3) -> just remove b3, wait for PLACE(c3)
                        from universalchess.board.logging import log
                        log.info(f"[Player.on_piece_event] Piece returned to {chess.square_name(square)} with {len(self._lifted_squares)} lifts - treating as nudge")
                        self._lifted_squares.remove(square)
                        return
                    # Exactly 2 lifted: placing on one of the lifted squares = capture move
                    # The OTHER lifted square is the source
                    from_sq = [sq for sq in self._lifted_squares if sq != square][0]
                else:
                    # Placing on a third square - unusual, take first lifted as source
                    from_sq = self._lifted_squares[0]
                self._lifted_squares = []
                self._on_move_formed(chess.Move(from_sq, square))
    
    def _on_move_formed(self, move: chess.Move) -> None:
        """Called when a move is formed from piece events.
        
        Default implementation submits via move_callback.
        Subclasses can override to add validation (e.g., engine/Lichess
        checking if the move matches their expected move).
        
        Args:
            move: The formed move.
        """
        if self._move_callback:
            self._move_callback(move)
    
    @abstractmethod
    def on_move_made(self, move: chess.Move, board: chess.Board) -> None:
        """Notification that a move was made on the board.
        
        Called after any move is made (by either player).
        Players can use this to track game state.
        
        Args:
            move: The move that was made.
            board: Board state after the move.
        """
        pass
    
    @abstractmethod
    def on_new_game(self) -> None:
        """Notification that a new game is starting.
        
        Called when the board is reset to starting position.
        Players should reset internal state.
        """
        pass
    
    # =========================================================================
    # Optional Methods - May be overridden by subclasses
    # =========================================================================
    
    def on_takeback(self, board: chess.Board) -> None:
        """Notification that a takeback occurred.
        
        Called when a move is taken back. Not all players support
        takeback. Default implementation does nothing.
        
        Args:
            board: Board state after the takeback.
        """
        pass
    
    def on_resign(self, color: chess.Color) -> None:
        """Notification that a player resigned.
        
        Args:
            color: The color that resigned.
        """
        pass
    
    def on_draw_offer(self) -> None:
        """Notification that a draw was offered.
        
        For online players, this sends the draw offer. Default
        implementation does nothing.
        """
        pass
    
    def consider_draw_offer(self, board: chess.Board) -> bool:
        """Decide whether this player accepts a draw offer from the opponent.
        
        Called synchronously when the human at the physical board offers a draw,
        so the opponent can accept or refuse based on the position. Returning
        True records the draw; returning False continues the game.
        
        The default accepts, which matches a human opponent agreeing to a draw
        at the board (2-player mode). Engine players override this to evaluate
        the position and decline when they are clearly winning.
        
        Args:
            board: Current position at the time of the offer.
        
        Returns:
            True to accept the draw, False to decline and keep playing.
        """
        return True
    
    def on_correction_mode_exit(self) -> None:
        """Notification that correction mode has exited.
        
        Called when the physical board has been corrected to match the
        logical board state. Players can use this to restore UI state
        (status messages, LED hints, etc.) that was interrupted by
        correction mode.
        
        Default implementation does nothing. Override in subclasses
        that need to restore state after correction.
        """
        pass
    
    def supports_takeback(self) -> bool:
        """Whether this player supports takeback.
        
        Returns:
            True if takeback is supported, False otherwise.
        """
        return True
    
    def get_info(self) -> dict:
        """Get information about this player for display.
        
        Returns:
            Dictionary with player info. Keys may include:
            - name: Player name
            - color: WHITE or BLACK
            - type: Player type (human, engine, etc.)
            - state: Current player state
        """
        return {
            'name': self.name,
            'color': 'white' if self._color == chess.WHITE else 'black' if self._color == chess.BLACK else None,
            'type': self.player_type.name,
            'state': self._state.name,
        }
