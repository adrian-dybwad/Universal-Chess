# Protocol Manager
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# This project started as a fork of DGTCentaur Mods by EdNekebno
# ( https://github.com/EdNekebno/DGTCentaur )
#
# Manages player integrations, assistant management, and game callbacks.
# Protocol emulation (Millennium, Pegasus, Chessnut) is now handled by
# RemoteController in the controllers/ module.
#
# Player integrations are handled via the players/ module, which provides
# clean abstractions for different player types (Human, Engine, Remote).
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

import time as _t
import logging as _log_temp
_logger = _log_temp.getLogger(__name__)
_s = _t.time()

from universalchess.board.logging import log
_logger.debug(f"[protocol import] board.logging: {(_t.time() - _s)*1000:.0f}ms"); _s = _t.time()

from universalchess.managers.game import GameManager
log.debug(f"[protocol import] game: {(_t.time() - _s)*1000:.0f}ms"); _s = _t.time()

# Import Player and Assistant managers
from universalchess.players import PlayerManager
from universalchess.managers.assistant import AssistantManager
log.debug(f"[protocol import] players/assistant managers: {(_t.time() - _s)*1000:.0f}ms"); _s = _t.time()

import chess
from typing import Optional
log.debug(f"[protocol import] stdlib: {(_t.time() - _s)*1000:.0f}ms")


