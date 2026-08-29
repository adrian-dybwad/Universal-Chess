# Player Manager
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Manages both players in a chess game. Each game has a white player
# and a black player, and this manager coordinates between them.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

from typing import Optional, Callable

import chess

from universalchess.board.logging import log
from universalchess.state.players import get_players_state
from .base import Player, PlayerType


class PlayerManager:
    """Manages both players in a chess game.
    
    Each game has two players (White and Black). The PlayerManager:
    - Holds references to both players
    - Routes piece events to the current player
    - Routes move requests to the current player
    - Notifies both players of moves made
    - Manages player lifecycle (start/stop)
    
    All players receive piece events and submit moves via callback.
    The difference is how they process events:
    - Human: Forms move from lift/place, submits any move
    - Engine/Lichess: Has pending move, only submits if events match
    
    Example:
        # Setup
        white_player = HumanPlayer()
        black_player = create_engine_player(chess.BLACK, "stockfish")
        
        manager = PlayerManager(white_player, black_player)
        manager.set_move_callback(on_move)
        manager.set_pending_move_callback(on_pending_move)  # For LED display
        manager.start()
        
        # On each turn
        manager.request_move(board)
        
        # Route piece events
        manager.on_piece_event("lift", square, board)
        manager.on_piece_event("place", square, board)
        
        # When a move is made
        manager.on_move_made(move, board)
    """
    
    def __init__(
        self,
        white_player: Player,
        black_player: Player,
        move_callback: Optional[Callable[[chess.Move], bool]] = None,
        pending_move_callback: Optional[Callable[[chess.Move], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        error_callback: Optional[Callable[[str], None]] = None,
        ready_callback: Optional[Callable[[], None]] = None,
    ):
        """Initialize the player manager.
        
        Args:
            white_player: The player for White.
            black_player: The player for Black.
            move_callback: Called when any player submits a move.
            pending_move_callback: Called when a player has a pending move (for LEDs).
            status_callback: Called with status messages.
            error_callback: Called when a player reports an error (e.g., place without lift).
            ready_callback: Called when all players are ready.
        """
        self._white_player = white_player
        self._black_player = black_player
        self._move_callback = move_callback
        self._pending_move_callback = pending_move_callback
        self._status_callback = status_callback
        self._error_callback = error_callback
        self._ready_callback = ready_callback
        self._ready_fired = False  # Only fire once
        
        # Set colors
        self._white_player.color = chess.WHITE
        self._black_player.color = chess.BLACK
        
        # Wire callbacks
        self._wire_callbacks()
        
        # Update observable PlayersState
        self._update_players_state()
        
        log.info(f"[PlayerManager] Created with White={white_player.name} ({white_player.player_type.name}), "
                 f"Black={black_player.name} ({black_player.player_type.name})")
    
    def _wire_callbacks(self) -> None:
        """Wire callbacks to players."""
        # Move callback - all players submit moves via this
        if self._move_callback:
            self._white_player.set_move_callback(self._move_callback)
            self._black_player.set_move_callback(self._move_callback)
        
        # Pending move callback - for LED display
        if self._pending_move_callback:
            self._white_player.set_pending_move_callback(self._pending_move_callback)
            self._black_player.set_pending_move_callback(self._pending_move_callback)
        
        # Status callback
        if self._status_callback:
            self._white_player.set_status_callback(self._status_callback)
            self._black_player.set_status_callback(self._status_callback)
        
        # Error callback
        if self._error_callback:
            self._white_player.set_error_callback(self._error_callback)
            self._black_player.set_error_callback(self._error_callback)
        
        # Ready callback - fire manager callback when both players ready
        self._white_player.set_ready_callback(self._on_player_ready)
        self._black_player.set_ready_callback(self._on_player_ready)
    
    def _on_player_ready(self) -> None:
        """Handle a player becoming ready.
        
        Fires the manager's ready callback when both players are ready.
        Only fires once.
        """
        if self._ready_fired:
            return
        
        if self.is_ready:
            self._ready_fired = True
            log.info("[PlayerManager] All players ready")
            if self._ready_callback:
                self._ready_callback()
    
    def _update_players_state(self) -> None:
        """Update the observable PlayersState with current player info.
        
        Called when players are initialized or swapped.
        Sets player names and hand-brain mode for each player.
        """
        players_state = get_players_state()
        players_state.set_player_names(
            white_name=self._white_player.name,
            black_name=self._black_player.name
        )
        
        # Set hand-brain mode from player configs (only HumanPlayerConfig has this)
        white_hand_brain = getattr(self._white_player._config, 'hand_brain', False)
        black_hand_brain = getattr(self._black_player._config, 'hand_brain', False)
        players_state.set_hand_brain(white_hand_brain, black_hand_brain)
    
    # =========================================================================
    # Callback Setters
    # =========================================================================
    
    def set_move_callback(self, callback: Callable[[chess.Move], bool]) -> None:
        """Set callback for when a player submits a move.
        
        All players submit moves via this callback after piece events.
        
        Args:
            callback: Function(move) -> bool. Returns True if accepted, False if rejected.
        """
        self._move_callback = callback
        self._white_player.set_move_callback(callback)
        self._black_player.set_move_callback(callback)
    
    def set_pending_move_callback(self, callback: Callable[[chess.Move], None]) -> None:
        """Set callback for when a player has a pending move to display.
        
        Used for LED display. Called when engine computes or server sends.
        
        Args:
            callback: Function(move) called for LED display.
        """
        self._pending_move_callback = callback
        self._white_player.set_pending_move_callback(callback)
        self._black_player.set_pending_move_callback(callback)
    
    def set_ready_callback(self, callback: Callable[[], None]) -> None:
        """Set callback for when all players are ready.
        
        Args:
            callback: Function() called when both players are ready.
        """
        self._ready_callback = callback
    
    def set_status_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback for player status messages.
        
        Args:
            callback: Function(message) for status updates.
        """
        self._status_callback = callback
        self._white_player.set_status_callback(callback)
        self._black_player.set_status_callback(callback)
    
    def set_error_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback for player error conditions.
        
        Called when a player detects an error that requires correction mode,
        such as place-without-lift (extra piece on board).
        
        Args:
            callback: Function(error_type) for error conditions.
        """
        self._error_callback = callback
        self._white_player.set_error_callback(callback)
        self._black_player.set_error_callback(callback)
    
    # =========================================================================
    # Lifecycle Methods
    # =========================================================================
    
    def start(self) -> bool:
        """Start both players.
        
        Returns:
            True if both players started successfully.
        """
        log.info("[PlayerManager] Starting players")
        
        white_ok = self._white_player.start()
        black_ok = self._black_player.start()
        
        if not white_ok:
            log.error("[PlayerManager] White player failed to start")
        if not black_ok:
            log.error("[PlayerManager] Black player failed to start")
        
        return white_ok and black_ok
    
    def stop(self) -> None:
        """Stop both players and release resources."""
        log.info("[PlayerManager] Stopping players")
        self._white_player.stop()
        self._black_player.stop()
    
    def clear_pending_moves(self) -> None:
        """Clear any pending moves from both players.
        
        Called when an external app connects and takes over game control.
        """
        for player in [self._white_player, self._black_player]:
            if hasattr(player, 'clear_pending_move'):
                player.clear_pending_move()
    
    def on_new_game(self) -> None:
        """Notify both players of new game."""
        self._white_player.on_new_game()
        self._black_player.on_new_game()

    def bind_remote_sessions(
        self,
        *,
        clock_callback=None,
        game_info_callback=None,
        time_control_callback=None,
    ) -> None:
        """Offer clock and game-info callbacks to both slots.

        Local players no-op. A remote player forwards the server time control,
        remaining clock, and names. Callers must not inspect player classes.
        """
        for player in (self._white_player, self._black_player):
            player.bind_remote_session(
                clock_callback=clock_callback,
                game_info_callback=game_info_callback,
                time_control_callback=time_control_callback,
            )

    def abort_remote_games(self) -> None:
        """Abort any attached remote game (in-game abort menu)."""
        self._white_player.abort_remote_game()
        self._black_player.abort_remote_game()

    def leave_remote_games(self) -> None:
        """Detach any attached remote game.

        Timed games abort or resign so the opponent is not stranded.
        Correspondence disconnects without ending the server game.
        """
        self._white_player.leave_remote_game()
        self._black_player.leave_remote_game()
    
    def on_resign(self, color: chess.Color) -> None:
        """Notify both players that ``color`` resigned.

        Every resign gesture (BACK menu, kings-in-center, king lift) must reach
        both players: the resigning side's own player stops, and a remote
        opponent resigns the server's game so it does not stay open after the
        board shows the result.

        Args:
            color: The side that resigned.
        """
        self._white_player.on_resign(color)
        self._black_player.on_resign(color)

    def on_takeback(self, board: chess.Board) -> None:
        """Notify both players of takeback.
        
        Args:
            board: Board position after takeback.
        """
        self._white_player.on_takeback(board)
        self._black_player.on_takeback(board)
    
    # =========================================================================
    # Game Flow Methods
    # =========================================================================
    
    def get_player(self, color: chess.Color) -> Player:
        """Get the player for a specific color.
        
        Args:
            color: chess.WHITE or chess.BLACK
            
        Returns:
            The player for that color.
        """
        return self._white_player if color == chess.WHITE else self._black_player
    
    def set_player(self, color: chess.Color, player: Player) -> Player:
        """Replace a player for a specific color.
        
        Used when a remote client connects and takes over from a local player
        (e.g., Millennium app replacing the local engine). The new player is
        wired with the same callbacks and started.
        
        Args:
            color: chess.WHITE or chess.BLACK
            player: The new player to use for this color.
            
        Returns:
            The previous player that was replaced.
        """
        # Get and stop the current player
        old_player = self.get_player(color)
        old_player.stop()
        
        # Set color on new player
        player.color = color
        
        # Wire callbacks to new player
        if self._move_callback:
            player.set_move_callback(self._move_callback)
        if self._pending_move_callback:
            player.set_pending_move_callback(self._pending_move_callback)
        if self._status_callback:
            player.set_status_callback(self._status_callback)
        if self._error_callback:
            player.set_error_callback(self._error_callback)
        player.set_ready_callback(self._on_player_ready)
        
        # Store new player
        if color == chess.WHITE:
            self._white_player = player
        else:
            self._black_player = player
        
        # Start the new player
        player.start()
        
        # Update observable PlayersState
        self._update_players_state()
        
        log.info(f"[PlayerManager] Replaced {old_player.name} with {player.name} for {'White' if color == chess.WHITE else 'Black'}")
        
        return old_player

    def reassign_slots(self, white_player: Player, black_player: Player) -> None:
        """Move already-started players between colors without stop/start.

        A Lichess stream may report the local account is Black after the players
        were built from settings. :meth:`set_player` stops the outgoing player,
        which would kill the live seek/stream. This only retargets the two
        existing instances. Names on PlayersState are left alone: for a Lichess
        game they already hold the gameFull usernames, and ``player.name`` on the
        remote slot is the local account, which would reverse the labels.
        """
        self._white_player = white_player
        self._black_player = black_player
        white_player.color = chess.WHITE
        black_player.color = chess.BLACK
        players_state = get_players_state()
        white_hand_brain = getattr(self._white_player._config, 'hand_brain', False)
        black_hand_brain = getattr(self._black_player._config, 'hand_brain', False)
        players_state.set_hand_brain(white_hand_brain, black_hand_brain)
        log.info(
            f"[PlayerManager] Reassigned slots: White={white_player.name}, "
            f"Black={black_player.name}"
        )
    
    def get_current_player(self, board: chess.Board) -> Player:
        """Get the player whose turn it is.
        
        Args:
            board: Current board position.
            
        Returns:
            The player to move.
        """
        return self.get_player(board.turn)
    
    def get_current_pending_move(self, board: chess.Board) -> Optional[chess.Move]:
        """Get the pending move from the current player, if any.
        
        For engine/Lichess players, this returns the computed move waiting
        to be executed on the physical board. For humans, returns None.
        
        Args:
            board: Current board position.
            
        Returns:
            The pending move, or None if no move is pending.
        """
        return self.get_current_player(board).pending_move
    
    def request_move(self, board: chess.Board) -> None:
        """Request a move from the current player.
        
        Notifies the current player it's their turn. They should prepare
        to receive piece events and eventually submit a move.
        
        The player's base class handles queuing if not yet ready.
        If the player is already thinking, this is a no-op.
        
        Args:
            board: Current board position.
        """
        player = self.get_current_player(board)
        
        # Skip if player is already thinking (avoids redundant calls)
        if player.is_thinking:
            log.debug(f"[PlayerManager] {player.name} already thinking, skipping request")
            return
        
        log.debug(f"[PlayerManager] Requesting move from {player.name}")
        player.request_move(board)
    
    def on_piece_event(self, event_type: str, square: int, board: chess.Board) -> None:
        """Route a piece event to the current player.
        
        Called when a piece is lifted or placed on the physical board.
        The current player processes the event and may submit a move.
        
        Args:
            event_type: "lift" or "place"
            square: The square index (0-63)
            board: Current board position
        """
        player = self.get_current_player(board)
        player.on_piece_event(event_type, square, board)
    
    def on_move_made(self, move: chess.Move, board: chess.Board) -> None:
        """Notify both players that a move was made.
        
        Args:
            move: The move that was made.
            board: Board position after the move.
        """
        self._white_player.on_move_made(move, board)
        self._black_player.on_move_made(move, board)
    
    # =========================================================================
    # Properties
    # =========================================================================
    
    @property
    def white_player(self) -> Player:
        """The White player."""
        return self._white_player
    
    @property
    def black_player(self) -> Player:
        """The Black player."""
        return self._black_player
    
    @property
    def is_two_human(self) -> bool:
        """Check if both players are human (2-player mode)."""
        return (self._white_player.player_type == PlayerType.HUMAN and 
                self._black_player.player_type == PlayerType.HUMAN)
    
    @property
    def has_engine(self) -> bool:
        """Check if either player is an engine."""
        return (self._white_player.player_type == PlayerType.ENGINE or 
                self._black_player.player_type == PlayerType.ENGINE)
    
    @property
    def requires_rebuild_on_new_game(self) -> bool:
        """True when either player cannot continue in place after a local reset."""
        return (
            self._white_player.requires_rebuild_on_new_game
            or self._black_player.requires_rebuild_on_new_game
        )
    
    @property
    def is_ready(self) -> bool:
        """Check if both players are ready."""
        return self._white_player.is_ready and self._black_player.is_ready
    
    @property
    def supports_takeback(self) -> bool:
        """Check if both players support takeback."""
        return (self._white_player.supports_takeback() and 
                self._black_player.supports_takeback())
    
    def get_info(self) -> dict:
        """Get information about both players."""
        return {
            'white': self._white_player.get_info(),
            'black': self._black_player.get_info(),
            'is_two_human': self.is_two_human,
            'has_engine': self.has_engine,
            'requires_rebuild_on_new_game': self.requires_rebuild_on_new_game,
        }
