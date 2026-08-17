# Lichess Player
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# A player that connects to Lichess for online games. Moves come from
# the Lichess server (either from a remote human opponent or Lichess AI).
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Callable

import chess

from universalchess.board import board, centaur
from universalchess.board.logging import log
from ..base import Player, PlayerConfig, PlayerState, PlayerType


class LichessGameMode(Enum):
    """Lichess game modes."""
    NEW = auto()        # Seek a new game with specified parameters
    ONGOING = auto()    # Resume an ongoing game by ID
    CHALLENGE = auto()  # Accept or wait for a challenge
    ATTACH = auto()     # Watch for an ongoing game; do not post a seek


@dataclass
class LichessPlayerConfig(PlayerConfig):
    """Configuration for Lichess player.
    
    Attributes:
        name: Display name.
        color: The color this player plays (set after game starts).
        mode: Game mode (NEW, ONGOING, CHALLENGE, or ATTACH).
        time_minutes: Time control in minutes (for NEW mode).
        increment_seconds: Increment in seconds (for NEW mode).
        rated: Whether game is rated (for NEW mode).
        color_preference: Preferred color when seeking ('white', 'black', 'random').
        rating_range: Rating range for matchmaking (for NEW mode).
        game_id: Game ID to resume (for ONGOING mode).
        challenge_id: Challenge ID to accept (for CHALLENGE mode).
        challenge_direction: 'in' for incoming, 'out' for outgoing.
        account_id: Lichess credential id this slot plays as (``org:alice``).
            Empty uses the default (first) credential for back-compat; the
            token, host, and rating range are resolved at start().
    """
    mode: LichessGameMode = LichessGameMode.NEW
    time_minutes: int = 10
    increment_seconds: int = 5
    rated: bool = False
    color_preference: str = 'random'
    rating_range: str = ''
    game_id: str = ''
    challenge_id: str = ''
    challenge_direction: str = 'in'
    account_id: str = ''


def ongoing_game_id(game: dict) -> str:
    """Id of one ``GET /api/account/playing`` nowPlaying row.

    Lichess JSON uses ``gameId``. A snake_case converter or a truncated object
    may expose ``game_id`` or ``id`` instead. Empty means the row cannot be
    streamed.
    """
    if not game:
        return ""
    return str(game.get("gameId") or game.get("game_id") or game.get("id") or "")


def server_move_list_delta(previous: str, current: str):
    """Compare two Lichess ``moves`` strings (space-separated UCI).

    Returns ``(removed_count, added_ucis)``. A takeback is a shorter prefix;
    a new ply appends; a different continuation removes then adds.
    """
    prev = previous.split() if previous else []
    curr = current.split() if current else []
    i = 0
    limit = min(len(prev), len(curr))
    while i < limit and prev[i].lower() == curr[i].lower():
        i += 1
    return len(prev) - i, curr[i:]


