"""
Local game controller.

Handles games where moves come from local players (human at the board,
chess engine, or Lichess opponent). The physical board is the primary
input for human moves.
"""

import chess
from typing import TYPE_CHECKING, Optional, Callable

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from .base import GameController

if TYPE_CHECKING:
    from universalchess.managers.game import GameManager
    from universalchess.players import PlayerManager
    from universalchess.managers.assistant import AssistantManager
    from universalchess.assistants import Suggestion


class LocalController(GameController):
    """Controller for local games with PlayerManager.
    
    Manages games where:
    - Human plays on physical board
    - Engine computes opponent moves
    - Lichess provides remote opponent moves
    
    Field events are routed to GameManager for move detection.
    Turn changes trigger player move requests.
    """
    
    def __init__(self, game_manager: 'GameManager'):
        """Initialize the local controller.
        
        Args:
            game_manager: The GameManager instance.
        """
        super().__init__(game_manager)
        self._player_manager: Optional['PlayerManager'] = None
        # Per-color assistant managers for Hand+Brain mode
        # Each human player can have their own assistant engine configured
        self._white_assistant_manager: Optional['AssistantManager'] = None
        self._black_assistant_manager: Optional['AssistantManager'] = None
        # Legacy single assistant manager for backward compatibility
        self._assistant_manager: Optional['AssistantManager'] = None
        self._suggestion_callback: Optional[Callable] = None
        self._takeback_callback: Optional[Callable] = None
        # Callback to forward game events to RemoteController (for Bluetooth sync)
        self._event_forward_callback: Optional[Callable] = None
        # Callback for external game event handling (reset analysis, clocks, etc.)
        self._external_event_callback: Optional[Callable] = None
        # When True, prevent this controller from requesting moves from players.
        # Used during resume while the saved move list is being replayed into the game state.
        self._suppress_move_requests: bool = False

    def set_suppress_move_requests(self, suppress: bool) -> None:
        """Enable/disable move requests from this controller.
        
        Args:
            suppress: If True, suppresses calls that would trigger player.request_move().
        """
        self._suppress_move_requests = suppress
        log.info(f"[LocalController] suppress_move_requests set to {suppress}")
    
    def set_player_manager(self, player_manager: 'PlayerManager') -> None:
        """Set the player manager.
        
        Args:
            player_manager: The PlayerManager instance.
        """
        self._player_manager = player_manager
        self._game_manager.set_player_manager(player_manager)

        log.info(f"[LocalController] PlayerManager set: "
                 f"White={player_manager.white_player.name}, "
                 f"Black={player_manager.black_player.name}")
    
    def set_assistant_manager(self, assistant_manager: 'AssistantManager') -> None:
        """Set the assistant manager for Hand+Brain hints (legacy, single manager).
        
        For per-player assistant engines, use set_white_assistant_manager() and
        set_black_assistant_manager() instead.
        
        Args:
            assistant_manager: The AssistantManager instance.
        """
        self._assistant_manager = assistant_manager
        assistant_manager.set_suggestion_callback(self._on_assistant_suggestion)
    
    def set_white_assistant_manager(self, assistant_manager: 'AssistantManager') -> None:
        """Set the assistant manager for White player's Hand+Brain hints.
        
        Args:
            assistant_manager: The AssistantManager instance for White.
        """
        self._white_assistant_manager = assistant_manager
        assistant_manager.set_suggestion_callback(self._on_assistant_suggestion)
        log.info("[LocalController] White assistant manager set")
    
    def set_black_assistant_manager(self, assistant_manager: 'AssistantManager') -> None:
        """Set the assistant manager for Black player's Hand+Brain hints.
        
        Args:
            assistant_manager: The AssistantManager instance for Black.
        """
        self._black_assistant_manager = assistant_manager
        assistant_manager.set_suggestion_callback(self._on_assistant_suggestion)
        log.info("[LocalController] Black assistant manager set")
    
    def set_suggestion_callback(self, callback: Callable) -> None:
        """Set callback for assistant suggestions.
        
        Args:
            callback: Function(piece_symbol, squares) called with suggestion.
        """
        self._suggestion_callback = callback
    
    def set_takeback_callback(self, callback: Callable) -> None:
        """Set callback for takeback events.
        
        Args:
            callback: Function() called when takeback detected.
        """
        self._takeback_callback = callback
    
    def set_event_forward_callback(self, callback: Callable) -> None:
        """Set callback for forwarding game events to RemoteController.
        
        Used by ControllerManager to sync events to Bluetooth apps.
        
        Args:
            callback: Function(event_type, *args) called for each game event.
        """
        self._event_forward_callback = callback
    
    def set_external_event_callback(self, callback: Callable) -> None:
        """Set callback for external game event handling.
        
        Called with game events (EVENT_NEW_GAME, EVENT_WHITE_TURN, etc.)
        to allow external handlers to respond (e.g., reset analysis, clocks).
        
        Args:
            callback: Function(event) called for each game event.
        """
        self._external_event_callback = callback
    
    @property
    def player_manager(self) -> Optional['PlayerManager']:
        """Get the player manager."""
        return self._player_manager
    
    @property
    def is_lichess(self) -> bool:
        """Whether this is a Lichess game."""
        if not self._player_manager:
            return False
        from universalchess.players.lichess import LichessPlayer
        return any(isinstance(p, LichessPlayer) 
                   for p in [self._player_manager.white_player, 
                             self._player_manager.black_player])
    
    @property
    def is_two_player_mode(self) -> bool:
        """Whether both players are human."""
        if self._player_manager:
            return self._player_manager.is_two_human
        return True
    
    # =========================================================================
    # GameController Interface
    # =========================================================================
    
    def start(self) -> None:
        """Start the local controller.
        
        Subscribes to GameManager events and starts players.
        """
        self._active = True
        self._subscribe_to_game_manager()
        if self._player_manager:
            self._player_manager.start()
        log.info("[LocalController] Started")
    
    def _subscribe_to_game_manager(self) -> None:
        """Subscribe to GameManager events for local game coordination."""
        if not self._game_manager:
            log.warning("[LocalController] Cannot subscribe - no GameManager")
            return
        
        log.info("[LocalController] Subscribing to GameManager events")
        self._game_manager.subscribe_game(
            self._on_game_event,
            self._on_move_made,
            self._on_key_press,
            self._on_takeback
        )
    
    def stop(self) -> None:
        """Stop the local controller.
        
        Clears pending moves but doesn't stop players entirely
        (they may be resumed if controller is reactivated).
        """
        self._active = False
        if self._player_manager:
            self._player_manager.clear_pending_moves()
        log.info("[LocalController] Stopped")
    
    def on_field_event(self, piece_event: int, field: int, time_seconds: float) -> None:
        """Handle piece lift/place from the physical board.
        
        Routes to GameManager for move detection when active.
        """
        if not self._active:
            return
        self._game_manager.receive_field(piece_event, field, time_seconds)
    
    def on_key_event(self, key) -> None:
        """Handle key press from the physical board.
        
        Routes to GameManager when active.
        """
        if not self._active:
            return
        self._game_manager.receive_key(key)
    
    # =========================================================================
    # Player Readiness
    # =========================================================================
    
    def on_all_players_ready(self) -> None:
        """Handle all players becoming ready.
        
        Called by PlayerManager when both players are ready.
        Triggers first move request if White is not human.
        """
        from universalchess.players import PlayerType
        
        if not self._player_manager:
            return

        # During resume, the saved move list is replayed AFTER _start_game_mode().
        # Requesting a move here would pass an incomplete board state to players
        # (notably Hand+Brain REVERSE), which can produce illegal suggestions.
        if self._suppress_move_requests:
            log.info("[LocalController] All players ready, but move requests are suppressed (resume in progress)")
            return
        
        white_player = self._player_manager.get_player(chess.WHITE)
        if white_player.player_type != PlayerType.HUMAN:
            log.info("[LocalController] All players ready, requesting first move from White")
            self._request_current_player_move()
        else:
            log.debug("[LocalController] All players ready, waiting for human to move")
    
    def _request_current_player_move(self) -> None:
        """Request a move from the current player.
        
        Called when turn changes. For human players, does nothing.
        For engines/Lichess, starts move computation.
        """
        if not self._player_manager:
            return

        if self._suppress_move_requests:
            log.debug("[LocalController] Move requests suppressed, skipping _request_current_player_move")
            return
        
        if not self._player_manager.is_ready:
            return
        
        chess_board = self._game_manager.chess_board
        
        if chess_board.is_game_over():
            return
        
        log.debug("[LocalController] Requesting move from current player")
        self._player_manager.request_move(chess_board)
    
    # =========================================================================
    # GameManager Subscription Callbacks
    # =========================================================================
    
    def _on_game_event(self, event, piece_event=None, field=None, time_seconds=None) -> None:
        """Handle game events from GameManager subscription.
        
        Called by GameManager for events like new game, turn changes, piece events.
        Triggers player move requests and assistant suggestions.
        Also notifies external callback for UI updates (analysis reset, clock management).
        
        Note: Display updates happen automatically via ChessGameState observer in
        DisplayManager - no manual update calls needed here.
        """
        try:
            from universalchess.managers.game import EVENT_NEW_GAME, EVENT_WHITE_TURN, EVENT_BLACK_TURN
            from universalchess.managers.events import EVENT_LIFT_PIECE, EVENT_PLACE_PIECE
            from universalchess.players.hand_brain import HandBrainPlayer, HandBrainMode
            
            log.debug(f"[LocalController] _on_game_event: {event}")
            
            # Piece lift/place events: for Hand+Brain REVERSE, do not forward to BT (avoid emulator clearing LEDs)
            if event == EVENT_LIFT_PIECE or event == EVENT_PLACE_PIECE:
                skip_bt = False
                if self._player_manager:
                    current = self._player_manager.get_player(self._game_manager.chess_board.turn)
                    if isinstance(current, HandBrainPlayer) and current.mode == HandBrainMode.REVERSE:
                        skip_bt = True
                if (not skip_bt) and self._event_forward_callback:
                    self._event_forward_callback('game_event', event, piece_event, field, time_seconds)
                return
            
            # Notify external handler (resets analysis on new game, manages clocks)
            if self._external_event_callback:
                self._external_event_callback(event)
            
            if event == EVENT_NEW_GAME:
                # Notify players and assistants of new game
                if self._player_manager:
                    self._player_manager.on_new_game()
                if self._assistant_manager:
                    self._assistant_manager.on_new_game()
                # Engine/human games continue in place. A Lichess player is still
                # attached to the remote game, so requesting a move here would be
                # against that game while the local board is at start. The app
                # rebuilds through _start_game_mode (new seek + waiting splash).
                if self._player_manager and self._player_manager.has_lichess:
                    log.info("[LocalController] Lichess board-reset - skipping in-place move request")
                else:
                    self._request_current_player_move()
                    self._check_assistant_suggestion()
            elif event == EVENT_WHITE_TURN or event == EVENT_BLACK_TURN:
                self._request_current_player_move()
                self._check_assistant_suggestion()
            
            # Forward to RemoteController for Bluetooth sync
            if self._event_forward_callback:
                self._event_forward_callback('game_event', event, piece_event, field, time_seconds)
        except Exception as e:
            log.error(f"[LocalController] Error in _on_game_event: {e}")
            import traceback
            traceback.print_exc()
    
    def _on_move_made(self, move) -> None:
        """Handle move events from GameManager subscription.
        
        Notifies players of moves made on the board.
        """
        try:
            log.debug(f"[LocalController] _on_move_made: {move}")
            
            if self._player_manager:
                chess_move = chess.Move.from_uci(str(move)) if not isinstance(move, chess.Move) else move
                self._player_manager.on_move_made(chess_move, self._game_manager.chess_board)
            
            # Forward to RemoteController for Bluetooth sync
            if self._event_forward_callback:
                self._event_forward_callback('move_made', move)
        except Exception as e:
            log.error(f"[LocalController] Error in _on_move_made: {e}")
            import traceback
            traceback.print_exc()
    
    def _on_key_press(self, key) -> None:
        """Handle key events from GameManager subscription."""
        log.debug(f"[LocalController] _on_key_press: {key}")
        # Forward to RemoteController for Bluetooth sync
        if self._event_forward_callback:
            self._event_forward_callback('key_press', key)
    
    def _on_takeback(self) -> None:
        """Handle takeback events from GameManager subscription."""
        try:
            log.info("[LocalController] _on_takeback")
            
            # Notify display callback
            if self._takeback_callback:
                self._takeback_callback()
            
            # Notify players
            if self._player_manager:
                self._player_manager.on_takeback(self._game_manager.chess_board)
            
            # Notify assistants
            if self._assistant_manager:
                self._assistant_manager.on_takeback(self._game_manager.chess_board)
            
            # Forward to RemoteController for Bluetooth sync
            if self._event_forward_callback:
                self._event_forward_callback('takeback')
        except Exception as e:
            log.error(f"[LocalController] Error in _on_takeback: {e}")
            import traceback
            traceback.print_exc()
    
    # =========================================================================
    # Assistant Handling
    # =========================================================================
    
    def _on_assistant_suggestion(self, suggestion: 'Suggestion') -> None:
        """Handle suggestion from assistant (Hand+Brain)."""
        from universalchess.assistants import SuggestionType
        
        if suggestion.suggestion_type == SuggestionType.PIECE_TYPE:
            log.info(f"[LocalController] Suggestion: piece type {suggestion.piece_type} "
                     f"(squares: {suggestion.squares})")
            if self._suggestion_callback:
                self._suggestion_callback(suggestion.piece_type or "", suggestion.squares)
        elif suggestion.suggestion_type == SuggestionType.MOVE:
            log.info(f"[LocalController] Suggestion: move "
                     f"{suggestion.move.uci() if suggestion.move else 'none'}")
    
    def _get_active_assistant_manager(self, color: bool) -> Optional['AssistantManager']:
        """Get the assistant manager for the given color.
        
        Checks per-color assistants first, then falls back to legacy single assistant.
        
        Args:
            color: chess.WHITE or chess.BLACK
        
        Returns:
            The AssistantManager for that color, or None if none configured.
        """
        import chess
        if color == chess.WHITE and self._white_assistant_manager:
            return self._white_assistant_manager
        elif color == chess.BLACK and self._black_assistant_manager:
            return self._black_assistant_manager
        return self._assistant_manager
    
    def _check_assistant_suggestion(self) -> None:
        """Check if assistant should provide a suggestion.
        
        Uses per-color assistant managers if configured, allowing each human player
        to have their own assistant engine for Hand+Brain mode.
        """
        chess_board = self._game_manager.chess_board
        
        # Get the assistant manager for the current turn's color
        assistant_manager = self._get_active_assistant_manager(chess_board.turn)
        
        if not assistant_manager:
            return
        
        if not assistant_manager.is_active:
            return
        
        if not assistant_manager.auto_suggest:
            return
        
        # Only show suggestions for human players
        if self._player_manager:
            from universalchess.players import PlayerType
            current_player = self._player_manager.get_current_player(chess_board)
            if current_player.player_type != PlayerType.HUMAN:
                assistant_manager.clear_suggestion()
                return
        
        if chess_board.is_game_over():
            return
        
        log.debug(f"[LocalController] Requesting suggestion from assistant manager (color={chess_board.turn})")
        assistant_manager.request_suggestion(chess_board, chess_board.turn)
    
    # =========================================================================
    # Cleanup
    # =========================================================================
    
    def cleanup(self) -> None:
        """Clean up resources.
        
        Stops all assistant managers if active, releasing engine processes.
        """
        log.info("[LocalController] Cleaning up...")
        
        # Stop per-color assistant managers
        if self._white_assistant_manager:
            self._white_assistant_manager.stop()
            self._white_assistant_manager = None
            log.debug("[LocalController] White assistant manager stopped")
        
        if self._black_assistant_manager:
            self._black_assistant_manager.stop()
            self._black_assistant_manager = None
            log.debug("[LocalController] Black assistant manager stopped")
        
        # Stop legacy single assistant manager
        if self._assistant_manager:
            self._assistant_manager.stop()
            self._assistant_manager = None
            log.debug("[LocalController] Assistant manager stopped")
        
        log.info("[LocalController] Cleanup complete")
