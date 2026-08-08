"""
Chess game state.

Holds the authoritative game state: board position, result, and termination.
Widgets observe this state to display the current position and game status.

The chess.Board is owned here - GameManager and other components mutate it
through this state object's methods, which trigger observer notifications.

This module has minimal dependencies (python-chess, typing, and the pure
state.alerts policy) to keep imports fast for widgets.
"""

import chess
from typing import Optional, Callable, List

from universalchess.state import alerts
from universalchess.utils.observers import notify_observers


class ChessGameState:
    """Observable chess game state.
    
    Holds:
    - The chess.Board (position, legal moves, turn)
    - Game result and termination reason
    
    Observers are notified on:
    - Position changes (moves, takeback, new position)
    - Game over (checkmate, stalemate, resignation, flag, draw)
    - Check (when the side-to-move's king is in check after a move)
    - Queen threat (when the side-to-move's own queen is under attack, unless the
      player disabled that warning -- see set_alert_preferences)
    - Alert clear (when no check or threat exists, or the only applicable warning
      is disabled)
    
    Thread safety: This class is NOT thread-safe. Callers must ensure
    mutations happen from a single thread or use external synchronization.
    """
    
    def __init__(self):
        """Initialize game state with the standard starting position."""
        self._start_fen: str = chess.STARTING_FEN
        self._chess960: bool = False
        self._board = chess.Board()
        self._result: Optional[str] = None  # '1-0', '0-1', '1/2-1/2'
        self._termination: Optional[str] = None  # 'checkmate', 'stalemate', 'resignation', etc.
        # Which warnings to raise. Defaults to all enabled so a state built before
        # settings are loaded behaves as the board always has; main.py applies the
        # persisted preferences on settings load and on hot reload.
        self._alert_preferences = alerts.AlertPreferences()
        
        # Observer callbacks
        self._on_position_change: List[Callable[[], None]] = []
        self._on_game_over: List[Callable[[str, str], None]] = []  # (result, termination)
        self._on_check: List[Callable[[bool, int, int], None]] = []  # (is_black_in_check, attacker_sq, king_sq)
        self._on_queen_threat: List[Callable[[bool, int, int], None]] = []  # (is_black_threatened, attacker_sq, queen_sq)
        self._on_alert_clear: List[Callable[[], None]] = []
    
    # -------------------------------------------------------------------------
    # Properties (read-only access to state)
    # -------------------------------------------------------------------------
    
    @property
    def board(self) -> chess.Board:
        """The chess.Board instance. Use for read-only queries.
        
        For mutations, use the state methods (push_move, set_position, etc.)
        to ensure observers are notified.
        """
        return self._board
    
    @property
    def fen(self) -> str:
        """Current position in FEN notation."""
        return self._board.fen()

    @property
    def chess960(self) -> bool:
        """Whether this game is Chess960 (Fischer Random).

        When True, the board applies Chess960 castling rules and python-chess
        automatically sends ``UCI_Chess960`` to engines that receive a copy of
        this board (see ``board_copy``).
        """
        return self._chess960

    @property
    def start_fen(self) -> str:
        """The starting FEN this game resets to.

        For a standard game this is the normal start; for a Chess960 game it is
        the randomly generated 960 start. ``reset()`` returns here so re-setting
        up the physical board never changes the generated position.
        """
        return self._start_fen

    def history_positions(self) -> List[dict]:
        """Return authoritative per-ply positions for the whole game so far.

        Each entry is ``{"fen", "san", "uci"}``: the first is the starting
        position (``san``/``uci`` are None), and each subsequent entry is the
        position after that ply with its SAN and UCI. The list is rebuilt from
        the configured start FEN on a board carrying this game's ``chess960``
        flag, so every FEN and SAN is variant-correct.

        This is the source the web live board navigates history by, instead of
        replaying the PGN in the browser: chess.js mis-computes Chess960 castling
        (it moves the king to the wrong square), so browser-derived history
        positions are wrong for a 960 game. Deriving them here with python-chess
        keeps navigation correct for both variants.
        """
        root = chess.Board(self._start_fen, chess960=self._chess960)
        positions: List[dict] = [{"fen": root.fen(), "san": None, "uci": None}]
        node = root
        for move in self._board.move_stack:
            san = node.san(move)
            node.push(move)
            positions.append({"fen": node.fen(), "san": san, "uci": move.uci()})
        return positions

    def board_copy(self) -> chess.Board:
        """Return an independent copy of the board preserving the Chess960 flag.

        Engine/analysis callers must copy through this (not ``chess.Board(fen)``)
        so the ``chess960`` flag is carried over; python-chess only emits
        ``UCI_Chess960`` and applies 960 castling when the board it is given has
        ``chess960`` set.
        """
        return self._board.copy()
    
    @property
    def turn(self) -> chess.Color:
        """Which player's turn (chess.WHITE or chess.BLACK)."""
        return self._board.turn
    
    @property
    def turn_name(self) -> str:
        """Turn as string ('white' or 'black')."""
        return 'white' if self._board.turn == chess.WHITE else 'black'
    
    @property
    def legal_moves(self):
        """Generator of legal moves at current position."""
        return self._board.legal_moves
    
    @property
    def move_stack(self) -> List[chess.Move]:
        """List of moves made in this game."""
        return list(self._board.move_stack)
    
    @property
    def is_game_in_progress(self) -> bool:
        """Whether a game is in progress (at least one move has been made)."""
        return len(self._board.move_stack) > 0
    
    @property
    def is_check(self) -> bool:
        """Whether the current player is in check."""
        return self._board.is_check()
    
    @property
    def is_game_over(self) -> bool:
        """Whether the game has ended (by board state or external result)."""
        return self._board.is_game_over() or self._result is not None
    
    @property
    def result(self) -> Optional[str]:
        """Game result ('1-0', '0-1', '1/2-1/2') or None if ongoing."""
        if self._result is not None:
            return self._result
        outcome = self._board.outcome()
        if outcome is not None:
            return outcome.result()
        return None
    
    @property
    def termination(self) -> Optional[str]:
        """How the game ended ('checkmate', 'stalemate', 'resignation', etc.)."""
        if self._termination is not None:
            return self._termination
        outcome = self._board.outcome()
        if outcome is not None:
            return str(outcome.termination.name).lower()
        return None
    
    # -------------------------------------------------------------------------
    # Board queries (pure computations on current state)
    # -------------------------------------------------------------------------
    
    def get_legal_destinations(self, source_square: int) -> List[int]:
        """Get legal destination squares for a piece at the given square.
        
        Returns all squares where the piece can legally move, including
        the source square itself (allowing piece to be placed back).
        
        Args:
            source_square: The square index (0-63) of the piece.
            
        Returns:
            List of square indices including source and all legal destinations.
        """
        destinations = [source_square]  # Include source (put piece back)
        for move in self._board.legal_moves:
            if move.from_square == source_square:
                destinations.append(move.to_square)
        return destinations
    
    def to_piece_presence_state(self) -> bytearray:
        """Convert current position to piece presence state.
        
        Returns a 64-byte array where each byte is 1 if a piece is present
        on that square, 0 otherwise. Used for comparing against physical board.
        
        Returns:
            bytearray: 64 bytes representing piece presence (1) or absence (0).
        """
        state = bytearray(64)
        for square in range(64):
            piece = self._board.piece_at(square)
            state[square] = 1 if piece is not None else 0
        return state
    
    def get_check_info(self) -> Optional[tuple]:
        """Get information about check state.

        Raw position query: reports the check regardless of alert preferences
        (check is never suppressible). Use current_alert() for the warning the
        display should actually show.

        Returns:
            Tuple of (is_black_in_check, attacker_square, king_square) if in check,
            None if not in check.
        """
        check = alerts.find_check(self._board)
        if check is None:
            return None
        return (check.is_black_threatened, check.attacker_square, check.target_square)

    def get_queen_threat_info(self) -> Optional[tuple]:
        """Get information about queen threat state.

        Raw position query: reports an attacked own queen even when the player has
        switched the YOUR QUEEN warning off. Use current_alert() for the warning
        the display should actually show; see state/alerts.find_queen_threat for
        the rule and why it flags the side-to-move's own queen.

        Returns:
            Tuple of (is_black_queen_threatened, attacker_square, queen_square)
            if the side-to-move's queen is attacked, None otherwise.
        """
        threat = alerts.find_queen_threat(self._board)
        if threat is None:
            return None
        return (threat.is_black_threatened, threat.attacker_square, threat.target_square)

    # -------------------------------------------------------------------------
    # Alert policy (what the surfaces should warn about)
    # -------------------------------------------------------------------------

    @property
    def alert_preferences(self) -> alerts.AlertPreferences:
        """Which in-play warnings are enabled for this game.

        Read by surfaces that derive a warning from the position they are drawing
        rather than from the observers (the three-color red highlight), so they
        honor the same preferences as the alert text.
        """
        return self._alert_preferences

    def set_alert_preferences(self, preferences: alerts.AlertPreferences) -> None:
        """Adopt new alert preferences (from the persisted game settings).

        Deliberately does NOT re-emit the alert: a settings change arrives on the
        settings-subscriber thread, while the observing widgets may only be touched
        from the main thread. The main loop calls refresh_alerts() when it rebuilds
        the display, which re-derives the alert under the new preferences and takes
        down a warning that has just been switched off.

        Args:
            preferences: The preferences to apply from now on.
        """
        self._alert_preferences = preferences

    def current_alert(self) -> Optional[alerts.Alert]:
        """The warning this position raises under the active preferences.

        The single source every surface reads, so the e-paper text, the LED flash,
        the red highlight, and the web banner cannot disagree about one position.

        Returns:
            The alert to show, or None for a quiet position (or one whose only
            applicable warning the player has disabled).
        """
        return alerts.resolve_alert(self._board, self._alert_preferences)

    
    # -------------------------------------------------------------------------
    # Board state comparison utilities
    # -------------------------------------------------------------------------
    
    # Starting position as piece presence state (1 = piece, 0 = empty)
    # Ranks 1-2 and 7-8 have pieces, ranks 3-6 are empty
    STARTING_POSITION_STATE = bytearray([
        1, 1, 1, 1, 1, 1, 1, 1,  # Rank 1 (white pieces)
        1, 1, 1, 1, 1, 1, 1, 1,  # Rank 2 (white pawns)
        0, 0, 0, 0, 0, 0, 0, 0,  # Rank 3
        0, 0, 0, 0, 0, 0, 0, 0,  # Rank 4
        0, 0, 0, 0, 0, 0, 0, 0,  # Rank 5
        0, 0, 0, 0, 0, 0, 0, 0,  # Rank 6
        1, 1, 1, 1, 1, 1, 1, 1,  # Rank 7 (black pawns)
        1, 1, 1, 1, 1, 1, 1, 1,  # Rank 8 (black pieces)
    ])
    
    @staticmethod
    def is_starting_position(board_state) -> bool:
        """Check if a board state represents the starting position.
        
        Args:
            board_state: 64-byte piece presence array.
            
        Returns:
            True if the board is in starting position.
        """
        if board_state is None or len(board_state) != 64:
            return False
        return bytearray(board_state) == ChessGameState.STARTING_POSITION_STATE
    
    @staticmethod
    def states_match(current_state, expected_state) -> bool:
        """Compare two board states for equality.
        
        Args:
            current_state: First 64-byte piece presence array.
            expected_state: Second 64-byte piece presence array.
            
        Returns:
            True if both states represent the same piece positions.
        """
        if current_state is None or expected_state is None:
            return False
        if len(current_state) != 64 or len(expected_state) != 64:
            return False
        return bytearray(current_state) == bytearray(expected_state)
    
    # -------------------------------------------------------------------------
    # Observer management
    # -------------------------------------------------------------------------
    
    def on_position_change(self, callback: Callable[[], None]) -> None:
        """Register callback for position changes.
        
        Called after any move, takeback, or position reset.
        
        Args:
            callback: Function with no arguments, called on position change.
        """
        if callback not in self._on_position_change:
            self._on_position_change.append(callback)
    
    def on_game_over(self, callback: Callable[[str, str], None]) -> None:
        """Register callback for game over events.
        
        Args:
            callback: Function(result, termination) called when game ends.
        """
        if callback not in self._on_game_over:
            self._on_game_over.append(callback)
    
    def on_check(self, callback: Callable[[bool, int, int], None]) -> None:
        """Register callback for check events.
        
        Called when a king is in check after a move.
        
        Args:
            callback: Function(is_black_in_check, attacker_square, king_square)
        """
        if callback not in self._on_check:
            self._on_check.append(callback)
    
    def on_queen_threat(self, callback: Callable[[bool, int, int], None]) -> None:
        """Register callback for queen threat events.
        
        Called when a queen is under attack after a move (and no check).
        
        Args:
            callback: Function(is_black_queen_threatened, attacker_square, queen_square)
        """
        if callback not in self._on_queen_threat:
            self._on_queen_threat.append(callback)
    
    def on_alert_clear(self, callback: Callable[[], None]) -> None:
        """Register callback for alert clear events.
        
        Called when there is no check or queen threat after a move.
        
        Args:
            callback: Function with no arguments.
        """
        if callback not in self._on_alert_clear:
            self._on_alert_clear.append(callback)
    
    def remove_observer(self, callback: Callable) -> None:
        """Remove a previously registered callback.
        
        Args:
            callback: The callback to remove (from any observer list).
        """
        if callback in self._on_position_change:
            self._on_position_change.remove(callback)
        if callback in self._on_game_over:
            self._on_game_over.remove(callback)
        if callback in self._on_check:
            self._on_check.remove(callback)
        if callback in self._on_queen_threat:
            self._on_queen_threat.remove(callback)
        if callback in self._on_alert_clear:
            self._on_alert_clear.remove(callback)
    
    def notify_position_change(self) -> None:
        """Notify all position change observers.
        
        Called automatically by mutation methods (push_move, pop_move, etc.).
        Can also be called manually after direct board modifications.
        """
        notify_observers(self._on_position_change, context="on_position_change")
    
    def notify_game_over(self, result: str, termination: str) -> None:
        """Notify all game over observers.
        
        Args:
            result: Game result ('1-0', '0-1', '1/2-1/2')
            termination: How game ended
        """
        notify_observers(self._on_game_over, result, termination, context="on_game_over")
    
    def refresh_alerts(self) -> None:
        """Re-emit the current check / queen-threat status to all observers.

        The CHECK and YOUR QUEEN alerts are normally raised only as a side effect
        of push_move()/reset(). Their visible truth therefore lives transiently in
        the observing widgets. Any flow that rebuilds those widgets without a move
        (e.g. cancelling the king-lift resign or kings-in-center menu, which calls
        DisplayManager._init_widgets()) produces fresh, hidden alert widgets that
        are unaware of an in-progress check - silently dropping the alert ("remove
        and replace a piece, the check alert goes away" bug).

        Calling this after a widget rebuild re-derives the alert from this
        authoritative state, so a still-active check/threat is re-shown and a quiet
        position clears any stale alert. Idempotent and safe to call at any time.
        """
        self._notify_check_and_threats()

    def _notify_check_and_threats(self) -> None:
        """Notify observers of the warning this position raises, or of none.

        The alert itself (priority, and whether a warning is enabled at all) is
        resolved by current_alert(); this only routes it to the matching observer
        list. A disabled warning therefore arrives here as "no alert" and clears,
        so a warning switched off mid-game does not stay on the display.
        """
        alert = self.current_alert()
        if alert is None:
            notify_observers(self._on_alert_clear, context="on_alert_clear")
            return

        # Keyed lookup rather than an if/else fallthrough: a new alert kind added
        # to state/alerts without an observer list here raises a KeyError instead
        # of being silently delivered as the wrong kind of warning.
        observers, context = {
            alerts.CHECK: (self._on_check, "on_check"),
            alerts.QUEEN_THREAT: (self._on_queen_threat, "on_queen_threat"),
        }[alert.kind]
        notify_observers(
            observers,
            alert.is_black_threatened, alert.attacker_square, alert.target_square,
            context=context,
        )
    
    # -------------------------------------------------------------------------
    # State mutations (trigger observer notifications)
    # -------------------------------------------------------------------------
    
    def push_move(self, move: chess.Move) -> None:
        """Push a move onto the board.
        
        After the move, checks for and notifies:
        - Position change (always)
        - Check or queen threat (if applicable)
        - Game over (if applicable)
        
        Args:
            move: The chess.Move to execute.
            
        Raises:
            ValueError: If move is illegal.
        """
        if move not in self._board.legal_moves:
            raise ValueError(f"Illegal move: {move.uci()}")
        
        self._board.push(move)
        self.notify_position_change()
        
        # Detect and notify check/threats
        self._notify_check_and_threats()
        
        # Check for game end by board state
        outcome = self._board.outcome()
        if outcome is not None:
            self._result = outcome.result()
            self._termination = str(outcome.termination.name).lower()
            self.notify_game_over(self._result, self._termination)
    
    def push_uci(self, uci: str) -> chess.Move:
        """Push a move by UCI string.
        
        Args:
            uci: Move in UCI format (e.g., 'e2e4', 'e7e8q').
            
        Returns:
            The parsed chess.Move.
            
        Raises:
            ValueError: If UCI is invalid or move is illegal.
        """
        move = chess.Move.from_uci(uci)
        self.push_move(move)
        return move
    
    def pop_move(self) -> Optional[chess.Move]:
        """Pop the last move (takeback).
        
        Returns:
            The popped move, or None if no moves to pop.
        """
        if not self._board.move_stack:
            return None
        
        # Clear any external result on takeback
        self._result = None
        self._termination = None
        
        move = self._board.pop()
        self.notify_position_change()
        # Re-derive the check/queen alert for the reverted position, mirroring
        # push_move. Without this a takeback of a checking move left the CHECK
        # alert on screen over a position with no check (and a takeback INTO a
        # still-in-check position failed to re-raise it).
        self._notify_check_and_threats()
        return move
    
    def set_position(self, fen: str) -> None:
        """Set the board to a specific position.

        Preserves the current ``chess960`` flag: ``set_fen`` only rewrites the
        position, not the variant, so a Chess960 game stays Chess960 when a
        position is applied (e.g. loading the generated 960 start after game
        mode init).

        Args:
            fen: FEN string of the position.
            
        Raises:
            ValueError: If FEN is invalid.
        """
        self._board.set_fen(fen)
        self._result = None
        self._termination = None
        self.notify_position_change()
        # Re-derive the check/queen alert for the adopted position, mirroring
        # configure_start. Without this, loading a position never updated the
        # alert: an in-check position showed none, and a quiet one left a stale
        # CHECK on screen.
        self._notify_check_and_threats()

    def configure_start(self, fen: str, chess960: bool = False) -> None:
        """Set the game's starting position and variant.

        Establishes what ``reset()`` returns to. The board object is mutated in
        place (the ``chess960`` flag is set before ``set_fen`` so castling rights
        parse correctly) rather than reassigned, because GameManager captures the
        board reference once and relies on its identity. Setting the flag is what
        makes python-chess apply 960 castling rules and emit ``UCI_Chess960`` to
        engines that receive ``board_copy()``.

        Args:
            fen: Starting FEN for the game.
            chess960: True for a Chess960 (Fischer Random) game.

        Raises:
            ValueError: If FEN is invalid.
        """
        self._start_fen = fen
        self._chess960 = chess960
        self._board.chess960 = chess960
        self._board.set_fen(fen)
        self._result = None
        self._termination = None
        self.notify_position_change()
        self._notify_check_and_threats()

    def reset(self) -> None:
        """Reset to this game's starting position.

        Variant-aware: restores the configured start FEN and ``chess960`` flag
        rather than the standard start, so a Chess960 game keeps its generated
        position across a board-reset (the physical home-rank gesture must not
        regenerate a new random position). Clears any check/threat alerts since a
        starting position has no threats.
        """
        self._board.chess960 = self._chess960
        self._board.set_fen(self._start_fen)
        self._result = None
        self._termination = None
        self.notify_position_change()
        # Clear alerts - starting position has no check or threats
        self._notify_check_and_threats()

    def reset_to_standard(self) -> None:
        """Reset to the standard starting position and clear the variant.

        Used when a fresh game begins so a prior Chess960 game's start FEN does
        not leak into a new standard game (``reset()`` is variant-aware and would
        otherwise restore the previous game's 960 start).
        """
        self._start_fen = chess.STARTING_FEN
        self._chess960 = False
        self._board.chess960 = False
        self._board.reset()
        self._result = None
        self._termination = None
        self.notify_position_change()
        self._notify_check_and_threats()
    
    def set_result(self, result: str, termination: str) -> None:
        """Set game result from external event (resignation, flag, draw agreement).
        
        Use this for game endings that aren't determined by board state
        (e.g., resignation, time forfeit, draw by agreement).
        
        Args:
            result: Game result ('1-0', '0-1', '1/2-1/2')
            termination: How game ended ('resignation', 'time_forfeit', 'draw_agreement')
        """
        self._result = result
        self._termination = termination
        self.notify_game_over(result, termination)


# -----------------------------------------------------------------------------
# Singleton instance
# -----------------------------------------------------------------------------

_instance: Optional[ChessGameState] = None


def get_chess_game() -> ChessGameState:
    """Get the singleton ChessGameState instance.
    
    Returns:
        The global ChessGameState instance.
    """
    global _instance
    if _instance is None:
        _instance = ChessGameState()
    return _instance


def reset_chess_game() -> ChessGameState:
    """Reset the singleton to a fresh instance.
    
    Primarily for testing. Creates a new instance and returns it.
    
    Returns:
        The new ChessGameState instance.
    """
    global _instance
    _instance = ChessGameState()
    return _instance
