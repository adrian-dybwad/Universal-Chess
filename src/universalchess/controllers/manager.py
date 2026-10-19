"""
Controller manager.

Manages switching between LocalController and RemoteController.
Only one controller is active at a time.
"""

from typing import TYPE_CHECKING, Optional, Callable

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from .base import GameController
from .local import LocalController
from .remote import RemoteController

if TYPE_CHECKING:
    from universalchess.managers.game import GameManager


class ControllerManager:
    """Manages game controllers and handles switching between them.
    
    Ensures only one controller is active at a time. Routes events
    to the active controller.
    """
    
    def __init__(self, game_manager: 'GameManager'):
        """Initialize the controller manager.
        
        Args:
            game_manager: The GameManager instance.
        """
        self._game_manager = game_manager
        self._local: Optional[LocalController] = None
        self._remote: Optional[RemoteController] = None
        self._protocol_detected_listener: Optional[Callable[[str], None]] = None
        self._active: Optional[GameController] = None
        
        # Callback when controller changes
        self._on_controller_change: Optional[Callable] = None
    
    @property
    def local_controller(self) -> Optional[LocalController]:
        """Get the local controller."""
        return self._local
    
    @property
    def remote_controller(self) -> Optional[RemoteController]:
        """Get the remote controller."""
        return self._remote
    
    @property
    def active_controller(self) -> Optional[GameController]:
        """Get the currently active controller."""
        return self._active
    
    @property
    def is_local_active(self) -> bool:
        """Whether the local controller is active."""
        return self._active is self._local and self._local is not None
    
    @property
    def is_remote_active(self) -> bool:
        """Whether the remote controller is active."""
        return self._active is self._remote and self._remote is not None
    
    def set_on_controller_change(self, callback: Callable) -> None:
        """Set callback for controller changes.
        
        Args:
            callback: Function(is_remote: bool) called when controller changes.
        """
        self._on_controller_change = callback
    
    def create_local_controller(self) -> LocalController:
        """Create and return the local controller.
        
        Returns:
            The LocalController instance.
        """
        self._local = LocalController(self._game_manager)
        # Wire event forwarding to sync with RemoteController
        self._local.set_event_forward_callback(self._forward_event_to_remote)
        log.info("[ControllerManager] Created LocalController")
        return self._local
    
    def _forward_event_to_remote(self, event_type: str, *args) -> None:
        """Forward game events from LocalController to RemoteController.
        
        Called when LocalController receives events from GameManager.
        Forwards to RemoteController for Bluetooth app sync.
        
        Args:
            event_type: Type of event ('game_event', 'move_made', 'key_press', 'takeback')
            *args: Event-specific arguments
        """
        if not self._remote or not self._remote.is_protocol_detected:
            return
        
        if event_type == 'game_event':
            event, piece_event, field, time_seconds = args
            self._remote.on_game_event(event, piece_event, field, time_seconds)
        elif event_type == 'move_made':
            move = args[0]
            self._remote.on_move_made(move)
        elif event_type == 'key_press':
            key = args[0]
            self._remote.on_key_from_game_manager(key)
        elif event_type == 'takeback':
            self._remote.on_takeback()
    
    def create_remote_controller(self, send_callback: Optional[Callable] = None,
                                   protocol_detected_callback: Optional[Callable[[str], None]] = None) -> RemoteController:
        """Create and return the remote controller.
        
        Args:
            send_callback: Callback to send data to Bluetooth client.
            protocol_detected_callback: Callback when protocol is detected.
                Called with client_type (CLIENT_MILLENNIUM, etc.) when protocol
                is first detected from incoming data. Used by ProtocolManager
                to swap engine players to human players.
            
        Returns:
            The RemoteController instance.
        """
        self._remote = RemoteController(self._game_manager, send_callback)
        self._protocol_detected_listener = protocol_detected_callback
        self._remote.set_protocol_detected_callback(self._on_remote_protocol_detected)
        self._game_manager.on_web_piece_event = self._forward_web_piece_event
        
        log.info("[ControllerManager] Created RemoteController")
        return self._remote

    def _forward_web_piece_event(self, piece_event: int, field: int) -> None:
        """Report a piece event of a move played on the web board to the app.

        Goes to the app only, never to GameManager: the move is already played.
        There is no hardware timestamp, so the time is None.
        """
        if self._remote and self._remote.is_protocol_detected:
            self._remote.on_field_event(piece_event, field, None)

    def _on_remote_protocol_detected(self, client_type: str) -> None:
        """Hand the live game to the app once it has spoken a board protocol.

        The session starts here rather than in activate_remote, which runs as
        soon as a client connects: until a protocol is recognised the engine is
        still seated, and a session would leave it playing with no correction,
        no LEDs, and a frozen clock for a client that may not be a chess app.
        """
        self._game_manager.set_remote_app_session(True)
        if self._protocol_detected_listener:
            self._protocol_detected_listener(client_type)
    
    def activate_local(self) -> None:
        """Activate the local controller.
        
        Stops the remote controller if active.
        """
        if self._active is self._local:
            log.debug("[ControllerManager] Local already active")
            return
        
        if self._active:
            self._active.stop()
        
        self._active = self._local
        if self._local:
            self._local.start()
            log.info("[ControllerManager] Activated LocalController")
        
        if self._on_controller_change:
            self._on_controller_change(False)
    
    def activate_remote(self) -> None:
        """Activate the remote controller.
        
        Stops the local controller if active.
        """
        if self._active is self._remote:
            log.debug("[ControllerManager] Remote already active")
            return
        
        if self._active:
            self._active.stop()
        
        self._active = self._remote
        if self._remote:
            self._remote.start()
            log.info("[ControllerManager] Activated RemoteController")
            if self._remote.is_protocol_detected:
                self._game_manager.set_remote_app_session(True)
        
        if self._on_controller_change:
            self._on_controller_change(True)
    
    def deactivate_all(self) -> None:
        """Deactivate all controllers."""
        if self._active:
            self._active.stop()
            self._active = None
            log.info("[ControllerManager] Deactivated all controllers")
    
    # =========================================================================
    # Event Routing
    # =========================================================================
    
    def on_field_event(self, piece_event: int, field: int, time_seconds: float) -> None:
        """Route field event to active controller and GameManager.
        
        Field events always go to:
        1. GameManager for move processing
        2. Remote controller (if protocol detected) for BLE sync
        
        This ensures moves are processed regardless of which controller is active.

        Events of a correction after web play are kept from the app, including
        the touch that starts it and the one that completes it: the app's board
        is the web board's position, and putting the pieces back is not a move.
        """
        withheld = self._game_manager.is_withholding_physical_board
        # Always forward to GameManager for move processing
        self._game_manager.receive_field(piece_event, field, time_seconds)
        if withheld or self._game_manager.is_withholding_physical_board:
            return
        
        # If remote protocol is detected, also sync with BLE client
        if self._remote and self._remote.is_protocol_detected:
            self._remote.on_field_event(piece_event, field, time_seconds)
    
    def on_key_event(self, key) -> None:
        """Route key event to GameManager and remote controller.
        
        Key events always go to:
        1. GameManager for game control (BACK button, etc.)
        2. Remote controller (if protocol detected) for BLE sync
        """
        # Always forward to GameManager for game control
        self._game_manager.receive_key(key)
        
        # If remote protocol is detected, also sync with BLE client
        if self._remote and self._remote.is_protocol_detected:
            self._remote.on_key_event(key)
    
    # =========================================================================
    # Remote Data Handling
    # =========================================================================
    
    def receive_bluetooth_data(self, byte_value: int) -> bool:
        """Receive data from Bluetooth connection.
        
        If remote controller detects a protocol, activates it.
        
        Args:
            byte_value: Raw byte from Bluetooth.
            
        Returns:
            True if byte was parsed successfully.
        """
        if not self._remote:
            return False
        
        # Ensure emulators exist before trying to parse
        # (emulators are created in start() but we need them for protocol detection
        # even before the remote controller is activated)
        if self._remote._pegasus is None:
            self._remote._create_emulators()
        
        # If remote is not active but exists, start it temporarily for parsing
        was_inactive = not self._remote.is_active
        if was_inactive:
            self._remote._active = True  # Temporarily enable for parsing
        
        result = self._remote.receive_data(byte_value)
        
        # If protocol was detected, activate remote controller
        if result and self._remote.is_protocol_detected and self._active is not self._remote:
            log.info("[ControllerManager] Bluetooth protocol detected, activating remote")
            self.activate_remote()
        elif was_inactive and not self._remote.is_protocol_detected:
            self._remote._active = False  # Restore inactive state
        
        return result
    
    def on_bluetooth_disconnected(self) -> None:
        """Handle Bluetooth client disconnection.
        
        Reactivates local controller, recreates emulators for next connection,
        and requests a move from the current player (in case an engine player
        was just restored and it's their turn).
        """
        if self._remote:
            self._remote.stop()
            # Force recreate emulators for next connection (reset their state)
            self._remote._create_emulators(force=True)

        self._game_manager.set_remote_app_session(False)
        self.activate_local()
        
        # Request move from current player after restoring local control
        # This triggers the engine to compute if it's the engine's turn
        if self._local:
            self._local._request_current_player_move()
        
        log.info("[ControllerManager] Bluetooth disconnected, activated local")
    
    # =========================================================================
    # Cleanup
    # =========================================================================
    
    def cleanup(self) -> None:
        """Clean up all controllers."""
        log.info("[ControllerManager] Cleaning up...")
        
        if self._active:
            self._active.stop()
            self._active = None
        
        if self._local:
            # Stop player manager if present
            if hasattr(self._local, '_player_manager') and self._local._player_manager:
                self._local._player_manager.stop()
            # Clean up local controller (stops assistant manager)
            self._local.cleanup()
        
        self._local = None
        self._remote = None
        
        log.info("[ControllerManager] Cleanup complete")