class LichessPlayer(Player):
    """A player that connects to Lichess for online games.
    
    This player represents the remote opponent. Moves come from the
    Lichess server via HTTP streaming.
    
    Move Flow (when it's this player's turn):
    1. Stream receives move from server, stores as pending_move
    2. Notifies via lichess_move_callback for LED display
    3. on_piece_event() forms move from lift/place
    4. If move matches pending_move - submits via move_callback
    5. If move doesn't match - board needs correction, no submission
    
    Thread Model:
    - start() authenticates and begins seek/stream
    - Event and poll threads attach a seek-take even if seek() never returns
    - Incoming challenges are offered on a worker so the event loop stays free
    - Stream thread receives remote moves and stores them
    - Piece events validate execution and submit
    """
    
    def __init__(self, config: Optional[LichessPlayerConfig] = None):
        """Initialize the Lichess player.
        
        Args:
            config: Lichess configuration. If None, uses defaults.
        """
        super().__init__(config or LichessPlayerConfig())
        self._lichess_config: LichessPlayerConfig = self._config
        
        # Lichess API client (berserk) and the connection that owns its session,
        # kept so stop() can abort streams berserk holds out of reach.
        self._client = None
        self._connection = None
        self._token = None
        # Rating range resolved from the bound account (used for matchmaking when
        # the config does not override it).
        self._account_range: str = ''
        
        # Game state
        self._game_id: Optional[str] = None
        self._player_is_white: Optional[bool] = None  # Is the LOCAL human white?
        self._current_turn_is_white: bool = True
        
        # Player info
        self._username: str = ''
        self._white_player: str = ''
        self._black_player: str = ''
        self._white_rating: str = ''
        self._black_rating: str = ''
        
        # Clock state (in seconds)
        self._white_time: int = 0
        self._black_time: int = 0
        
        # Move tracking for remote move detection
        self._remote_moves: str = ''
        self._last_processed_moves: str = ''
        
        # Pending move from server (for validation)
        self._pending_move: Optional[chess.Move] = None
        
        # Threading
        self._should_stop = threading.Event()
        self._stream_thread: Optional[threading.Thread] = None
        self._seek_thread: Optional[threading.Thread] = None
        self._event_thread: Optional[threading.Thread] = None
        self._match_thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()
        
        # Board orientation
        self._board_flip: bool = False
        
        # Callbacks for game events
        self._on_game_connected: Optional[Callable] = None
        self._clock_callback: Optional[Callable[[int, int], None]] = None
        self._game_info_callback: Optional[Callable[[str, str, str, str], None]] = None
        self._game_over_callback: Optional[Callable[[str, str, Optional[str]], None]] = None
        self._takeback_offer_callback: Optional[Callable[[Callable, Callable], None]] = None
        self._draw_offer_callback: Optional[Callable[[Callable, Callable], None]] = None
        self._challenge_offer_callback = None
        self._challenge_prompt_lock = threading.Lock()
        self._challenge_prompt_open = False
        self._remote_takeback_callback = None
        self._info_message_callback: Optional[Callable[[str], None]] = None
    
    @property
    def player_type(self) -> PlayerType:
        """Moves arrive from outside; the local human transcribes them."""
        return PlayerType.REMOTE

    @property
    def requires_rebuild_on_new_game(self) -> bool:
        """Still attached to the remote game after a local board-reset."""
        return True
    
    @property
    def pending_move(self) -> Optional[chess.Move]:
        """The move from Lichess server waiting to be executed on the board."""
        return self._pending_move
    
    @property
    def board_flip(self) -> bool:
        """Whether board display should be flipped (True if local player is black)."""
        return self._board_flip
    
    @property
    def game_id(self) -> Optional[str]:
        """Current Lichess game ID."""
        return self._game_id
    
    @property
    def white_player(self) -> str:
        """White player's username."""
        return self._white_player
    
    @property
    def black_player(self) -> str:
        """Black player's username."""
        return self._black_player
    
    @property
    def white_rating(self) -> str:
        """White player's rating."""
        return self._white_rating
    
    @property
    def black_rating(self) -> str:
        """Black player's rating."""
        return self._black_rating
    
    def set_on_game_connected(self, callback: Callable) -> None:
        """Set callback for when game is connected and ready."""
        self._on_game_connected = callback
    
    def set_clock_callback(self, callback: Callable[[int, int], None]) -> None:
        """Set callback for clock updates (white_time, black_time in seconds)."""
        self._clock_callback = callback
    
    def set_game_info_callback(self, callback: Callable[[str, str, str, str], None]) -> None:
        """Set callback for game info (white_player, white_rating, black_player, black_rating)."""
        self._game_info_callback = callback

    def bind_remote_session(
        self,
        *,
        clock_callback: Optional[Callable[[int, int], None]] = None,
        game_info_callback: Optional[Callable[[str, str, str, str], None]] = None,
    ) -> None:
        """Wire ProtocolManager clock and name updates from the Lichess stream."""
        if clock_callback is not None:
            self.set_clock_callback(clock_callback)
        if game_info_callback is not None:
            self.set_game_info_callback(game_info_callback)
    
    def set_game_over_callback(self, callback: Callable[[str, str, Optional[str]], None]) -> None:
        """Set callback for game over (result, termination_type, winner).
        
        Args:
            callback: Called when game ends with (result, termination, winner).
                result: "1-0", "0-1", or "1/2-1/2"
                termination: "mate", "resign", "timeout", "draw", etc.
                winner: "white", "black", or None for draw
        """
        self._game_over_callback = callback
    
    def set_takeback_offer_callback(self, callback: Callable[[Callable, Callable], None]) -> None:
        """Set callback for takeback offer from opponent.
        
        Args:
            callback: Called with (accept_fn, decline_fn) when opponent offers takeback.
                Caller should show menu and call accept_fn() or decline_fn().
        """
        self._takeback_offer_callback = callback
    
    def set_draw_offer_callback(self, callback: Callable[[Callable, Callable], None]) -> None:
        """Set callback for draw offer from opponent.
        
        Args:
            callback: Called with (accept_fn, decline_fn) when opponent offers draw.
                Caller should show menu and call accept_fn() or decline_fn().
        """
        self._draw_offer_callback = callback

    def set_challenge_offer_callback(self, callback) -> None:
        """Set callback for an incoming challenge while seeking.

        A seek is the board's terms. A challenge is the opponent's. The
        callback is (offer, accept_fn, decline_fn); it must show those terms
        and call one of the functions. No callback leaves the challenge pending.
        """
        self._challenge_offer_callback = callback

    def set_remote_takeback_callback(self, callback) -> None:
        """Called with remaining half-move count when the server shortens the line.

        Lichess takeback (and a recapture line) streams a shorter ``moves``
        string. The game must pop to that count; this player cannot.
        """
        self._remote_takeback_callback = callback
    
    def set_info_message_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback for informational messages to display.
        
        Args:
            callback: Called with message string to show on display.
        """
        self._info_message_callback = callback
    
    def _resolve_account(self):
        """Resolve the bound account's token and rating range for this slot.

        Resolution order (each a deliberate fallback):
        1. the account bound to this slot (``config.account_id``);
        2. the default (first) account, used when the slot is unbound or its
           bound account was deleted -- keeps a saved setup playable rather than
           failing on a stale id;
        3. the legacy single ``[lichess]`` token (via ``centaur.get_lichess_api``)
           for a board that has a token but no migrated accounts yet.

        Returns:
            A ``(token, rating_range)`` tuple; ``rating_range`` is '' when the
            account has none (matchmaking then uses the config/global default).
        """
        from .accounts import (
            default_lichess_credential,
            get_lichess_credential,
            host_id_of,
        )
        from .hosts import DEFAULT_HOST_ID

        if self._lichess_config.account_id:
            account = get_lichess_credential(self._lichess_config.account_id)
            if account is None:
                log.warning(
                    f"[LichessPlayer] Bound account '{self._lichess_config.account_id}' "
                    "not found"
                )
                self._host_id = DEFAULT_HOST_ID
                return "", ""
        else:
            account = default_lichess_credential()
        if account is not None:
            self._host_id = host_id_of(account)
            return account.get("api_token", ""), account.get("range", "")
        self._host_id = DEFAULT_HOST_ID
        return centaur.get_lichess_api(), ""

    def start(self) -> bool:
        """Start the Lichess connection and game.
        
        Authenticates with Lichess API, then starts the appropriate
        game flow based on config.mode (NEW, ONGOING, CHALLENGE, or ATTACH).
        
        Returns:
            True if connection started successfully, False on error.
        """
        log.info("[LichessPlayer] Starting Lichess player")
        if self._should_stop.is_set():
            return False
        self._set_state(PlayerState.INITIALIZING)
        self._report_status("Connecting to Lichess...")
        
        # Resolve the API token (and rating range) from the bound account.
        self._token, self._account_range = self._resolve_account()
        if not self._token or self._token == "tokenhere":  # noqa: S105 # nosec B105 - placeholder sentinel, not a secret
            log.error("[LichessPlayer] No valid API token configured")
            self._set_state(PlayerState.ERROR, "No API token configured")
            return False
        
        # Initialize berserk client
        try:
            from .match import create_lichess_connection
            # Published from a local: stop() runs on the key thread and clears
            # both attributes, so reading self._connection back here could find
            # the None it just wrote and fail the start as an API error rather
            # than the cancel it is.
            connection = create_lichess_connection(
                self._token, host_id=getattr(self, "_host_id", "org")
            )
            self._connection = connection
            self._client = connection.client
        except ImportError:
            log.error("[LichessPlayer] berserk library not installed")
            self._set_state(PlayerState.ERROR, "berserk not installed")
            return False
        except Exception as e:
            log.error(f"[LichessPlayer] Failed to create berserk client: {e}")
            self._set_state(PlayerState.ERROR, "API client error")
            return False

        # BACK can run on the key thread while this start() is still on the
        # main thread. If stop() already ran, do not authenticate or post a seek.
        if self._abandon_start() or self._client is None:
            return False
        
        # Authenticate and get user info
        self._report_status("Authenticating...")
        try:
            user_info = self._client.account.get()
            self._username = user_info.get('username', '')
            # Update config name to use Lichess username
            self._config.name = self._username if self._username else "Lichess"
            log.info(f"[LichessPlayer] Authenticated as: {self._username}")
        except Exception as e:
            if self._should_stop.is_set():
                self._close_http_session()
                return False
            log.error(f"[LichessPlayer] Authentication failed: {e}")
            self._set_state(PlayerState.ERROR, "API token invalid")
            return False

        if self._abandon_start():
            return False
        
        # Start appropriate game flow
        if self._lichess_config.mode == LichessGameMode.NEW:
            return self._start_new_game()
        elif self._lichess_config.mode == LichessGameMode.ONGOING:
            return self._start_ongoing_game()
        elif self._lichess_config.mode == LichessGameMode.CHALLENGE:
            return self._start_challenge()
        elif self._lichess_config.mode == LichessGameMode.ATTACH:
            return self._attach_without_seek()
        
        return False
    
    def stop(self) -> None:
        """Stop the Lichess connection and cleanup.

        Aborts the in-flight Board API streams before joining their threads.
        Lichess keeps a lobby seek alive until the streamed ``board.seek`` POST
        connection closes, and those threads are blocked reading it, so joining
        first would simply time out and leave the seek listed.
        """
        log.info("[LichessPlayer] Stopping Lichess player")
        self._should_stop.set()
        self._close_http_session()

        # Wait for threads to finish
        if self._stream_thread and self._stream_thread.is_alive():
            self._stream_thread.join(timeout=2.0)
        if self._seek_thread and self._seek_thread.is_alive():
            self._seek_thread.join(timeout=2.0)
        if self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=2.0)
        if self._match_thread and self._match_thread.is_alive():
            self._match_thread.join(timeout=2.0)

        self._set_state(PlayerState.STOPPED)
        log.info("[LichessPlayer] Lichess player stopped")

    def _close_http_session(self) -> None:
        """Abort in-flight Board API streams (seek, events, game), then close.

        Aborting is what cancels the seek. ``Session.close()`` alone only clears
        idle pooled connections, so the streamed ``POST /api/board/seek`` -- which
        is checked out and blocked in a read on the seek thread -- survived it and
        Lichess kept listing the seek after BACK.
        """
        connection = self._connection
        self._connection = None
        self._client = None
        if connection is None:
            return
        aborted = connection.close()
        if aborted:
            log.info(f"[LichessPlayer] Aborted {aborted} in-flight Lichess stream(s)")

    def _abandon_start(self) -> bool:
        """True when stop() ran during start(); drop any client created after.

        stop() on the key thread can beat client creation, so there is no
        session to close. Returning here is what prevents a seek after BACK.
        """
        if not self._should_stop.is_set():
            return False
        self._close_http_session()
        return True
    
    def _do_request_move(self, board: chess.Board) -> None:
        """Request a move from this player.
        
        If a pending move exists (received from server), displays LEDs.
        Resets piece event tracking for the new turn.
        
        Args:
            board: Current chess position.
        """
        self._lifted_squares = []
        
        if self._pending_move:
            log.info(f"[LichessPlayer] Displaying pending move: {self._pending_move.uci()}")
            if self._pending_move_callback:
                self._pending_move_callback(self._pending_move)
        else:
            log.debug("[LichessPlayer] request_move called - waiting for server move")
    
    def _on_move_formed(self, move: chess.Move) -> None:
        """Validate formed move matches server's move.
        
        Only submits if the move matches the pending move from server.
        If it doesn't match, the board state is wrong and needs correction.
        
        Handles destination-only moves (from_square == to_square) which indicate
        a missed lift event. If the destination matches the pending move's to_square,
        we trust the move was executed correctly.
        
        Args:
            move: The formed move from piece events.
        """
        log.debug(f"[LichessPlayer] Move formed: {move.uci()}")
        
        if self._pending_move is None:
            # No move from server yet - user moved pieces prematurely
            log.warning(f"[LichessPlayer] Move formed but no pending move from server")
            self._report_error("move_mismatch")
            return
        
        # Handle destination-only move (missed lift event)
        # If from_square == to_square and matches pending move's to_square, trust it
        if move.from_square == move.to_square:
            if move.to_square == self._pending_move.to_square:
                log.warning(f"[LichessPlayer] MISSED LIFT RECOVERY: Destination-only move to {chess.square_name(move.to_square)} matches pending move's destination")
                if self._move_callback:
                    self._move_callback(self._pending_move)
                return
            else:
                log.warning(f"[LichessPlayer] Destination-only move {chess.square_name(move.to_square)} does not match pending {self._pending_move.uci()}")
                self._report_error("move_mismatch")
                return
        
        # Check if move matches (ignoring promotion - use pending move's promotion)
        if move.from_square == self._pending_move.from_square and \
           move.to_square == self._pending_move.to_square:
            # Match! Submit the pending move (includes promotion if any)
            log.info(f"[LichessPlayer] Move matches server: {self._pending_move.uci()}")
            if self._move_callback:
                self._move_callback(self._pending_move)
            else:
                log.warning("[LichessPlayer] No move callback set, cannot submit move")
        else:
            # Doesn't match - board needs correction
            log.warning(f"[LichessPlayer] Move {move.uci()} does not match server {self._pending_move.uci()}")
            self._report_error("move_mismatch")
    
    def on_move_made(self, move: chess.Move, board: chess.Board) -> None:
        """Notification that a move was made on the board.
        
        Clears pending state. For the local player's moves, sends to Lichess.
        
        Args:
            move: The move that was made.
            board: Board state after the move.
        """
        # Clear pending state
        self._pending_move = None
        self._lifted_squares = []
        
        # If this was the remote player's move (this player's move), don't send to server
        # The move came FROM the server, so we don't echo it back
        # on_move_made is called for ALL moves, so we need to check whose move it was
        # The board.turn is now the NEXT player's turn, so if it's our color, the last move was opponent's
        if board.turn == self._color:
            # Last move was opponent's (local player's) - they made a move, send it to server
            log.info(f"[LichessPlayer] Sending local player's move to server: {move.uci()}")
            self._send_move_to_server(move)
        else:
            # Last move was ours (remote player's) - came from server, don't echo
            log.debug(f"[LichessPlayer] Our move executed: {move.uci()}")
    
    def on_takeback(self, board: chess.Board) -> None:
        """Notification that a takeback occurred.
        
        Clear any pending move since the position has changed and the
        server's move is no longer valid.
        """
        if self._pending_move is not None:
            log.info(f"[LichessPlayer] Takeback - clearing pending move {self._pending_move.uci()}")
            self._pending_move = None
            self._lifted_squares = []
        else:
            log.debug("[LichessPlayer] Takeback - no pending move to clear")
    
    def _send_move_to_server(self, move: chess.Move) -> None:
        """Send a move to the Lichess server.
        
        Args:
            move: The move to send.
        """
        if self._state != PlayerState.READY:
            log.warning(f"[LichessPlayer] Cannot send move - state is {self._state}")
            return
        
        move_uci = move.uci()
        
        retries = 3
        for attempt in range(retries):
            try:
                self._client.board.make_move(self._game_id, move_uci)
                log.debug("[LichessPlayer] Move sent successfully")
                return
            except Exception as e:
                log.warning(f"[LichessPlayer] Move attempt {attempt + 1} failed: {e}")
                if attempt < retries - 1:
                    time.sleep(0.5)
        
        log.error(f"[LichessPlayer] Failed to send move after {retries} attempts")
    
    def on_new_game(self) -> None:
        """Board reset: leave the remote game so a rebuild can seek a new one.

        Abort if still allowed (early in the game); otherwise resign. ``stop()``
        only ends the stream, which would leave the opponent waiting in the
        abandoned Lichess game.
        """
        log.info("[LichessPlayer] New game notification - leaving remote game")
        self._pending_move = None
        self._lifted_squares = []
        self.leave_remote_game()

    def leave_remote_game(self) -> None:
        """Abort the Lichess game, or resign when abort is no longer legal."""
        if not self._game_id or not self._client:
            return
        try:
            self._client.board.abort_game(self._game_id)
            log.info("[LichessPlayer] Aborted remote game")
            return
        except Exception as abort_error:
            log.info(f"[LichessPlayer] Abort not available ({abort_error}); resigning")
        try:
            self._client.board.resign_game(self._game_id)
            log.info("[LichessPlayer] Resigned remote game")
        except Exception as e:
            log.warning(f"[LichessPlayer] Could not leave remote game: {e}")
    
    def on_resign(self, color: chess.Color) -> None:
        """Resign the current game."""
        if not self._game_id or not self._client:
            log.warning("[LichessPlayer] Cannot resign - no active game")
            return
        
        if self._state != PlayerState.READY:
            log.info(f"[LichessPlayer] Cannot resign - state is {self._state}")
            return
        
        log.info("[LichessPlayer] Resigning game")
        try:
            self._client.board.resign_game(self._game_id)
        except Exception as e:
            log.error(f"[LichessPlayer] Failed to resign: {e}")
    
    def on_draw_offer(self) -> None:
        """Offer a draw to the opponent."""
        if not self._game_id or not self._client:
            log.warning("[LichessPlayer] Cannot offer draw - no active game")
            return
        
        log.info("[LichessPlayer] Offering draw")
        try:
            self._client.board.offer_draw(self._game_id)
        except Exception as e:
            log.error(f"[LichessPlayer] Failed to offer draw: {e}")
    
    def accept_draw(self) -> None:
        """Accept a draw offer from opponent."""
        if not self._game_id or not self._client:
            log.warning("[LichessPlayer] Cannot accept draw - no active game")
            return
        
        log.info("[LichessPlayer] Accepting draw")
        try:
            # In Lichess API, offering draw while opponent has offered = accept
            self._client.board.offer_draw(self._game_id)
        except Exception as e:
            log.error(f"[LichessPlayer] Failed to accept draw: {e}")
    
    def decline_draw(self) -> None:
        """Decline a draw offer from opponent."""
        if not self._game_id or not self._client:
            log.warning("[LichessPlayer] Cannot decline draw - no active game")
            return
        
        log.info("[LichessPlayer] Declining draw")
        try:
            self._client.board.decline_draw(self._game_id)
        except Exception as e:
            log.error(f"[LichessPlayer] Failed to decline draw: {e}")
    
    def accept_takeback(self) -> None:
        """Accept a takeback offer from opponent."""
        if not self._game_id or not self._client:
            log.warning("[LichessPlayer] Cannot accept takeback - no active game")
            return
        
        log.info("[LichessPlayer] Accepting takeback")
        try:
            # Accept takeback via Lichess API
            self._client.board.handle_takeback_offer(self._game_id, accept=True)
        except Exception as e:
            log.error(f"[LichessPlayer] Failed to accept takeback: {e}")
    
    def decline_takeback(self) -> None:
        """Decline a takeback offer from opponent."""
        if not self._game_id or not self._client:
            log.warning("[LichessPlayer] Cannot decline takeback - no active game")
            return
        
        log.info("[LichessPlayer] Declining takeback")
        try:
            self._client.board.handle_takeback_offer(self._game_id, accept=False)
            # Send a polite message
            try:
                self._client.board.post_message(
                    self._game_id,
                    "Sorry, this external board doesn't handle takebacks well",
                    spectator=False
                )
            except Exception:  # noqa: S110 # nosec B110 - the courtesy chat message is best-effort; failing to send it must not abort declining the takeback
                pass  # Message is optional
        except Exception as e:
            log.error(f"[LichessPlayer] Failed to decline takeback: {e}")
    
    @property
    def player_is_white(self) -> Optional[bool]:
        """Whether the local Lichess account sits White, once the stream has said.

        None until ``_extract_player_info`` runs. Used to remap Human/Lichess
        slots and flip the board without reading a private attribute.
        """
        return self._player_is_white

    def abort_game(self) -> None:
        """Abort the current game (only valid in first few moves).

        Does not require READY: the stream can accept while the started splash
        is still up, and READY is set only after ``on_game_connected`` returns.
        """
        if not self._game_id or not self._client:
            log.warning("[LichessPlayer] Cannot abort - no active game")
            return
        
        log.info("[LichessPlayer] Aborting game")
        try:
            self._client.board.abort_game(self._game_id)
        except Exception as e:
            log.error(f"[LichessPlayer] Failed to abort: {e}")

    def abort_remote_game(self) -> None:
        """In-game abort menu: abort only, no resign fallback."""
        self.abort_game()
    
    def supports_takeback(self) -> bool:
        """Lichess doesn't support takeback from external boards."""
        return False
    
    def get_info(self) -> dict:
        """Get information about this player."""
        info = super().get_info()
        info.update({
            'game_id': self._game_id,
            'username': self._username,
            'white_player': self._white_player,
            'black_player': self._black_player,
            'white_rating': self._white_rating,
            'black_rating': self._black_rating,
            'description': 'Lichess online game',
        })
        return info
    
    # =========================================================================
    # Game Flow Methods - Private
    # =========================================================================
    
    def _listen_for_match(self) -> None:
        """Start the Board API event stream and ongoing-game poller.

        Does not post a seek. NEW adds a seek thread on top of this; ATTACH
        uses only these listeners so a boot resume can reconnect without
        listing a new game in the Lichess lobby.
        """
        self._event_thread = threading.Thread(
            target=self._event_stream_thread,
            name="lichess-events",
            daemon=True,
        )
        self._match_thread = threading.Thread(
            target=self._match_poll_thread,
            name="lichess-match-poll",
            daemon=True,
        )
        self._event_thread.start()
        self._match_thread.start()

    def _start_new_game(self) -> bool:
        """Start seeking a new game.

        Three threads run until a game id is known:

        * the Board API event stream (``gameStart``; incoming challenges are
          offered, not accepted, so the Human can refuse the opponent's terms);
        * ``board.seek``, which holds an HTTP stream open for the lobby hook;
        * a poller on ``games.get_ongoing``, because seek() often does not
          return after someone takes the hook (Lichess keeps sending
          keep-alives) and ``gameStart`` is a one-shot that can be missed.
        """
        if self._should_stop.is_set():
            return False
        log.info(f"[LichessPlayer] Seeking: {self._lichess_config.time_minutes}+{self._lichess_config.increment_seconds}")
        self._report_status("Finding opponent...")
        self._listen_for_match()
        self._seek_thread = threading.Thread(
            target=self._seek_game_thread,
            name="lichess-seek",
            daemon=True,
        )
        self._seek_thread.start()
        return True

    def _attach_without_seek(self) -> bool:
        """Reconnect to an ongoing game. Does not post ``board.seek``."""
        log.info("[LichessPlayer] Watching for an ongoing game (no seek)")
        self._report_status("Connecting...")
        self._listen_for_match()
        return True

    def _begin_game(self, game_id: str) -> bool:
        """Attach ``game_id`` and start the game stream at most once."""
        if not game_id:
            return False
        with self._state_lock:
            if self._game_id or self._should_stop.is_set():
                return False
            self._game_id = game_id
        log.info(f"[LichessPlayer] Found game: {game_id}")
        self._start_game_stream()
        return True

    def _try_attach_ongoing_game(self) -> bool:
        """Attach the first nowPlaying game, if any."""
        if self._game_id or not self._client:
            return bool(self._game_id)
        try:
            ongoing = self._client.games.get_ongoing(30)
        except Exception as e:
            log.warning(f"[LichessPlayer] Error checking ongoing games: {e}")
            return False
        for game in ongoing or []:
            game_id = ongoing_game_id(game)
            if game_id and self._begin_game(game_id):
                return True
        return False

    def _match_poll_thread(self):
        """Poll ongoing games until attached. Does not wait for seek()."""
        while not self._should_stop.is_set() and not self._game_id:
            if self._try_attach_ongoing_game():
                return
            time.sleep(0.5)

    def _event_stream_thread(self):
        """Board incoming-event stream: gameStart and challenges.

        Challenge prompts block on the e-paper menu. They run on a worker so
        this loop can still attach a seek-take (``gameStart``) while the dialog
        is up.
        """
        if not self._client:
            return
        try:
            for event in self._client.board.stream_incoming_events():
                if self._should_stop.is_set() or self._game_id:
                    break
                if (event or {}).get("type") == "challenge":
                    threading.Thread(
                        target=self._handle_incoming_event,
                        args=(event,),
                        name="lichess-challenge-offer",
                        daemon=True,
                    ).start()
                    continue
                self._handle_incoming_event(event)
        except Exception as e:
            if not self._should_stop.is_set() and not self._game_id:
                log.warning(f"[LichessPlayer] Event stream ended: {e}")

    def _handle_incoming_event(self, event: dict) -> None:
        """React to one Board API event. Safe to call from tests."""
        if not event or self._should_stop.is_set():
            return
        etype = event.get("type")
        if etype == "gameStart":
            game = event.get("game") or {}
            self._begin_game(ongoing_game_id(game) or str(game.get("id") or ""))
            return
        if etype != "challenge" or self._lichess_config.mode != LichessGameMode.NEW:
            return
        if self._game_id:
            return
        challenge = event.get("challenge") or {}
        self._offer_incoming_challenge(challenge)

    def _offer_incoming_challenge(self, challenge: dict) -> None:
        """Ask the UI to Accept/Decline. Does not POST accept itself."""
        if self._game_id or self._should_stop.is_set():
            return
        if not self._challenge_is_incoming(challenge):
            return
        from .match import lichess_challenge_offer

        offer = lichess_challenge_offer(challenge)
        if offer is None:
            return
        callback = self._challenge_offer_callback
        if callback is None:
            log.info(
                f"[LichessPlayer] Incoming challenge {offer.challenge_id} left pending"
            )
            return
        with self._challenge_prompt_lock:
            if self._challenge_prompt_open:
                log.info(
                    f"[LichessPlayer] Incoming challenge {offer.challenge_id} "
                    "deferred; prompt already open"
                )
                return
            self._challenge_prompt_open = True
        try:
            callback(
                offer,
                lambda: self._accept_incoming_challenge(offer.challenge_id),
                lambda: self._decline_incoming_challenge(offer.challenge_id),
            )
        finally:
            with self._challenge_prompt_lock:
                self._challenge_prompt_open = False

    def _accept_incoming_challenge(self, challenge_id: str) -> None:
        """POST accept, or decline if a seek-take already attached a game."""
        if not challenge_id or not self._client:
            return
        if self._game_id or self._should_stop.is_set():
            self._decline_incoming_challenge(challenge_id)
            return
        log.info(f"[LichessPlayer] Accepting incoming challenge: {challenge_id}")
        try:
            self._client.challenges.accept(challenge_id)
        except Exception as e:
            log.warning(f"[LichessPlayer] Could not accept challenge: {e}")

    def _decline_incoming_challenge(self, challenge_id: str) -> None:
        """POST decline so the web opponent is not left waiting."""
        if not challenge_id or not self._client:
            return
        log.info(f"[LichessPlayer] Declining incoming challenge: {challenge_id}")
        try:
            self._client.challenges.decline(challenge_id)
        except Exception as e:
            log.warning(f"[LichessPlayer] Could not decline challenge: {e}")

    def _challenge_is_incoming(self, challenge: dict) -> bool:
        """True when this account is the challenge destination."""
        dest = challenge.get("destUser") or {}
        dest_id = str(dest.get("id") or dest.get("name") or "").lower()
        if dest_id:
            return dest_id == (self._username or "").lower()
        return str(challenge.get("direction") or "").lower() == "in"

    def _seek_game_thread(self):
        """Post the lobby seek and hold the HTTP stream open.

        When Lichess closes the stream, poll once more in case the event
        stream missed gameStart. The match poller is already looking while
        this call blocks.
        """
        if self._should_stop.is_set() or self._client is None:
            return
        try:
            rated = self._lichess_config.rated
            color = self._lichess_config.color_preference.lower()
            if color == 'random':
                color = None
            rating_range = (
                self._lichess_config.rating_range
                or self._account_range
            )
            
            self._client.board.seek(
                int(self._lichess_config.time_minutes),
                int(self._lichess_config.increment_seconds),
                rated,
                color=color,
                rating_range=rating_range
            )
            
            if not self._should_stop.is_set() and not self._game_id:
                self._try_attach_ongoing_game()
                
        except Exception as e:
            if not self._should_stop.is_set() and not self._game_id:
                log.error(f"[LichessPlayer] Seek failed: {e}")
                self._set_state(PlayerState.ERROR, "Seek failed")

    def _start_ongoing_game(self) -> bool:
        """Resume an ongoing game."""
        self._game_id = self._lichess_config.game_id
        if not self._game_id:
            log.error("[LichessPlayer] No game_id provided for ONGOING mode")
            return False
        
        log.info(f"[LichessPlayer] Resuming game: {self._game_id}")
        self._start_game_stream()
        return True
    
    def _start_challenge(self) -> bool:
        """Accept or wait for a challenge."""
        challenge_id = self._lichess_config.challenge_id
        if not challenge_id:
            log.error("[LichessPlayer] No challenge_id provided")
            return False
        
        log.info(f"[LichessPlayer] Handling challenge: {challenge_id}")
        self._report_status("Accepting challenge...")
        
        try:
            if self._lichess_config.challenge_direction == 'in':
                self._client.challenges.accept(challenge_id)
            
            self._game_id = challenge_id
            self._start_game_stream()
            return True
            
        except Exception as e:
            log.error(f"[LichessPlayer] Challenge handling failed: {e}")
            self._set_state(PlayerState.ERROR, "Challenge failed")
            return False
    
    def _start_game_stream(self):
        """Start the game state streaming thread."""
        log.info(f"[LichessPlayer] Starting game stream: {self._game_id}")
        
        self._stream_thread = threading.Thread(
            target=self._game_stream_thread,
            name="lichess-stream",
            daemon=True
        )
        self._stream_thread.start()
    
    def _game_stream_thread(self):
        """Background thread for streaming game state from Lichess."""
        log.info(f"[LichessPlayer] Stream thread started for {self._game_id}")
        
        try:
            game_stream = self._client.board.stream_game_state(self._game_id)
            
            for state in game_stream:
                if self._should_stop.is_set():
                    break
                
                self._process_game_state(state)
                
        except Exception as e:
            if not self._should_stop.is_set():
                log.error(f"[LichessPlayer] Stream error: {e}")
                self._set_state(PlayerState.ERROR, "Stream disconnected")
        
        log.info("[LichessPlayer] Stream thread ended")
    
    def _process_game_state(self, state: dict):
        """Process a game state update from Lichess stream."""
        log.debug(f"[LichessPlayer] State update: {state}")
        
        # Handle chat line messages (skip for game state purposes)
        if 'chatLine' in str(state):
            return
        
        # Handle opponent gone notification
        if 'opponentGone' in str(state):
            return
        
        # Handle text messages (takeback requests, draw offers)
        if 'text' in state:
            self._handle_text_message(state.get('text', ''))
            return
        
        # Extract player info from initial state
        if 'white' in state and 'black' in state:
            self._extract_player_info(state)
        
        # Extract moves and status
        if 'state' in state:
            inner_state = state['state']
            moves = inner_state.get('moves', '')
            status = inner_state.get('status', '')
            self._process_time_update(inner_state)
            # Check for takeback/draw offers in nested state
            if 'wtakeback' in inner_state or 'btakeback' in inner_state:
                self._handle_takeback_state(inner_state)
            if 'wdraw' in inner_state or 'bdraw' in inner_state:
                self._handle_draw_state(inner_state)
        else:
            moves = state.get('moves', '')
            status = state.get('status', '')
            self._process_time_update(state)
            # Check for takeback/draw offers
            if 'wtakeback' in state or 'btakeback' in state:
                self._handle_takeback_state(state)
            if 'wdraw' in state or 'bdraw' in state:
                self._handle_draw_state(state)
        
        moves = str(moves) if moves else ''
        
        if moves != self._remote_moves:
            self._sync_server_moves(moves)
        
        # Check game status
        self._check_game_status(status, state)
    
    def _handle_text_message(self, message: str):
        """Handle text messages from Lichess (takeback, draw offers).
        
        Args:
            message: The text message from Lichess.
        """
        log.info(f"[LichessPlayer] Text message: {message}")
        
        message_lower = message.lower()
        
        if 'takeback' in message_lower:
            # Opponent is requesting a takeback
            if self._takeback_offer_callback:
                log.info("[LichessPlayer] Takeback offer received, calling callback")
                self._takeback_offer_callback(self.accept_takeback, self.decline_takeback)
            else:
                # No callback - auto decline
                log.info("[LichessPlayer] No takeback callback, declining")
                self.decline_takeback()
        
        elif 'offers draw' in message_lower or 'draw offer' in message_lower:
            # Opponent is offering a draw
            if self._draw_offer_callback:
                log.info("[LichessPlayer] Draw offer received, calling callback")
                self._draw_offer_callback(self.accept_draw, self.decline_draw)
            else:
                # No callback - show info message but don't auto-accept/decline
                if self._info_message_callback:
                    self._info_message_callback("Draw offered")
    
    def _handle_takeback_state(self, state: dict):
        """Handle takeback offer state from Lichess.
        
        Args:
            state: State dict containing wtakeback/btakeback.
        """
        # Check if opponent has offered takeback
        opponent_offered = False
        if self._player_is_white:
            # We are white, opponent is black
            opponent_offered = state.get('btakeback', False)
        else:
            # We are black, opponent is white  
            opponent_offered = state.get('wtakeback', False)
        
        if opponent_offered:
            if self._takeback_offer_callback:
                log.info("[LichessPlayer] Opponent takeback offer in state, calling callback")
                self._takeback_offer_callback(self.accept_takeback, self.decline_takeback)
    
    def _handle_draw_state(self, state: dict):
        """Handle draw offer state from Lichess.
        
        Args:
            state: State dict containing wdraw/bdraw.
        """
        # Check if opponent has offered draw
        opponent_offered = False
        if self._player_is_white:
            # We are white, opponent is black
            opponent_offered = state.get('bdraw', False)
        else:
            # We are black, opponent is white
            opponent_offered = state.get('wdraw', False)
        
        if opponent_offered:
            if self._draw_offer_callback:
                log.info("[LichessPlayer] Opponent draw offer in state, calling callback")
                self._draw_offer_callback(self.accept_draw, self.decline_draw)
    
    def _extract_player_info(self, state: dict):
        """Extract player information from game state."""
        white_info = state.get('white', {})
        black_info = state.get('black', {})
        
        self._white_player = str(white_info.get('name', 'Unknown'))
        self._white_rating = str(white_info.get('rating', ''))
        self._black_player = str(black_info.get('name', 'Unknown'))
        self._black_rating = str(black_info.get('rating', ''))
        
        if self._white_player == self._username:
            self._player_is_white = True
            self._board_flip = False
            # This player represents the remote opponent (Black)
            self._color = chess.BLACK
        else:
            self._player_is_white = False
            self._board_flip = True
            # This player represents the remote opponent (White)
            self._color = chess.WHITE
        
        log.info(f"[LichessPlayer] Players: {self._white_player} ({self._white_rating}) vs "
                 f"{self._black_player} ({self._black_rating})")
        log.info(f"[LichessPlayer] Local user is: {'White' if self._player_is_white else 'Black'}")
        log.info(f"[LichessPlayer] This player instance represents: {'White' if self._color == chess.WHITE else 'Black'}")
        
        # Notify game info callback
        if self._game_info_callback:
            self._game_info_callback(
                self._white_player, self._white_rating,
                self._black_player, self._black_rating
            )
        
        # Replace the waiting splash with the started splash before READY. READY
        # fires on_all_players_ready (first-move request, clock); that must not
        # run while the panel still holds a modal "Waiting for game" splash.
        if self._on_game_connected:
            try:
                self._on_game_connected()
            except Exception as e:
                log.warning(f"[LichessPlayer] Error in on_game_connected: {e}")

        self._set_state(PlayerState.READY)
    
    def _process_time_update(self, state: dict):
        """Process clock time update."""
        try:
            wtime = state.get('wtime')
            btime = state.get('btime')
            
            if wtime is not None and isinstance(wtime, int):
                self._white_time = wtime // 1000
            
            if btime is not None and isinstance(btime, int):
                self._black_time = btime // 1000
            
            if self._clock_callback:
                self._clock_callback(self._white_time, self._black_time)
                
        except Exception as e:
            log.warning(f"[LichessPlayer] Error processing time: {e}")
    
    def _sync_server_moves(self, moves: str) -> None:
        """Apply a Lichess ``moves`` string: rewind takebacks, then new plies.

        The stream used to treat any change as a new last move. A takeback is
        a shorter prefix; without popping, Lichess and the board diverge.
        """
        removed, added = server_move_list_delta(self._last_processed_moves, moves)
        self._remote_moves = moves
        if removed:
            log.info(f"[LichessPlayer] Server took back {removed} half-move(s)")
            self._pending_move = None
            remaining = len(moves.split()) if moves else 0
            if self._remote_takeback_callback:
                self._remote_takeback_callback(remaining)
            kept = (self._last_processed_moves.split() if self._last_processed_moves else [])
            self._last_processed_moves = " ".join(kept[: len(kept) - removed])
        if not added:
            self._last_processed_moves = moves
            return
        self._check_for_remote_move()

    def _check_for_remote_move(self):
        """Check if there's a new remote move to process."""
        if not self._remote_moves:
            return
        
        moves_list = self._remote_moves.split()
        if not moves_list:
            return
        
        last_move = moves_list[-1].lower()
        
        if self._remote_moves == self._last_processed_moves:
            return
        
        # Determine who made the last move
        move_count = len(moves_list)
        last_move_was_white = (move_count % 2 == 1)
        
        # Echo classification needs the local player's colour. Until player info
        # has been parsed (_player_is_white set) the move cannot be classified as
        # the opponent's move vs an echo of our own. Defer WITHOUT consuming it
        # (leave _last_processed_moves unchanged) so it is re-evaluated once the
        # colour is known, rather than fabricating a possibly-self-echo pending move.
        if self._player_is_white is None:
            log.warning(f"[LichessPlayer] Deferring remote move {last_move}: local colour not yet known")
            return
        
        self._last_processed_moves = self._remote_moves
        
        # An echo of the local player's own move: the last move's colour matches
        # the local player's colour. Ignore it (already played and sent).
        if self._player_is_white == last_move_was_white:
            log.debug(f"[LichessPlayer] Ignoring echo of local move: {last_move}")
            return
        
        log.info(f"[LichessPlayer] Remote move from server: {last_move}")
        
        # Store as pending move - will be submitted after piece events confirm
        try:
            self._pending_move = chess.Move.from_uci(last_move)
            
            # Notify for LED display
            if self._pending_move_callback:
                self._pending_move_callback(self._pending_move)
        except Exception as e:
            log.error(f"[LichessPlayer] Invalid move from Lichess: {last_move}: {e}")
    
    def _check_game_status(self, status: str, state: dict):
        """Check game status and handle game end conditions.
        
        Fires game over callback with result, termination type, and winner.
        """
        status = str(status).lower()
        
        terminal_states = ['mate', 'resign', 'draw', 'aborted', 'outoftime', 'timeout', 'stalemate']
        
        if status in terminal_states:
            log.info(f"[LichessPlayer] Game ended: {status}")
            
            # Extract winner from state
            winner = state.get('winner')
            if winner:
                winner = str(winner).lower()
            
            # Determine result string
            if status == 'draw' or status == 'stalemate':
                result = "1/2-1/2"
            elif winner == 'white':
                result = "1-0"
            elif winner == 'black':
                result = "0-1"
            else:
                # Aborted or unclear - treat as draw
                result = "1/2-1/2" if status != 'aborted' else None
            
            # Map status to termination type
            termination_map = {
                'mate': 'CHECKMATE',
                'resign': 'RESIGN',
                'draw': 'DRAW',
                'aborted': 'ABORTED',
                'outoftime': 'TIMEOUT',
                'timeout': 'TIMEOUT',
                'stalemate': 'STALEMATE',
            }
            termination = termination_map.get(status, status.upper())
            
            log.info(f"[LichessPlayer] Game result: {result}, termination: {termination}, winner: {winner}")
            
            # Play sound effect
            try:
                from universalchess.board import board
                board.beep(board.SOUND_WRONG_MOVE)
            except Exception:  # noqa: S110 # nosec B110 - the game-over sound is cosmetic; a beep failure must not block the game-over callback
                pass
            
            # Fire game over callback
            if self._game_over_callback and result:
                try:
                    self._game_over_callback(result, termination, winner)
                except Exception as e:
                    log.error(f"[LichessPlayer] Error in game_over_callback: {e}")
            
            self._set_state(PlayerState.STOPPED)


