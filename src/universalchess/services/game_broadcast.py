"""
Game state broadcast service using Unix domain sockets.

Provides real-time game state updates from the main application to the web
application. Uses a Unix socket for secure, low-latency IPC.

Architecture:
    Main app (publisher) -> Unix socket -> Web app (subscriber) -> SSE -> Browsers

Message format (JSON):
    {
        "type": "game_state",
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "pgn": "1. e4 e5 2. Nf3 ...",
        "turn": "w",
        "move_number": 1,
        "last_move": "e2e4",
        "game_over": false,
        "result": null,
        "white": "Human",
        "black": "Stockfish",
        "timestamp": 1703577600.123
    }

Security:
    - Socket file permissions restrict access to the `pi` user
    - No network exposure (Unix socket only)
    - OS-level authentication via file ownership
"""

import json
import os
import socket
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Callable, List

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


# Socket path - in /run for volatile runtime data
SOCKET_DIR = Path("/run/universalchess")
SOCKET_PATH = SOCKET_DIR / "game.sock"
SETTINGS_SOCKET_PATH = SOCKET_DIR / "settings.sock"

# Fallback for development (when /run isn't available)
DEV_SOCKET_DIR = Path("/tmp/universalchess")
DEV_SOCKET_PATH = DEV_SOCKET_DIR / "game.sock"
DEV_SETTINGS_SOCKET_PATH = DEV_SOCKET_DIR / "settings.sock"


def get_socket_path() -> Path:
    """Get the appropriate socket path based on environment."""
    if SOCKET_DIR.exists() or os.access(SOCKET_DIR.parent, os.W_OK):
        return SOCKET_PATH
    return DEV_SOCKET_PATH


def get_settings_socket_path() -> Path:
    """Get the appropriate settings socket path based on environment."""
    if SOCKET_DIR.exists() or os.access(SOCKET_DIR.parent, os.W_OK):
        return SETTINGS_SOCKET_PATH
    return DEV_SETTINGS_SOCKET_PATH


@dataclass
class GameState:
    """Current game state for broadcasting."""
    # NOTE: `fen` is the piece-placement field only (8 ranks with `/`).
    # chessboard.js expects placement-only; full FEN (turn/castling/etc) can crash it.
    fen: str
    # Full FEN (optional). When not provided and `fen` looks like a full FEN,
    # __post_init__ will normalize `fen` to placement-only and store the full value here.
    fen_full: Optional[str] = None
    pgn: str = ""
    turn: str = "w"
    move_number: int = 1
    last_move: Optional[str] = None
    game_over: bool = False
    result: Optional[str] = None
    # How the game ended: 'checkmate', 'stalemate', 'resignation', 'time_forfeit', etc.
    termination: Optional[str] = None
    white: str = "White"
    black: str = "Black"
    timestamp: float = 0.0
    # Pending move: a move in progress on the physical board (from-to in UCI format)
    # Set when a piece is lifted (from square known) and optionally to square
    pending_move: Optional[str] = None
    
    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

        # Normalize FEN for web display: chessboard.js expects placement-only.
        # If a full FEN is provided in `fen`, split it and preserve the full value.
        if self.fen and " " in self.fen:
            if self.fen_full is None:
                self.fen_full = self.fen
            self.fen = self.fen.split(" ", 1)[0]
    
    def to_json(self) -> str:
        """Serialize to JSON string."""
        data = asdict(self)
        data["type"] = "game_state"
        return json.dumps(data)
    
    @classmethod
    def from_json(cls, json_str: str) -> "GameState":
        """Deserialize from JSON string."""
        data = json.loads(json_str)
        data.pop("type", None)  # Remove type field if present
        return cls(**data)


