"""
Remote game controller.

Handles games where an external Bluetooth app (Millennium, Pegasus, Chessnut)
controls the game. The physical board becomes a sensor that reports piece
positions to the app.
"""

from typing import TYPE_CHECKING, Optional, Callable

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from .base import GameController

if TYPE_CHECKING:
    from universalchess.managers.game import GameManager


# Client type constants
CLIENT_UNKNOWN = "unknown"
CLIENT_MILLENNIUM = "millennium"
CLIENT_PEGASUS = "pegasus"
CLIENT_CHESSNUT = "chessnut"


class RemoteController(GameController):
    """Controller for games controlled by external Bluetooth apps.
    
    Manages the protocol emulators (Millennium, Pegasus, Chessnut) that
    communicate with external chess apps. The app controls the game;
    the physical board reports piece positions.
    
    Auto-detects the protocol from incoming data.
    """
    
    def __init__(self, game_manager: 'GameManager', send_callback: Optional[Callable] = None):
        """Initialize the remote controller.
        
        Args:
            game_manager: The GameManager instance.
            send_callback: Callback function(data) to send messages to the client.
        """
        super().__init__(game_manager)
        self._send_callback = send_callback
        self._client_type_hint: Optional[str] = None
        self._client_type = CLIENT_UNKNOWN
        
        # Protocol detection flags
        self._is_millennium = False
        self._is_pegasus = False
        self._is_chessnut = False
        
        # Emulator instances
        self._millennium = None
        self._pegasus = None
        self._chessnut = None
        
        # Compare mode for relay
        self._compare_mode = False
        self._pending_response = None
        
        # Callback for protocol detection notification
        self._protocol_detected_callback: Optional[Callable[[str], None]] = None
    
    def set_protocol_detected_callback(self, callback: Optional[Callable[[str], None]]) -> None:
        """Set callback for when a protocol is detected.
        
        Called once when the protocol (Millennium, Pegasus, Chessnut) is first
        detected from incoming data. Used to swap engine players to human players
        for remote-controlled games.
        
        Args:
            callback: Function(client_type) called when protocol is detected.
                      client_type is one of CLIENT_MILLENNIUM, CLIENT_PEGASUS, CLIENT_CHESSNUT.
        """
        self._protocol_detected_callback = callback
    
    def set_client_type_hint(self, hint: Optional[str]) -> None:
        """Set a hint for the expected client type.
        
        Args:
            hint: CLIENT_MILLENNIUM, CLIENT_PEGASUS, CLIENT_CHESSNUT, or None
        """
        self._client_type_hint = hint
    
    def set_compare_mode(self, enabled: bool) -> None:
        """Enable/disable compare mode for relay.
        
        In compare mode, responses are buffered instead of sent.
        """
        self._compare_mode = enabled
    
    def _create_emulators(self, force: bool = False) -> None:
        """Create the protocol emulators.
        
        By default, preserves existing emulators to maintain their state
        (important for protocol state machines like Pegasus which track
        connection state). Use force=True to recreate all emulators for
        a new connection.
        
        Args:
            force: If True, recreate all emulators even if they exist.
        """
        from universalchess.emulators.millennium import Millennium
        from universalchess.emulators.pegasus import Pegasus
        from universalchess.emulators.chessnut import Chessnut
        
        if force or self._millennium is None:
            self._millennium = Millennium(
                sendMessage_callback=self._handle_emulator_response,
                manager=self._game_manager
            )
            log.debug("[RemoteController] Created Millennium emulator")
        
        if force or self._pegasus is None:
            self._pegasus = Pegasus(
                sendMessage_callback=self._handle_emulator_response,
                manager=self._game_manager
            )
            log.debug("[RemoteController] Created Pegasus emulator")
        
        if force or self._chessnut is None:
            self._chessnut = Chessnut(
                sendMessage_callback=self._handle_emulator_response,
                manager=self._game_manager
            )
            log.debug("[RemoteController] Created Chessnut emulator")
    
    def _handle_emulator_response(self, data) -> None:
        """Handle response from an emulator.
        
        In compare mode, buffers the response. Otherwise, sends to client.
        """
        if self._compare_mode:
            self._pending_response = bytes(data) if data else None
            log.debug(f"[RemoteController] Response buffered ({len(data) if data else 0} bytes)")
        else:
            if self._send_callback:
                self._send_callback(data)
    
    @property
    def client_type(self) -> str:
        """The detected client type."""
        return self._client_type
    
    @property
    def is_protocol_detected(self) -> bool:
        """Whether a protocol has been detected."""
        return self._is_millennium or self._is_pegasus or self._is_chessnut
    
    # =========================================================================
    # GameController Interface
    # =========================================================================
    
    def start(self) -> None:
        """Start the remote controller.
        
        Creates emulators and prepares for incoming connections.
        Note: Does NOT subscribe to GameManager - events are routed through
        ControllerManager from LocalController to avoid multiple game threads.
        """
        self._active = True
        self._create_emulators()
        log.info("[RemoteController] Started")
    
    def stop(self) -> None:
        """Stop the remote controller.
        
        Resets protocol detection but keeps emulators for potential reuse.
        """
        self._active = False
        self._reset_protocol_detection()
        log.info("[RemoteController] Stopped")
    
    def on_field_event(self, piece_event: int, field: int, time_seconds: float) -> None:
        """Handle piece lift/place from the physical board.
        
        Forwards to the detected emulator for state sync with the app.
        """
        if not self._active:
            return
        
        # Skip forwarding lift/place while HandBrain REVERSE is consuming events locally
        try:
            pm = getattr(self._game_manager, "_player_manager", None)
            if pm is not None:
                current = pm.get_player(self._game_manager.chess_board.turn)
                from universalchess.players.hand_brain import HandBrainPlayer, HandBrainMode
                if isinstance(current, HandBrainPlayer) and current.mode == HandBrainMode.REVERSE:
                    return
        except Exception:
            pass
        
        # Forward to active emulator
        from universalchess.managers.events import EVENT_LIFT_PIECE, EVENT_PLACE_PIECE
        event = EVENT_LIFT_PIECE if piece_event == 0 else EVENT_PLACE_PIECE
        
        if self._is_millennium and self._millennium:
            if hasattr(self._millennium, 'handle_manager_event'):
                self._millennium.handle_manager_event(event, piece_event, field, time_seconds)
        elif self._is_pegasus and self._pegasus:
            if hasattr(self._pegasus, 'handle_manager_event'):
                self._pegasus.handle_manager_event(event, piece_event, field, time_seconds)
        elif self._is_chessnut and self._chessnut:
            if hasattr(self._chessnut, 'handle_manager_event'):
                self._chessnut.handle_manager_event(event, piece_event, field, time_seconds)
        else:
            # Protocol not yet detected - forward to all emulators
            if self._millennium and hasattr(self._millennium, 'handle_manager_event'):
                self._millennium.handle_manager_event(event, piece_event, field, time_seconds)
            if self._pegasus and hasattr(self._pegasus, 'handle_manager_event'):
                self._pegasus.handle_manager_event(event, piece_event, field, time_seconds)
            if self._chessnut and hasattr(self._chessnut, 'handle_manager_event'):
                self._chessnut.handle_manager_event(event, piece_event, field, time_seconds)
    
    def on_key_event(self, key) -> None:
        """Handle key press from the physical board.
        
        Forwards to the detected emulator.
        """
        if not self._active:
            return
        
        if self._is_millennium and self._millennium:
            if hasattr(self._millennium, 'handle_manager_key'):
                self._millennium.handle_manager_key(key)
        elif self._is_pegasus and self._pegasus:
            if hasattr(self._pegasus, 'handle_manager_key'):
                self._pegasus.handle_manager_key(key)
        elif self._is_chessnut and self._chessnut:
            if hasattr(self._chessnut, 'handle_manager_key'):
                self._chessnut.handle_manager_key(key)
    
    # =========================================================================
    # Protocol Handling
    # =========================================================================
    
    def receive_data(self, byte_value: int) -> bool:
        """Receive a byte from the Bluetooth connection.
        
        Auto-detects protocol from incoming data. Once detected,
        unused emulators are freed.
        
        Args:
            byte_value: Raw byte value from wire.
            
        Returns:
            True if byte was successfully parsed, False otherwise.
        """
        if not self._active:
            return False
        
        # If already detected, route directly
        if self._is_millennium and self._millennium:
            return self._millennium.parse_byte(byte_value)
        elif self._is_pegasus and self._pegasus:
            return self._pegasus.parse_byte(byte_value)
        elif self._is_chessnut and self._chessnut:
            return self._chessnut.parse_byte(byte_value)
        
        # Auto-detect: try emulators in priority order based on hint
        emulators_to_try = self._get_emulator_priority()
        
        for emulator, name, client_type in emulators_to_try:
            if emulator and emulator.parse_byte(byte_value):
                hint_info = ""
                if self._client_type_hint == client_type:
                    hint_info = " (matches hint)"
                elif self._client_type_hint:
                    hint_info = f" (hint was {self._client_type_hint})"
                
                log.info(f"[RemoteController] {name} protocol detected{hint_info}")
                
                self._client_type = client_type
                self._set_detected_protocol(client_type)
                return True
        
        return False
    
    def _get_emulator_priority(self) -> list:
        """Get emulators in priority order based on hint."""
        if self._client_type_hint == CLIENT_MILLENNIUM:
            return [
                (self._millennium, "Millennium", CLIENT_MILLENNIUM),
                (self._pegasus, "Pegasus", CLIENT_PEGASUS),
                (self._chessnut, "Chessnut", CLIENT_CHESSNUT),
            ]
        elif self._client_type_hint == CLIENT_PEGASUS:
            return [
                (self._pegasus, "Pegasus", CLIENT_PEGASUS),
                (self._millennium, "Millennium", CLIENT_MILLENNIUM),
                (self._chessnut, "Chessnut", CLIENT_CHESSNUT),
            ]
        elif self._client_type_hint == CLIENT_CHESSNUT:
            return [
                (self._chessnut, "Chessnut", CLIENT_CHESSNUT),
                (self._millennium, "Millennium", CLIENT_MILLENNIUM),
                (self._pegasus, "Pegasus", CLIENT_PEGASUS),
            ]
        else:
            return [
                (self._millennium, "Millennium", CLIENT_MILLENNIUM),
                (self._pegasus, "Pegasus", CLIENT_PEGASUS),
                (self._chessnut, "Chessnut", CLIENT_CHESSNUT),
            ]
    
    def _set_detected_protocol(self, client_type: str) -> None:
        """Set the detected protocol and free unused emulators.
        
        Also fires the protocol detection callback if set, allowing
        ProtocolManager to swap engine players to human players.
        """
        if client_type == CLIENT_MILLENNIUM:
            self._is_millennium = True
            self._pegasus = None
            self._chessnut = None
        elif client_type == CLIENT_PEGASUS:
            self._is_pegasus = True
            self._millennium = None
            self._chessnut = None
        elif client_type == CLIENT_CHESSNUT:
            self._is_chessnut = True
            self._millennium = None
            self._pegasus = None
        
        # Notify listener that protocol was detected
        if self._protocol_detected_callback:
            self._protocol_detected_callback(client_type)
    
    def _reset_protocol_detection(self) -> None:
        """Reset protocol detection for new connection."""
        self._is_millennium = False
        self._is_pegasus = False
        self._is_chessnut = False
        self._client_type = CLIENT_UNKNOWN
    
    def reset_parser(self) -> None:
        """Reset packet parser state for all active emulators."""
        if self._millennium:
            self._millennium.reset_parser()
        if self._pegasus:
            self._pegasus.reset()
        if self._chessnut:
            self._chessnut.reset()
    
    # =========================================================================
    # Game Event Handling (from ControllerManager forwarding)
    # =========================================================================
    
    def on_game_event(self, event, piece_event=None, field=None, time_seconds=None) -> None:
        """Handle game-level event from GameManager (new game, turn, termination).

        Routes to the detected emulator for sync with the app.

        Piece lift/place events are intentionally NOT handled here: they are
        delivered to emulators via on_field_event (the raw board-event path).
        ControllerManager.on_field_event drives both paths from the same source
        under the same is_protocol_detected gating, so forwarding piece events
        here too delivered every lift/place to the emulator twice. That is
        harmless for the idempotent FEN handler but corrupts stateful handlers -
        e.g. the Chessnut setup tracker, where a duplicate lift (same square,
        already empty) discards the held piece. Single-source the piece events
        through on_field_event.
        """
        if not self._active:
            return

        from universalchess.managers.events import EVENT_LIFT_PIECE, EVENT_PLACE_PIECE
        if event in (EVENT_LIFT_PIECE, EVENT_PLACE_PIECE):
            return

        if self._is_millennium and self._millennium:
            if hasattr(self._millennium, 'handle_manager_event'):
                self._millennium.handle_manager_event(event, piece_event, field, time_seconds)
        elif self._is_pegasus and self._pegasus:
            if hasattr(self._pegasus, 'handle_manager_event'):
                self._pegasus.handle_manager_event(event, piece_event, field, time_seconds)
        elif self._is_chessnut and self._chessnut:
            if hasattr(self._chessnut, 'handle_manager_event'):
                self._chessnut.handle_manager_event(event, piece_event, field, time_seconds)
        else:
            # Protocol not yet detected - forward to all
            if self._millennium and hasattr(self._millennium, 'handle_manager_event'):
                self._millennium.handle_manager_event(event, piece_event, field, time_seconds)
            if self._pegasus and hasattr(self._pegasus, 'handle_manager_event'):
                self._pegasus.handle_manager_event(event, piece_event, field, time_seconds)
            if self._chessnut and hasattr(self._chessnut, 'handle_manager_event'):
                self._chessnut.handle_manager_event(event, piece_event, field, time_seconds)
    
    def on_move_made(self, move) -> None:
        """Handle move made on the board.
        
        Notifies the detected emulator for sync with the app.
        """
        if not self._active:
            return
        
        if self._is_millennium and self._millennium:
            if hasattr(self._millennium, 'handle_manager_move'):
                self._millennium.handle_manager_move(move)
        elif self._is_pegasus and self._pegasus:
            if hasattr(self._pegasus, 'handle_manager_move'):
                self._pegasus.handle_manager_move(move)
        elif self._is_chessnut and self._chessnut:
            if hasattr(self._chessnut, 'handle_manager_move'):
                self._chessnut.handle_manager_move(move)
        else:
            # Protocol not yet detected - forward to all
            if self._millennium and hasattr(self._millennium, 'handle_manager_move'):
                self._millennium.handle_manager_move(move)
            if self._pegasus and hasattr(self._pegasus, 'handle_manager_move'):
                self._pegasus.handle_manager_move(move)
            if self._chessnut and hasattr(self._chessnut, 'handle_manager_move'):
                self._chessnut.handle_manager_move(move)
    
    def on_takeback(self) -> None:
        """Handle takeback event.
        
        Notifies the detected emulator.
        """
        if not self._active:
            return
        
        if self._is_millennium and self._millennium:
            if hasattr(self._millennium, 'handle_manager_takeback'):
                self._millennium.handle_manager_takeback()
        elif self._is_pegasus and self._pegasus:
            if hasattr(self._pegasus, 'handle_manager_takeback'):
                self._pegasus.handle_manager_takeback()
        elif self._is_chessnut and self._chessnut:
            if hasattr(self._chessnut, 'handle_manager_takeback'):
                self._chessnut.handle_manager_takeback()
    
    def on_key_from_game_manager(self, key) -> None:
        """Handle key event from GameManager.
        
        Notifies the detected emulator.
        """
        if not self._active:
            return
        
        if self._is_millennium and self._millennium:
            if hasattr(self._millennium, 'handle_manager_key'):
                self._millennium.handle_manager_key(key)
        elif self._is_pegasus and self._pegasus:
            if hasattr(self._pegasus, 'handle_manager_key'):
                self._pegasus.handle_manager_key(key)
        elif self._is_chessnut and self._chessnut:
            if hasattr(self._chessnut, 'handle_manager_key'):
                self._chessnut.handle_manager_key(key)
    
    # =========================================================================
    # Compare Mode (for relay)
    # =========================================================================
    
    def get_pending_response(self) -> Optional[bytes]:
        """Get and clear the pending emulator response.
        
        Used in compare mode for relay functionality.
        """
        response = self._pending_response
        self._pending_response = None
        return response
    
    def compare_with_shadow(self, shadow_response: bytes) -> tuple:
        """Compare shadow host response with emulator response.
        
        Returns:
            Tuple (match, emulator_response).
        """
        emulator_response = self.get_pending_response()
        
        if emulator_response is None:
            return (None, None)
        
        shadow_bytes = bytes(shadow_response) if shadow_response else b''
        match = emulator_response == shadow_bytes
        
        if not match:
            log.warning("[RemoteController] Response MISMATCH:")
            log.warning(f"  Shadow: {shadow_bytes.hex() if shadow_bytes else '(empty)'}")
            log.warning(f"  Emulator: {emulator_response.hex() if emulator_response else '(empty)'}")
        
        return (match, emulator_response)