class ProtocolManager:
    """Manages protocol parsing and routing for chess app connections.
    
    Supports Millennium, Pegasus, and Chessnut protocols. Auto-detects protocol
    from incoming data. Bridges external app protocols to GameManager.
    
    GameManager and PlayerManager are injected dependencies, not created here.
    """
    
    # Client type constants
    CLIENT_UNKNOWN = "unknown"
    CLIENT_MILLENNIUM = "millennium"
    CLIENT_PEGASUS = "pegasus"
    CLIENT_CHESSNUT = "chessnut"
    
    def __init__(self, game_manager: GameManager):
        """Initialize the ProtocolManager.
        
        Args:
            game_manager: The GameManager instance (required, injected dependency)
        """
        # Suggestion callback for assistants (Hand+Brain, hints, etc.)
        self._suggestion_callback = None
        
        # Player manager for white and black players
        self._player_manager: Optional[PlayerManager] = None
        
        # Assistant manager for Hand+Brain, hints, etc.
        self._assistant_manager: Optional[AssistantManager] = None
        
        # Game manager (injected dependency)
        self.game_manager = game_manager
        
        # Original player saved when remote client takes over.
        # Used to restore the player when remote client disconnects.
        # Tuple of (color, original_player) or None if no swap has occurred.
        self._original_player: Optional[tuple] = None
        
        # Protocol detection flags (set by RemoteController callback)
        self.is_millennium = False
        self.is_pegasus = False
        self.is_chessnut = False
        self.client_type = self.CLIENT_UNKNOWN
        
        log.info(f"[ProtocolManager] Initialized")
    
    @property
    def is_two_player_mode(self) -> bool:
        """Check if the game is in 2-player mode (both players are human).
        
        Returns:
            True if 2-player mode (human vs human), False otherwise
        """
        if self._player_manager:
            return self._player_manager.is_two_human
        return True
    
    @property
    def player_manager(self) -> Optional[PlayerManager]:
        """Get the player manager."""
        return self._player_manager
    
    def set_player_manager(self, player_manager: PlayerManager) -> None:
        """Set the player manager.
        
        Args:
            player_manager: The PlayerManager instance
        """
        self._player_manager = player_manager
        self.game_manager.set_player_manager(player_manager)
        player_manager.bind_remote_sessions(
            clock_callback=self._on_remote_clock_update,
            game_info_callback=self._on_remote_game_info,
        )
        
        log.info(f"[ProtocolManager] PlayerManager set: White={player_manager.white_player.name}, Black={player_manager.black_player.name}")
    
    def set_suggestion_callback(self, callback) -> None:
        """Set the suggestion callback for Hand+Brain hints.
        
        Args:
            callback: Function(piece_symbol, squares) called with suggestion
        """
        self._suggestion_callback = callback
    
    # =========================================================================
    # GameManager Callback Delegation (Law of Demeter compliance)
    # =========================================================================
    
    def set_on_terminal_position(self, callback):
        """Set callback for terminal positions. Callback(result, termination)."""
        self.game_manager.on_terminal_position = callback
    
    def set_on_back_pressed(self, callback):
        """Set callback for BACK button during game. Callback()."""
        self.game_manager.on_back_pressed = callback
    
    def set_on_kings_in_center(self, callback):
        """Set callback for kings-in-center gesture. Callback()."""
        self.game_manager.on_kings_in_center = callback
    
    def set_on_kings_in_center_cancel(self, callback):
        """Set callback when kings-in-center gesture is cancelled. Callback()."""
        self.game_manager.on_kings_in_center_cancel = callback
    
    def set_on_king_lift_resign(self, callback):
        """Set callback for king-lift resign gesture. Callback(color)."""
        self.game_manager.on_king_lift_resign = callback
    
    def set_on_king_lift_resign_cancel(self, callback):
        """Set callback to cancel king-lift resign. Callback()."""
        self.game_manager.on_king_lift_resign_cancel = callback

    def set_on_promotion_needed(self, callback):
        """Set callback for promotion selection. Callback(is_white) -> piece_symbol."""
        self.game_manager.on_promotion_needed = callback

    def receive_key(self, key_id):
        """Receive a key event from the app coordinator and forward to GameManager.
        
        This is called by main.py when it receives key events from the board.
        The event is forwarded to GameManager which handles game-related key logic.
        
        Args:
            key_id: Key identifier (board.Key enum value)
        """
        if self.game_manager:
            self.game_manager.receive_key(key_id)
    
    def receive_field(self, piece_event: int, field: int, time_in_seconds: float):
        """Receive a field event from the app coordinator and forward to GameManager.
        
        This is called by main.py when it receives field events from the board.
        The event is forwarded to GameManager which handles piece detection logic.
        
        Args:
            piece_event: 0 = lift, 1 = place
            field: Board field index (0-63)
            time_in_seconds: Event timestamp
        """
        if self.game_manager:
            self.game_manager.receive_field(piece_event, field, time_in_seconds)

    # =========================================================================
    # Player Management
    # =========================================================================
    
    def start_players(self) -> bool:
        """Start all configured players.
        
        Returns:
            True if all players started successfully.
        """
        if self._player_manager:
            return self._player_manager.start()
        return False
    
    def stop_players(self) -> None:
        """Stop all players and release resources."""
        if self._player_manager:
            self._player_manager.stop()
    
    # =========================================================================
    # Remote session (clock and names from an attached online game)
    # =========================================================================
    
    def _on_remote_clock_update(self, white_time: int, black_time: int):
        """Handle clock update from a remote player.

        Args:
            white_time: White's remaining time in seconds.
            black_time: Black's remaining time in seconds.
        """
        if self.game_manager:
            self.game_manager.set_clock(white_time, black_time)
    
    def _on_remote_game_info(self, white_player: str, white_rating: str,
                               black_player: str, black_rating: str):
        """Handle game info update from a remote player.

        Args:
            white_player: White player's username.
            white_rating: White player's rating.
            black_player: Black player's username.
            black_rating: Black player's rating.
        """
        if self.game_manager:
            white_str = f"{white_player}({white_rating})"
            black_str = f"{black_player}({black_rating})"
            self.game_manager.set_game_info("", "", "", white_str, black_str)
    
    # =========================================================================
    # App Connection Management
    # =========================================================================
    
    def is_app_connected(self) -> bool:
        """Check if any chess app protocol has been detected.
        
        Note: When ControllerManager is used, controller switching is managed
        by ControllerManager, not by checking this method.
        
        Returns:
            True if a chess app is connected (Millennium, Pegasus, or Chessnut)
        """
        return self.is_millennium or self.is_pegasus or self.is_chessnut

    def on_app_connected(self):
        """Called when a BLE app connects.
        
        Clears any pending engine moves to prepare for remote control.
        The actual player swap happens in on_protocol_detected() when
        the protocol type is identified.
        """
        log.info("[ProtocolManager] App connected - clearing pending moves")
        
        # Clear any pending engine moves so they don't interfere
        if self._player_manager:
            self._player_manager.clear_pending_moves()
    
    def on_protocol_detected(self, client_type: str):
        """Called when a BLE protocol is detected.
        
        Swaps the engine player with a HumanPlayer named after the remote
        client type (Millennium, Pegasus, Chessnut). This allows the remote
        app to control the game - any legal move made on the board is accepted.
        
        Args:
            client_type: The detected protocol type (CLIENT_MILLENNIUM, etc.)
        """
        from universalchess.players import HumanPlayer, EnginePlayer
        from universalchess.players.human import HumanPlayerConfig
        
        # Map client type to display name
        client_names = {
            self.CLIENT_MILLENNIUM: "Millennium",
            self.CLIENT_PEGASUS: "Pegasus",
            self.CLIENT_CHESSNUT: "Chessnut",
        }
        client_name = client_names.get(client_type, client_type)
        
        # Set protocol detection flags
        self.client_type = client_type
        self.is_millennium = (client_type == self.CLIENT_MILLENNIUM)
        self.is_pegasus = (client_type == self.CLIENT_PEGASUS)
        self.is_chessnut = (client_type == self.CLIENT_CHESSNUT)
        
        log.info(f"[ProtocolManager] Protocol detected: {client_name}")
        
        if not self._player_manager:
            log.warning("[ProtocolManager] No player manager - cannot swap player")
            return
        
        # Find engine player and swap with human player
        # Remote apps typically control the engine side (Black in Human vs Engine games)
        for color in [chess.BLACK, chess.WHITE]:
            player = self._player_manager.get_player(color)
            if isinstance(player, EnginePlayer):
                # Create a HumanPlayer with the remote client's name
                config = HumanPlayerConfig(name=client_name)
                remote_player = HumanPlayer(config)
                
                # Swap the player
                original_player = self._player_manager.set_player(color, remote_player)
                
                # Store original player for restoration on disconnect
                self._original_player = (color, original_player)
                
                color_name = "White" if color == chess.WHITE else "Black"
                log.info(f"[ProtocolManager] Swapped {original_player.name} with {client_name} for {color_name}")
                break
        else:
            log.debug("[ProtocolManager] No engine player to swap - already human vs human")
    
    def on_app_disconnected(self):
        """Called when app disconnects - restore original player.
        
        Restores the original engine player that was swapped out when the
        remote client connected. Then requests a move from the current player.
        
        Emulator recreation is now handled by ControllerManager/RemoteController.
        """
        log.info("[ProtocolManager] App disconnected - restoring local player")
        
        # Restore original player if one was swapped
        if self._original_player and self._player_manager:
            color, original_player = self._original_player
            
            # Get current player name for logging
            current_player = self._player_manager.get_player(color)
            
            # Swap back to original player
            self._player_manager.set_player(color, original_player)
            
            color_name = "White" if color == chess.WHITE else "Black"
            log.info(f"[ProtocolManager] Restored {original_player.name} for {color_name} (was {current_player.name})")
            
            self._original_player = None
        
        # Reset protocol detection flags
        self.is_millennium = False
        self.is_pegasus = False
        self.is_chessnut = False
        self.client_type = self.CLIENT_UNKNOWN
        
        # Note: Move request is handled by ControllerManager.on_bluetooth_disconnected()
        # which calls LocalController._request_current_player_move() after activating local
    
    def cleanup(self):
        """Clean up resources including players, assistants, and game manager."""
        log.info("[ProtocolManager] Starting cleanup...")
        
        # Stop players
        log.info("[ProtocolManager] Stopping players...")
        if self._player_manager:
            try:
                self._player_manager.stop()
                log.info("[ProtocolManager] Players stopped")
            except Exception as e:
                log.error(f"[ProtocolManager] Error stopping players: {e}", exc_info=True)
        else:
            log.info("[ProtocolManager] No player manager to stop")
        
        # Stop assistant manager if active
        log.info("[ProtocolManager] Stopping assistant manager...")
        if self._assistant_manager:
            try:
                self._assistant_manager.stop()
                log.info("[ProtocolManager] Assistant manager stopped")
            except Exception as e:
                log.error(f"[ProtocolManager] Error stopping assistant manager: {e}", exc_info=True)
            self._assistant_manager = None
        else:
            log.info("[ProtocolManager] No assistant manager to stop")
        
        # Unsubscribe from game manager (stops game thread)
        log.info("[ProtocolManager] Unsubscribing from game manager...")
        if self.game_manager:
            try:
                self.game_manager.unsubscribe_game()
                log.info("[ProtocolManager] Unsubscribed from game manager")
            except Exception as e:
                log.error(f"[ProtocolManager] Error unsubscribing from game manager: {e}", exc_info=True)
        else:
            log.info("[ProtocolManager] No game manager to unsubscribe from")
        
        log.info("[ProtocolManager] Cleanup complete")