class GameBroadcaster:
    """
    Publisher side - sends game state updates via Unix socket.
    
    Used by the main application to broadcast moves to the web app.
    """
    
    def __init__(self):
        self._socket: Optional[socket.socket] = None
        self._connected = False
        self._lock = threading.Lock()
    
    def _ensure_socket_dir(self) -> None:
        """Ensure the socket directory exists with correct permissions."""
        socket_path = get_socket_path()
        socket_dir = socket_path.parent
        
        if not socket_dir.exists():
            socket_dir.mkdir(parents=True, mode=0o755)
            log.info(f"[GameBroadcaster] Created socket directory: {socket_dir}")
    
    def connect(self) -> bool:
        """Connect to the game broadcast socket.
        
        Returns:
            True if connected, False otherwise.
        """
        with self._lock:
            if self._connected:
                return True
            
            try:
                self._ensure_socket_dir()
                socket_path = get_socket_path()
                
                self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                # For DGRAM, we don't connect - we just send to the path
                self._connected = True
                log.info(f"[GameBroadcaster] Ready to broadcast to {socket_path}")
                return True
            except Exception as e:
                log.debug(f"[GameBroadcaster] Failed to initialize: {e}")
                self._connected = False
                return False
    
    def broadcast(self, state: GameState) -> bool:
        """Broadcast game state to subscribers.
        
        Args:
            state: GameState to broadcast.
            
        Returns:
            True if sent successfully, False otherwise.
        """
        if not self._connected:
            if not self.connect():
                return False
        
        try:
            socket_path = get_socket_path()
            message = state.to_json().encode("utf-8")
            self._socket.sendto(message, str(socket_path))
            log.debug(f"[GameBroadcaster] Sent: {state.fen[:20]}...")
            return True
        except FileNotFoundError:
            # Subscriber not listening yet - that's OK
            log.debug("[GameBroadcaster] No subscriber listening")
            return False
        except Exception as e:
            log.debug(f"[GameBroadcaster] Send failed: {e}")
            self._connected = False
            return False

    def broadcast_event(self, event_type: str, data: Optional[dict] = None) -> bool:
        """Broadcast a generic event to subscribers.
        
        Args:
            event_type: Type of event (e.g., 'settings_changed').
            data: Optional additional data payload.
            
        Returns:
            True if sent successfully, False otherwise.
        """
        if not self._connected:
            if not self.connect():
                return False
        
        try:
            socket_path = get_socket_path()
            message_dict = {"type": event_type}
            if data:
                message_dict.update(data)
            message = json.dumps(message_dict).encode("utf-8")
            self._socket.sendto(message, str(socket_path))
            log.debug(f"[GameBroadcaster] Sent event: {event_type}")
            return True
        except FileNotFoundError:
            log.debug("[GameBroadcaster] No subscriber listening")
            return False
        except Exception as e:
            log.debug(f"[GameBroadcaster] Send failed: {e}")
            self._connected = False
            return False
    
    def close(self) -> None:
        """Close the socket."""
        with self._lock:
            if self._socket:
                try:
                    self._socket.close()
                except Exception:
                    pass
                self._socket = None
            self._connected = False