def create_lichess_player(
    mode: LichessGameMode = LichessGameMode.NEW,
    time_minutes: int = 10,
    increment_seconds: int = 5,
    rated: bool = False,
    color: str = 'random',
    game_id: str = '',
    challenge_id: str = '',
) -> LichessPlayer:
    """Factory function to create a Lichess player.
    
    Args:
        mode: Game mode (NEW, ONGOING, CHALLENGE).
        time_minutes: Time control in minutes.
        increment_seconds: Increment in seconds.
        rated: Whether game is rated.
        color: Preferred color ('white', 'black', 'random').
        game_id: Game ID for ONGOING mode.
        challenge_id: Challenge ID for CHALLENGE mode.
    
    Returns:
        Configured LichessPlayer instance.
    """
    config = LichessPlayerConfig(
        name="Lichess",
        mode=mode,
        time_minutes=time_minutes,
        increment_seconds=increment_seconds,
        rated=rated,
        color_preference=color,
        game_id=game_id,
        challenge_id=challenge_id,
    )
    
    return LichessPlayer(config)


def lichess_player_from_seek(seek, *, color, join=None) -> LichessPlayer:
    """Build the Lichess player from seek params and optional lobby join.

    ``seek`` is :class:`~universalchess.players.lichess.match.LichessSeek`
    (clock, rated, color preference, account, rating range). ``join`` is the
    stash from PLAY / New Game / Ongoing / Challenge (``mode``, ``game_id``,
    ``challenge_id``, ``challenge_direction``). Omitting ``join`` attaches an
    ongoing game if one exists and does **not** post a seek (piece lift, boot
    resume). PLAY and New Game stash ``mode`` NEW.
    """
    join = join or {}
    mode = join.get("mode") or LichessGameMode.ATTACH
    return LichessPlayer(
        LichessPlayerConfig(
            name="Lichess",
            color=color,
            mode=mode,
            time_minutes=seek.time_minutes,
            increment_seconds=seek.increment_seconds,
            rated=seek.rated,
            color_preference=seek.color,
            rating_range=seek.rating_range,
            game_id=join.get("game_id", "") or "",
            challenge_id=join.get("challenge_id", "") or "",
            challenge_direction=join.get("challenge_direction", "in") or "in",
            account_id=seek.account_id,
        )
    )