class GameSubscriber:
    """
    Subscriber side - receives game state updates via Unix socket.
    
    Used by the web application to receive moves from the main app.
    Callbacks are invoked when new game state arrives.
    Also supports raw message callbacks for generic events.
    """
    
    def __init__(self):
        self._socket: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callbacks: List[Callable[[GameState], None]] = []
        self._raw_callbacks: List[Callable[[dict], None]] = []
        self._lock = threading.Lock()
        self._last_state: Optional[GameState] = None
        # Latest live Bluetooth status snapshot (type == 'bt_status'). Cached so
        # the web /status endpoint and a freshly-connected SSE client can render
        # immediately, mirroring _last_state for game state. The board owns the
        # authoritative engine; this is the web process's most-recent copy.
        self._last_bt_status: Optional[dict] = None
        # Latest live battery snapshot (type == 'battery_status'). Battery is read
        # from the board controller in the main process; this is the web process's
        # most-recent copy so GET /api/system/battery and a fresh SSE client can
        # render the indicator without waiting for the next board poll to change.
        self._last_battery_status: Optional[dict] = None
    
    def _ensure_socket(self) -> None:
        """Create and bind the Unix socket."""
        socket_path = get_socket_path()
        socket_dir = socket_path.parent
        
        # Ensure directory exists
        if not socket_dir.exists():
            socket_dir.mkdir(parents=True, mode=0o755)
        
        # Remove stale socket file
        if socket_path.exists():
            socket_path.unlink()
        
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._socket.bind(str(socket_path))
        self._socket.settimeout(1.0)  # Allow periodic check for shutdown
        
        # Set permissions - only owner can read/write
        os.chmod(socket_path, 0o600)
        
        log.info(f"[GameSubscriber] Listening on {socket_path}")
    
    def add_callback(self, callback: Callable[[GameState], None]) -> None:
        """Register a callback for game state updates.
        
        Args:
            callback: Function to call with GameState on each update.
        """
        with self._lock:
            self._callbacks.append(callback)
    
    def add_raw_callback(self, callback: Callable[[dict], None]) -> None:
        """Register a callback for raw message updates.
        
        Raw callbacks receive all messages as parsed JSON dicts,
        including both game state and generic events.
        
        Args:
            callback: Function to call with parsed message dict.
        """
        with self._lock:
            self._raw_callbacks.append(callback)
    
    def remove_callback(self, callback: Callable[[GameState], None]) -> None:
        """Unregister a callback.
        
        Args:
            callback: Previously registered callback function.
        """
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)
    
    def get_last_state(self) -> Optional[GameState]:
        """Get the most recent game state received.
        
        Returns:
            Last GameState or None if no state received yet.
        """
        return self._last_state

    def get_last_bt_status(self) -> Optional[dict]:
        """Get the most recent Bluetooth status snapshot received.

        Returns:
            Last ``bt_status`` payload dict, or None if none received yet (e.g.
            after a web-service restart before the board re-broadcasts).
        """
        return self._last_bt_status

    def get_last_battery_status(self) -> Optional[dict]:
        """Get the most recent battery status snapshot received.

        Returns:
            Last ``battery_status`` payload dict, or None if none received yet
            (e.g. after a web-service restart before the board re-broadcasts).
        """
        return self._last_battery_status
    
    def start(self) -> None:
        """Start the subscriber thread."""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._thread.start()
        log.info("[GameSubscriber] Started")
    
    def stop(self) -> None:
        """Stop the subscriber thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        
        # Clean up socket file
        socket_path = get_socket_path()
        if socket_path.exists():
            try:
                socket_path.unlink()
            except Exception:
                pass
        
        log.info("[GameSubscriber] Stopped")
    
    def _receive_loop(self) -> None:
        """Main receive loop - runs in background thread."""
        try:
            self._ensure_socket()
        except Exception as e:
            log.error(f"[GameSubscriber] Failed to create socket: {e}")
            self._running = False
            return
        
        while self._running:
            try:
                data, _ = self._socket.recvfrom(65536)
                message = data.decode("utf-8")
                parsed = json.loads(message)
                
                # Cache the latest Bluetooth status so the web can render it
                # immediately (HTTP /status, fresh SSE client) without waiting
                # for the next board change.
                if parsed.get("type") == "bt_status":
                    self._last_bt_status = parsed

                # Cache the latest battery snapshot for the same reason: the web
                # battery indicator (REST initial fetch, fresh SSE client) can
                # render at once without waiting for the next board change.
                if parsed.get("type") == "battery_status":
                    self._last_battery_status = parsed

                # Notify raw callbacks for all message types
                with self._lock:
                    raw_callbacks = list(self._raw_callbacks)
                for callback in raw_callbacks:
                    try:
                        callback(parsed)
                    except Exception as e:
                        log.error(f"[GameSubscriber] Raw callback error: {e}")
                
                # Only process as GameState if it's a game_state message
                if parsed.get("type") == "game_state":
                    state = GameState.from_json(message)
                    self._last_state = state
                    
                    # Notify game state callbacks
                    with self._lock:
                        callbacks = list(self._callbacks)
                    
                    for callback in callbacks:
                        try:
                            callback(state)
                        except Exception as e:
                            log.error(f"[GameSubscriber] Callback error: {e}")
                
            except socket.timeout:
                # Normal timeout, check if we should continue
                continue
            except Exception as e:
                if self._running:
                    log.error(f"[GameSubscriber] Receive error: {e}")
                    time.sleep(0.1)


# -----------------------------------------------------------------------------
# Singleton instances
# -----------------------------------------------------------------------------

_broadcaster: Optional[GameBroadcaster] = None
_subscriber: Optional[GameSubscriber] = None


def get_broadcaster() -> GameBroadcaster:
    """Get the singleton GameBroadcaster instance."""
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = GameBroadcaster()
    return _broadcaster


def get_subscriber() -> GameSubscriber:
    """Get the singleton GameSubscriber instance."""
    global _subscriber
    if _subscriber is None:
        _subscriber = GameSubscriber()
    return _subscriber


# Global pending move state - shared between broadcast functions
_pending_move: Optional[str] = None


def set_pending_move(pending_move: Optional[str]) -> None:
    """Set the pending move (piece lifted, awaiting destination).
    
    This updates the global pending move state which is included in
    all subsequent game state broadcasts.
    
    Args:
        pending_move: Move in progress in UCI format (e.g., 'e2' for from-only,
                      'e2e4' for from-to), or None to clear.
    """
    global _pending_move
    _pending_move = pending_move


def get_pending_move() -> Optional[str]:
    """Get the current pending move."""
    return _pending_move


def broadcast_game_state(
    fen: str,
    pgn: str = "",
    turn: str = "w",
    move_number: int = 1,
    last_move: Optional[str] = None,
    game_over: bool = False,
    result: Optional[str] = None,
    termination: Optional[str] = None,
    white: str = "White",
    black: str = "Black",
    pending_move: Optional[str] = None,
) -> bool:
    """Convenience function to broadcast game state.
    
    Args:
        fen: Current position in FEN notation.
        pgn: Current game PGN string.
        turn: Whose turn ('w' or 'b').
        move_number: Current move number.
        last_move: Last move in UCI notation.
        game_over: Whether the game has ended.
        result: Game result if over ('1-0', '0-1', '1/2-1/2').
        termination: How the game ended ('checkmate', 'resignation', etc.).
        white: White player name.
        black: Black player name.
        pending_move: Move in progress (from-to in UCI format, e.g., 'e2e4').
        
    Returns:
        True if broadcast succeeded, False otherwise.
    """
    # Use provided pending_move or fall back to global state
    effective_pending_move = pending_move if pending_move is not None else _pending_move
    
    state = GameState(
        fen=fen,
        pgn=pgn,
        turn=turn,
        move_number=move_number,
        last_move=last_move,
        game_over=game_over,
        result=result,
        termination=termination,
        white=white,
        black=black,
        pending_move=effective_pending_move,
    )
    return get_broadcaster().broadcast(state)


def broadcast_settings_changed() -> bool:
    """Broadcast a settings_changed event to subscribers.
    
    Called when settings are saved from the main process (menu).
    The web app will forward this to SSE clients so React can refetch.
    
    Returns:
        True if broadcast succeeded, False otherwise.
    """
    return get_broadcaster().broadcast_event("settings_changed")


# -----------------------------------------------------------------------------
# Settings notification (web app → main process)
# -----------------------------------------------------------------------------

class SettingsPublisher:
    """
    Publisher side for settings changes (web app → main process).
    
    Used by the web application to notify the main process that settings changed.
    """
    
    def __init__(self):
        self._socket: Optional[socket.socket] = None
        self._connected = False
        self._lock = threading.Lock()
    
    def _ensure_socket_dir(self) -> None:
        """Ensure the socket directory exists with correct permissions."""
        socket_path = get_settings_socket_path()
        socket_dir = socket_path.parent
        
        if not socket_dir.exists():
            socket_dir.mkdir(parents=True, mode=0o755)
            log.info(f"[SettingsPublisher] Created socket directory: {socket_dir}")
    
    def connect(self) -> bool:
        """Connect to the settings socket.
        
        Returns:
            True if connected, False otherwise.
        """
        with self._lock:
            if self._connected:
                return True
            
            try:
                self._ensure_socket_dir()
                self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                self._connected = True
                log.info(f"[SettingsPublisher] Ready to publish to {get_settings_socket_path()}")
                return True
            except Exception as e:
                log.debug(f"[SettingsPublisher] Failed to initialize: {e}")
                self._connected = False
                return False
    
    def notify_settings_changed(self) -> bool:
        """Notify the main process that settings have changed.
        
        Returns:
            True if sent successfully, False otherwise.
        """
        if not self._connected:
            if not self.connect():
                return False
        
        try:
            socket_path = get_settings_socket_path()
            message = json.dumps({"type": "settings_changed"}).encode("utf-8")
            self._socket.sendto(message, str(socket_path))
            log.debug("[SettingsPublisher] Sent settings_changed notification")
            return True
        except FileNotFoundError:
            log.debug("[SettingsPublisher] Main process not listening")
            return False
        except Exception as e:
            log.debug(f"[SettingsPublisher] Send failed: {e}")
            self._connected = False
            return False
    
    def send_board_command(self, command: str, params: Optional[dict] = None) -> bool:
        """Send a board-control command to the main process.

        Carries web-initiated actions that change the board's live game (e.g.
        setting up a predefined position or aborting the current game) over the
        same settings socket. The main process applies the command on its main
        thread; see SettingsSubscriber command callbacks.

        Args:
            command: Command name (e.g. 'setup_position', 'abort_game').
            params: Optional command parameters merged into the message.

        Returns:
            True if sent successfully, False otherwise.
        """
        if not self._connected:
            if not self.connect():
                return False

        try:
            socket_path = get_settings_socket_path()
            message_dict = {"type": "board_command", "command": command}
            if params:
                message_dict.update(params)
            message = json.dumps(message_dict).encode("utf-8")
            self._socket.sendto(message, str(socket_path))
            log.debug(f"[SettingsPublisher] Sent board_command: {command}")
            return True
        except FileNotFoundError:
            log.debug("[SettingsPublisher] Main process not listening")
            return False
        except Exception as e:
            log.debug(f"[SettingsPublisher] Send failed: {e}")
            self._connected = False
            return False

    def request_game_state(self) -> bool:
        """Ask the main process to re-broadcast the current game state.
        
        Sent when a Live-board client connects but the web app has no cached
        state (e.g. after a web-service restart). The game->web broadcast is
        one-way with no replay, so this pull lets the Live board fill at once
        instead of waiting for the next physical move.
        
        Returns:
            True if sent successfully, False otherwise.
        """
        if not self._connected:
            if not self.connect():
                return False
        
        try:
            socket_path = get_settings_socket_path()
            message = json.dumps({"type": "request_game_state"}).encode("utf-8")
            self._socket.sendto(message, str(socket_path))
            log.debug("[SettingsPublisher] Sent request_game_state")
            return True
        except FileNotFoundError:
            log.debug("[SettingsPublisher] Main process not listening")
            return False
        except Exception as e:
            log.debug(f"[SettingsPublisher] Send failed: {e}")
            self._connected = False
            return False

    def request_bt_status(self) -> bool:
        """Ask the main process to re-broadcast the current Bluetooth status.

        Sent when the web app needs the live Bluetooth status but has no cached
        snapshot (e.g. after a web-service restart, or when the Connectivity page
        mounts). The board -> web broadcast is one-way with no replay, so this
        pull triggers an immediate re-broadcast of the engine state instead of
        waiting for the next Bluetooth change.

        Returns:
            True if sent successfully, False otherwise.
        """
        if not self._connected:
            if not self.connect():
                return False

        try:
            socket_path = get_settings_socket_path()
            message = json.dumps({"type": "request_bt_status"}).encode("utf-8")
            self._socket.sendto(message, str(socket_path))
            log.debug("[SettingsPublisher] Sent request_bt_status")
            return True
        except FileNotFoundError:
            log.debug("[SettingsPublisher] Main process not listening")
            return False
        except Exception as e:
            log.debug(f"[SettingsPublisher] Send failed: {e}")
            self._connected = False
            return False

    def request_battery_status(self) -> bool:
        """Ask the main process to re-broadcast the current battery status.

        Sent when the web app needs the live battery level but has no cached
        snapshot (e.g. after a web-service restart, or when the battery indicator
        first mounts). The board -> web broadcast is one-way with no replay, so
        this pull triggers an immediate re-broadcast of the current battery state
        instead of waiting for the next board battery poll to change the level.

        Returns:
            True if sent successfully, False otherwise.
        """
        if not self._connected:
            if not self.connect():
                return False

        try:
            socket_path = get_settings_socket_path()
            message = json.dumps({"type": "request_battery_status"}).encode("utf-8")
            self._socket.sendto(message, str(socket_path))
            log.debug("[SettingsPublisher] Sent request_battery_status")
            return True
        except FileNotFoundError:
            log.debug("[SettingsPublisher] Main process not listening")
            return False
        except Exception as e:
            log.debug(f"[SettingsPublisher] Send failed: {e}")
            self._connected = False
            return False

    def close(self) -> None:
        """Close the socket."""
        with self._lock:
            if self._socket:
                try:
                    self._socket.close()
                except Exception:
                    pass
                self._socket = None
            self._connected = False


class SettingsSubscriber:
    """
    Subscriber side for settings changes (main process listens).
    
    Used by the main application to receive notifications when settings change.
    """
    
    def __init__(self):
        self._socket: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callbacks: List[Callable[[], None]] = []
        self._request_callbacks: List[Callable[[], None]] = []
        self._bt_status_request_callbacks: List[Callable[[], None]] = []
        self._battery_status_request_callbacks: List[Callable[[], None]] = []
        self._command_callbacks: List[Callable[[dict], None]] = []
        self._lock = threading.Lock()
    
    def _ensure_socket(self) -> None:
        """Create and bind the Unix socket."""
        socket_path = get_settings_socket_path()
        socket_dir = socket_path.parent
        
        if not socket_dir.exists():
            socket_dir.mkdir(parents=True, mode=0o755)
        
        if socket_path.exists():
            socket_path.unlink()
        
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._socket.bind(str(socket_path))
        self._socket.settimeout(1.0)
        
        os.chmod(socket_path, 0o600)
        log.info(f"[SettingsSubscriber] Listening on {socket_path}")
    
    def add_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback for settings change notifications.
        
        Args:
            callback: Function to call when settings change (no arguments).
        """
        with self._lock:
            self._callbacks.append(callback)
    
    def add_request_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback for game-state request notifications.
        
        Invoked when the web app asks the main process to re-broadcast the
        current game state (a Live-board client connected with no cached state).
        
        Args:
            callback: Function to call on a state request (no arguments).
        """
        with self._lock:
            self._request_callbacks.append(callback)

    def add_bt_status_request_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback for Bluetooth-status re-broadcast requests.

        Invoked when the web app asks the main process to re-broadcast the
        current live Bluetooth status (the web mounted/restarted with no cached
        snapshot). The handler asks the status engine to broadcast now.

        Args:
            callback: Function to call on a bt-status request (no arguments).
        """
        with self._lock:
            self._bt_status_request_callbacks.append(callback)

    def add_battery_status_request_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback for battery-status re-broadcast requests.

        Invoked when the web app asks the main process to re-broadcast the current
        battery status (the web mounted/restarted with no cached snapshot). The
        handler asks the board to broadcast the current battery state now.

        Args:
            callback: Function to call on a battery-status request (no arguments).
        """
        with self._lock:
            self._battery_status_request_callbacks.append(callback)

    def add_command_callback(self, callback: Callable[[dict], None]) -> None:
        """Register a callback for board-control commands.

        Invoked with the parsed message dict for each ``board_command`` message
        (carrying a ``command`` name and any parameters). Handlers must defer any
        display/game-lifecycle work to the main thread, as this runs on the
        subscriber thread.

        Args:
            callback: Function to call with the parsed command dict.
        """
        with self._lock:
            self._command_callbacks.append(callback)

    def start(self) -> None:
        """Start the subscriber thread."""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._thread.start()
        log.info("[SettingsSubscriber] Started")
    
    def stop(self) -> None:
        """Stop the subscriber thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        
        socket_path = get_settings_socket_path()
        if socket_path.exists():
            try:
                socket_path.unlink()
            except Exception:
                pass
        
        log.info("[SettingsSubscriber] Stopped")
    
    def _receive_loop(self) -> None:
        """Main receive loop - runs in background thread."""
        try:
            self._ensure_socket()
        except Exception as e:
            log.error(f"[SettingsSubscriber] Failed to create socket: {e}")
            self._running = False
            return
        
        while self._running:
            try:
                data, _ = self._socket.recvfrom(65536)
                message = data.decode("utf-8")
                parsed = json.loads(message)
                
                msg_type = parsed.get("type")
                if msg_type == "settings_changed":
                    log.info("[SettingsSubscriber] Received settings_changed, notifying callbacks")
                    with self._lock:
                        callbacks = list(self._callbacks)
                    
                    for callback in callbacks:
                        try:
                            callback()
                        except Exception as e:
                            log.error(f"[SettingsSubscriber] Callback error: {e}")
                elif msg_type == "request_game_state":
                    log.debug("[SettingsSubscriber] Received request_game_state, notifying callbacks")
                    with self._lock:
                        request_callbacks = list(self._request_callbacks)
                    
                    for callback in request_callbacks:
                        try:
                            callback()
                        except Exception as e:
                            log.error(f"[SettingsSubscriber] Request callback error: {e}")
                elif msg_type == "request_bt_status":
                    log.debug("[SettingsSubscriber] Received request_bt_status, notifying callbacks")
                    with self._lock:
                        bt_status_callbacks = list(self._bt_status_request_callbacks)

                    for callback in bt_status_callbacks:
                        try:
                            callback()
                        except Exception as e:
                            log.error(f"[SettingsSubscriber] BT status request callback error: {e}")
                elif msg_type == "request_battery_status":
                    log.debug("[SettingsSubscriber] Received request_battery_status, notifying callbacks")
                    with self._lock:
                        battery_status_callbacks = list(self._battery_status_request_callbacks)

                    for callback in battery_status_callbacks:
                        try:
                            callback()
                        except Exception as e:
                            log.error(f"[SettingsSubscriber] Battery status request callback error: {e}")
                elif msg_type == "board_command":
                    log.info(f"[SettingsSubscriber] Received board_command: {parsed.get('command')}")
                    with self._lock:
                        command_callbacks = list(self._command_callbacks)

                    for callback in command_callbacks:
                        try:
                            callback(parsed)
                        except Exception as e:
                            log.error(f"[SettingsSubscriber] Command callback error: {e}")
                
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    log.error(f"[SettingsSubscriber] Receive error: {e}")
                    time.sleep(0.1)


# Singleton instances for settings notification
_settings_publisher: Optional[SettingsPublisher] = None
_settings_subscriber: Optional[SettingsSubscriber] = None


def get_settings_publisher() -> SettingsPublisher:
    """Get the singleton SettingsPublisher instance."""
    global _settings_publisher
    if _settings_publisher is None:
        _settings_publisher = SettingsPublisher()
    return _settings_publisher


def get_settings_subscriber() -> SettingsSubscriber:
    """Get the singleton SettingsSubscriber instance."""
    global _settings_subscriber
    if _settings_subscriber is None:
        _settings_subscriber = SettingsSubscriber()
    return _settings_subscriber


def notify_main_process_settings_changed() -> bool:
    """Notify the main process that settings have changed.
    
    Called from the web app when settings are saved.
    
    Returns:
        True if notification was sent, False otherwise.
    """
    return get_settings_publisher().notify_settings_changed()


def send_board_command(command: str, params: Optional[dict] = None) -> bool:
    """Send a board-control command to the main process.

    Called from the web app to set up a position or abort the running game on
    the board. The main process applies it on its main thread.

    Args:
        command: Command name (e.g. 'setup_position', 'abort_game').
        params: Optional command parameters.

    Returns:
        True if the command was sent, False otherwise.
    """
    return get_settings_publisher().send_board_command(command, params)


def request_game_state_broadcast() -> bool:
    """Ask the main process to re-broadcast the current game state.
    
    Called from the web app when a Live-board client connects without a cached
    state, so the board fills immediately rather than waiting for the next move.
    
    Returns:
        True if the request was sent, False otherwise.
    """
    return get_settings_publisher().request_game_state()


def request_bt_status_broadcast() -> bool:
    """Ask the main process to re-broadcast the current Bluetooth status.

    Called from the web app when the Connectivity page needs the live status but
    has no cached snapshot, so the board re-broadcasts immediately rather than
    the web waiting for the next Bluetooth change.

    Returns:
        True if the request was sent, False otherwise.
    """
    return get_settings_publisher().request_bt_status()


def request_battery_status_broadcast() -> bool:
    """Ask the main process to re-broadcast the current battery status.

    Called from the web app when the battery indicator needs the live level but
    has no cached snapshot, so the board re-broadcasts immediately rather than
    the web waiting for the next board battery poll to change the level.

    Returns:
        True if the request was sent, False otherwise.
    """
    return get_settings_publisher().request_battery_status()


def broadcast_battery_status(
    battery_level: Optional[int],
    battery_percent: Optional[int],
    charger_connected: bool,
) -> bool:
    """Broadcast the current battery status to the web (board -> web).

    Called from the main process whenever the battery level or charger state
    changes (and on demand for a re-broadcast request). The web app caches the
    snapshot and forwards it to SSE clients so the navbar indicator updates live.

    Args:
        battery_level: Battery level on the 0-20 scale, or None if unknown.
        battery_percent: Battery level as a percentage (0-100), or None if unknown.
        charger_connected: Whether the charger is connected.

    Returns:
        True if the broadcast was sent, False otherwise.
    """
    return get_broadcaster().broadcast_event(
        "battery_status",
        {
            "battery_level": battery_level,
            "battery_percent": battery_percent,
            "charger_connected": charger_connected,
        },
    )

