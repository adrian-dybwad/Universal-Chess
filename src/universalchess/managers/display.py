"""
Display manager for game-related UI widgets.

This module provides a centralized manager for all game display widgets,
handling widget lifecycle, menu presentation, and display state management.
It separates UI concerns from game logic (GameManager) and protocol handling (ProtocolManager).

Note: This is distinct from the lower-level epaper Manager class which handles
framebuffer rendering. This DisplayManager orchestrates game-specific widgets.

Responsibilities:
- Create and manage ChessBoardWidget, GameAnalysisWidget
- Handle promotion menu display and selection
- Handle back button menu (resign/draw/cancel)
- Restore display after menu interactions
- React to game events (new game, moves, etc.)
"""

import threading
import pathlib
import chess
import chess.engine

from universalchess.board import board
from universalchess.board.logging import log
from universalchess.services import get_chess_clock_service
from universalchess.services.engine_registry import get_engine_registry, EngineHandle
from universalchess.state import get_chess_clock as get_clock_state
from universalchess.state import get_chess_game

# Lazy imports for widgets to avoid loading all epaper modules at startup
_widgets_loaded = False
_ChessBoardWidget = None
_GameAnalysisWidget = None
_ChessClockWidget = None
_IconMenuWidget = None
_IconMenuEntry = None
_SplashScreen = None
_GameOverWidget = None
_AlertWidget = None
_PauseWidget = None
_SetupStatusWidget = None


def _load_widgets():
    """Lazily load widget classes."""
    global _widgets_loaded, _ChessBoardWidget, _GameAnalysisWidget, _ChessClockWidget
    global _IconMenuWidget, _IconMenuEntry, _SplashScreen
    global _GameOverWidget, _AlertWidget, _PauseWidget, _SetupStatusWidget
    
    if _widgets_loaded:
        return
    
    from universalchess.epaper import (
        ChessBoardWidget, GameAnalysisWidget, ChessClockWidget,
        IconMenuWidget, IconMenuEntry, SplashScreen,
        AlertWidget, SetupStatusWidget
    )
    from universalchess.epaper.game_over import GameOverWidget
    from universalchess.epaper.pause import PauseWidget
    _ChessBoardWidget = ChessBoardWidget
    _GameAnalysisWidget = GameAnalysisWidget
    _ChessClockWidget = ChessClockWidget
    _IconMenuWidget = IconMenuWidget
    _IconMenuEntry = IconMenuEntry
    _SplashScreen = SplashScreen
    _GameOverWidget = GameOverWidget
    _AlertWidget = AlertWidget
    _PauseWidget = PauseWidget
    _SetupStatusWidget = SetupStatusWidget
    _widgets_loaded = True


# Starting position FEN
STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class DisplayManager:
    """Manager for game display widgets.
    
    Manages the lifecycle of game-related widgets and handles UI interactions
    like menus. Provides a clean interface for game logic to update the display
    without knowing about widget implementation details.
    
    Note: This is distinct from the lower-level epaper Manager class. This
    DisplayManager orchestrates game-specific widgets at a higher level.
    
    Layout (below status bar at y=16):
    - Chess board: y=16, height=128 (16 pixels per square * 8)
    - Clock widget: y=144, height=72 (turn indicator / time - prominent display)
    - Analysis widget: y=216, height=80 (eval bar and history graph)
    
    Attributes:
        chess_board_widget: The chess board display widget
        clock_widget: The chess clock / turn indicator widget
        analysis_widget: The game analysis/evaluation widget
        analysis_engine: UCI engine for position analysis
    """
    
    def __init__(self, flip_board: bool = False, show_analysis: bool = True,
                 analysis_engine_path: str = None, on_exit: callable = None,
                 initial_fen: str = None,
                 time_control: int = 0, show_board: bool = True,
                 show_clock: bool = True,
                 show_graph: bool = True, analysis_mode: bool = True,
                 led_from_to_hint_callback: callable = None,
                 led_off_callback: callable = None):
        """Initialize the display controller.
        
        Args:
            flip_board: If True, display board from black's perspective
            show_analysis: If True, show analysis widget (default visible)
            analysis_engine_path: Path to UCI engine for analysis (e.g., ct800)
            on_exit: Callback function() when user requests exit via back menu
            initial_fen: FEN string for initial position. If None, uses starting position.
            time_control: Time per player in minutes (0 = disabled/untimed, shows turn only)
            show_board: If True, show the chess board widget
            show_clock: If True, show the clock/turn indicator widget
            show_graph: If True, show the history graph in analysis widget
            analysis_mode: If True, create analysis engine/widget (may be hidden by show_analysis)
            led_from_to_hint_callback: LED callback (from_sq, to_sq, repeat) for hint-style
                                       LEDs (slow speed, dim intensity). Used for check/queen alerts.
            led_off_callback: LED callback () to turn off all LEDs. Used for pause.
        
        Note: Player names are read from PlayersState by the clock widget.
              Hand-brain hints are set per-player via set_brain_hint().
        """
        _load_widgets()
        
        self._led_from_to_hint = led_from_to_hint_callback
        self._led_off = led_off_callback
        
        self._flip_board = flip_board
        self._show_analysis = show_analysis
        self._analysis_mode = analysis_mode  # Whether to create analysis engine/widget at all
        self._on_exit = on_exit
        self._time_control = time_control  # Minutes per player (0 = disabled)
        
        # Game state - authoritative source for position
        # Set initial position if provided, otherwise use game state's current position
        self._game_state = get_chess_game()
        if initial_fen and initial_fen != STARTING_FEN:
            self._game_state.set_position(initial_fen)
        self._show_board = show_board
        self._show_clock = show_clock
        self._show_graph = show_graph
        
        # Widgets
        self.chess_board_widget = None
        self.clock_widget = None
        self.analysis_widget = None
        self._analysis_engine_handle = None  # EngineHandle from registry
        self.alert_widget = None
        self.pause_widget = None
        self.game_over_widget = None
        self.setup_status_widget = None
        
        # Pause state
        self._is_paused = False
        self._on_resume_callback = None  # Called when game resumes to restore LEDs
        
        # Menu state
        self._menu_active = False
        self._current_menu = None
        self._menu_result_callback = None
        
        # Key callback for routing during menu
        self._original_key_callback = None
        self._key_callback = None
        
        # Store engine path for async initialization
        self._analysis_engine_path = analysis_engine_path
        self._engine_init_thread = None
        
        # Get ChessClock singleton for this game
        # The clock persists across widget creation/destruction
        self._clock = get_chess_clock_service()
        self._clock.configure(time_control_minutes=time_control)
        
        # Initialize widgets first (fast, non-blocking)
        self._init_widgets()
        
        # Initialize analysis engine asynchronously (slow, done in background)
        if analysis_engine_path:
            self._init_analysis_engine_async(analysis_engine_path)
    
    def _init_analysis_engine_async(self, engine_path: str):
        """Initialize the UCI analysis engine asynchronously via registry.
        
        Acquires the engine from the registry, which may reuse an existing
        instance if another consumer (player engine) is using the same binary.
        
        Args:
            engine_path: Path to the UCI engine executable
        """
        def _on_engine_ready(handle: EngineHandle):
            log.info(f"[DisplayManager] Analysis engine ready: {handle.path}")
            
            # Store handle for hints
            self._analysis_engine_handle = handle
            
            # Set engine handle on AnalysisService
            from universalchess.services.analysis import get_analysis_service
            analysis_service = get_analysis_service()
            analysis_service.set_engine_handle(handle)
            analysis_service.start()
        
        def _on_engine_error(e: Exception):
            log.warning(f"[DisplayManager] Could not initialize analysis engine: {e}")
            self._analysis_engine_handle = None
        
        log.info(f"[DisplayManager] Starting analysis engine initialization: {engine_path}")
        get_engine_registry().acquire_async(
            engine_path,
            on_ready=_on_engine_ready,
            on_error=_on_engine_error
        )
    
    def _reload_display_settings(self):
        """Reload display settings from config file.
        
        Called when display menu changes settings during a game.
        """
        from universalchess.board.settings import Settings
        
        def load_bool(key: str, default: bool) -> bool:
            val = Settings.read('game', key, 'true' if default else 'false')
            return val.lower() == 'true'
        
        self._show_board = load_bool('show_board', True)
        self._show_clock = load_bool('show_clock', True)
        self._show_analysis = load_bool('show_analysis', True)
        self._show_graph = load_bool('show_graph', True)
        
        log.info(f"[DisplayManager] Reloaded display settings: board={self._show_board}, "
                 f"clock={self._show_clock}, analysis={self._show_analysis}, "
                 f"graph={self._show_graph}")
    
    def _init_widgets(self):
        """Create and add widgets to the display manager.
        
        Layout:
        - Status bar: y=0, height=16
        - Chess board: y=16, height=128
        - Clock widget: y=144, height=72 (prominent turn/time display)
        - Analysis widget: y=216, height=80 (eval bar and history)
        
        Widget creation rules:
        - Clock widget: Always created if time_control > 0, hidden if show_clock=False
        - Analysis widget: Always created if analysis mode enabled, hidden if show_analysis=False
        - Board widget: Always created, hidden if show_board=False
        """
        # Reload settings from config in case they changed (e.g., via display menu)
        self._reload_display_settings()
        
        if not board.display_manager:
            log.error("[DisplayManager] No epaper manager available")
            return
        
        # Clear any existing widgets (performs full refresh to clear e-paper ghosting)
        board.display_manager.clear_widgets()

        # Create chess board widget at y=16 (below status bar)
        # Widget subscribes to game_state and updates automatically
        self.chess_board_widget = _ChessBoardWidget(
            0, 16, board.display_manager.update,
            game_state=self._game_state,
            flip=self._flip_board
        )
        
        # Note: Widgets subscribe to state directly for updates:
        # - ChessBoardWidget observes ChessGameState for position
        # - ChessClockWidget observes ChessGameState for turn indicator
        # - ChessClockWidget observes ChessClockState for times
        # - GameAnalysisWidget observes AnalysisState
        
        # Always add board widget, but hide if show_board=False
        board.display_manager.add_widget(self.chess_board_widget)
        if self._show_board:
            log.info("[DisplayManager] Chess board widget initialized (visible)")
        else:
            self.chess_board_widget.hide()
            log.info("[DisplayManager] Chess board widget initialized (hidden)")
        
        # Calculate dynamic layout based on which widgets are shown
        # Available space: y=144 to y=296 (152 pixels total)
        # Layout depends on what's enabled:
        # - Clock needs more space if timed mode, less if just turn indicator
        # - Analysis uses fixed height when visible
        
        # Determine analysis widget height based on visibility
        if self._show_analysis:
            if self._show_graph:
                # Full analysis: score text + graph
                analysis_height = 80
            else:
                # Score text only (no graph)
                analysis_height = 54
        else:
            analysis_height = 0
        
        # Clock gets remaining space
        clock_height = 152 - analysis_height
        if clock_height < 36:
            clock_height = 36  # Minimum for turn indicator
        
        clock_y = 144
        analysis_y = clock_y + clock_height
        
        log.info(f"[DisplayManager] Layout: clock_height={clock_height}, analysis_height={analysis_height}")
        
        # Create clock widget directly below board
        # Shows times if time_control > 0, otherwise shows turn indicator only
        # flip matches board orientation so clock top matches board top
        # Hand-brain hints are shown in the clock widget via set_brain_hint()
        timed_mode = self._time_control > 0
        
        # Set initial times only if clock hasn't been started yet
        # This preserves times when recreating widgets (e.g., after menu exit)
        if self._time_control > 0 and not self._clock.is_running:
            initial_seconds = self._time_control * 60
            self._clock.set_times(initial_seconds, initial_seconds)
        
        self.clock_widget = _ChessClockWidget(
            0, clock_y, 128, clock_height, board.display_manager.update,
            timed_mode=timed_mode, flip=self._flip_board
        )
        # Always add clock widget if timed mode, hidden if show_clock=False
        # For untimed mode, only add if show_clock=True
        if timed_mode:
            board.display_manager.add_widget(self.clock_widget)
            if self._show_clock:
                log.info(f"[DisplayManager] Clock widget initialized (visible, y={clock_y}, height={clock_height}, time_control={self._time_control} min)")
            else:
                self.clock_widget.hide()
                log.info(f"[DisplayManager] Clock widget initialized (hidden, y={clock_y}, height={clock_height}, time_control={self._time_control} min)")
        elif self._show_clock:
            board.display_manager.add_widget(self.clock_widget)
            log.info(f"[DisplayManager] Clock widget initialized (visible, turn indicator only, y={clock_y}, height={clock_height})")
        else:
            log.info("[DisplayManager] Clock widget disabled (untimed mode)")
        
        # Create analysis widget below clock - only if analysis_mode is enabled
        # Widget observes AnalysisState for display updates
        # AnalysisService handles running analysis and updating AnalysisState
        if self._analysis_mode:
            bottom_color = "black" if self.chess_board_widget.flip else "white"
            self.analysis_widget = _GameAnalysisWidget(
                0, analysis_y, 128, analysis_height if analysis_height > 0 else 80, board.display_manager.update,
                bottom_color=bottom_color,
                show_graph=self._show_graph
            )
            
            if not self._show_analysis:
                self.analysis_widget.hide()
            
            board.display_manager.add_widget(self.analysis_widget)
            log.info(f"[DisplayManager] Analysis widget initialized (visible={self._show_analysis}, graph={self._show_graph})")
        else:
            self.analysis_widget = None
            log.info("[DisplayManager] Analysis mode disabled - no analysis widget created")
        
        # Create alert widget for CHECK/QUEEN warnings (y=144, overlays clock widget)
        # Alert widget is hidden by default and shown when check or queen threat occurs
        self.alert_widget = _AlertWidget(
            0, 144, 128, 40, board.display_manager.update,
            led_from_to_hint_callback=self._led_from_to_hint
        )
        board.display_manager.add_widget(self.alert_widget)
        log.info("[DisplayManager] Alert widget initialized (hidden)")
        
        # Create game over widget (y=144, same position as clock)
        # Widget observes ChessGameState and shows/hides itself automatically:
        # - Shows on game_over event (checkmate, stalemate, resignation, etc.)
        # - Hides on position_change when game is no longer over (new game started)
        # ChessClockWidget also observes game_over and manages its own visibility.
        self.game_over_widget = _GameOverWidget(
            0, 144, 128, 72, board.display_manager.update,
            led_off_callback=self._led_off
        )
        board.display_manager.add_widget(self.game_over_widget)
        log.info("[DisplayManager] Game over widget initialized (hidden, observes game state)")

        # The freshly created alert widget is hidden and unaware of any check /
        # queen threat already present in the position. Re-derive the alert from
        # the authoritative game state so a rebuild mid-check (e.g. after the
        # king-lift resign or kings-in-center menu is cancelled) re-shows it rather
        # than silently dropping it.
        self._game_state.refresh_alerts()
    
    def set_key_callback(self, callback: callable):
        """Set the key callback for routing keys during normal play.
        
        Args:
            callback: Function(key) to call for key events
        """
        self._key_callback = callback
    
    def get_hint_move(self, board_obj, time_limit: float = 1.0):
        """Get a hint move for the current position.
        
        Uses the analysis engine to find the best move.
        
        Args:
            board_obj: chess.Board object to analyze
            time_limit: Analysis time limit in seconds (default 1.0 for hints)
            
        Returns:
            chess.Move object if a hint is available, None otherwise
        """
        if not self._analysis_engine_handle:
            log.warning("[DisplayManager] No analysis engine available for hint")
            return None
        
        try:
            import chess.engine
            result = self._analysis_engine_handle.play(board_obj, chess.engine.Limit(time=time_limit))
            if result.move:
                log.info(f"[DisplayManager] Hint move: {result.move.uci()}")
                return result.move
        except Exception as e:
            log.warning(f"[DisplayManager] Error getting hint move: {e}")
        
        return None
    
    def show_hint(self, move) -> None:
        """Show a hint move on the display and LEDs.
        
        Args:
            move: chess.Move object to show as hint
        """
        if not move:
            return
        
        if self.alert_widget:
            # Format move as readable text
            move_text = move.uci()
            from_sq = move.from_square
            to_sq = move.to_square
            
            self.alert_widget.show_hint(move_text, from_sq, to_sq)
            log.info(f"[DisplayManager] Showing hint: {move_text}")
    
    def set_clock_times(self, white_seconds: int, black_seconds: int) -> None:
        """Set the chess clock times for both players.
        
        Args:
            white_seconds: White's time in seconds
            black_seconds: Black's time in seconds
        """
        self._clock.set_times(white_seconds, black_seconds)
    
    def start_clock(self) -> None:
        """Start the chess clock countdown.
        
        For timed games (time_control > 0), starts the countdown.
        For untimed games, this is a no-op since the turn indicator comes from
        ChessGameState which the clock widget observes directly.
        
        Note: Turn indicator (whose turn it is) comes from ChessGameState, not
        from manual clock switching. The ChessClockWidget observes game state
        directly for turn changes.
        """
        if self._time_control > 0:
            # Timed mode: start the countdown
            self._clock.start()
    
    def pause_clock(self) -> None:
        """Pause the chess clock."""
        self._clock.pause()
    
    def stop_clock(self) -> None:
        """Stop the chess clock completely."""
        self._clock.stop()
    
    def reset_clock(self) -> None:
        """Reset the chess clock to initial time and stop it.
        
        Called when a new game starts to reset clock state.
        The clock will not start until the first move is made.
        """
        self._clock.reset()
        log.info(f"[DisplayManager] Clock reset to {self._time_control} min per player")
    
    def toggle_pause(self) -> bool:
        """Toggle pause state for the game.
        
        When paused:
        - Clock is paused
        - LEDs are turned off
        - A pause widget is shown in the center of the screen
        
        When resumed:
        - Clock resumes for the previously active player
        - Pause widget is hidden
        
        Returns:
            True if now paused, False if now resumed
        """
        if self._is_paused:
            self._resume_game()
            return False
        else:
            self._pause_game()
            return True
    
    def _pause_game(self) -> None:
        """Pause the game - stop clock, turn off LEDs, show pause widget."""
        if self._is_paused:
            return
        
        self._is_paused = True
        self._clock.pause()
        
        # Turn off LEDs via callback
        if self._led_off:
            self._led_off()
        else:
            log.warning("[DisplayManager] LED off callback not set, skipping LED off")
        
        # Show pause widget (centered on screen)
        # Import here to avoid circular imports
        self.pause_widget = _PauseWidget(update_callback=board.display_manager.update)
        board.display_manager.add_widget(self.pause_widget)
        
        log.info("[DisplayManager] Game paused")
    
    def _resume_game(self) -> None:
        """Resume the game - restart clock, remove pause widget, restore LEDs."""
        if not self._is_paused:
            return
        
        self._is_paused = False
        
        # Remove pause widget
        if self.pause_widget:
            board.display_manager.remove_widget(self.pause_widget)
            self.pause_widget = None
        
        # Resume clock (turn indicator comes from ChessGameState, not clock service)
        self._clock.resume()
        log.info("[DisplayManager] Clock resumed")
        
        # Notify resume callback to restore LEDs if needed
        if self._on_resume_callback:
            try:
                self._on_resume_callback()
            except Exception as e:
                log.warning(f"[DisplayManager] Error in resume callback: {e}")
        
        log.info("[DisplayManager] Game resumed")
    
    def is_paused(self) -> bool:
        """Check if the game is currently paused.
        
        Returns:
            True if game is paused, False otherwise
        """
        return self._is_paused
    
    def clear_pause(self) -> None:
        """Clear pause state without resuming clock.
        
        Called on new game to ensure clean state.
        """
        if self._is_paused:
    
            # Remove pause widget if present
            if self.pause_widget:
                board.display_manager.remove_widget(self.pause_widget)
                self.pause_widget = None
            self._is_paused = False
            log.info("[DisplayManager] Pause state cleared")
    
    def on_setup_display(self, active: bool, fen: str = None) -> None:
        """Drive the display while Chessnut puzzle setup mode is active.

        Replaces the turn indicator with a "SETUP MODE" status panel and redraws
        the board from the evolving setup FEN. Idempotent: repeated active calls
        only refresh the board. Injected into GameManager via
        set_setup_display_handler so the emulator can signal setup progress.

        Args:
            active: True to show setup status (and redraw with ``fen``); False to
                hide it and restore the normal turn indicator.
            fen: Placement (or full) FEN to draw on the board while active.
        """
        if active:
            self._show_setup_status()
            if fen and self.chess_board_widget:
                self.chess_board_widget.set_fen(fen)
        else:
            self._hide_setup_status()

    def _show_setup_status(self) -> None:
        """Show the setup status panel in the turn-indicator region (idempotent)."""
        if self.setup_status_widget is not None:
            return
        # Hide the turn indicator while setup suspends normal play.
        if self.clock_widget:
            self.clock_widget.hide()
        self.setup_status_widget = _SetupStatusWidget(
            0, 144, 128, 72, board.display_manager.update
        )
        board.display_manager.add_widget(self.setup_status_widget)
        log.info("[DisplayManager] Setup status shown (turn indicator hidden)")

    def _hide_setup_status(self) -> None:
        """Remove the setup status panel and restore the turn indicator."""
        if self.setup_status_widget is not None:
            board.display_manager.remove_widget(self.setup_status_widget)
            self.setup_status_widget = None
            log.info("[DisplayManager] Setup status hidden")
        if self.clock_widget:
            self.clock_widget.show()

    def get_clock_times(self) -> tuple:
        """Get the current clock times for both players.

        Returns:
            Tuple of (white_seconds, black_seconds)
        """
        return self._clock.get_times()

    def set_on_flag(self, callback) -> None:
        """Set callback for when a player's time expires (flag).

        Args:
            callback: Function(color: str) where color is 'white' or 'black'
        """
        # Observers register on state, control goes through service
        get_clock_state().on_flag(callback)
    
    def set_on_resume(self, callback) -> None:
        """Set callback for when game is resumed from pause.
        
        Called after clock resumes to allow restoration of LEDs for pending moves.
        
        Args:
            callback: Function() called when game resumes
        """
        self._on_resume_callback = callback
    
    def set_brain_hint(self, color: str, piece_symbol: str) -> None:
        """Set the brain hint piece type for a player in Hand+Brain mode.
        
        Shows the piece letter in the clock widget next to the player's timer,
        replacing the turn indicator circle.
        
        Args:
            color: 'white' or 'black'
            piece_symbol: Piece symbol (K, Q, R, B, N, P) or empty to clear
        """
        if self.clock_widget:
            self.clock_widget.set_brain_hint(color, piece_symbol)
    
    def clear_brain_hint(self, color: str) -> None:
        """Clear the brain hint for a player.
        
        Args:
            color: 'white' or 'black'
        """
        if self.clock_widget:
            self.clock_widget.clear_brain_hint(color)
    
    
    def show_promotion_menu(self, is_white: bool) -> str:
        """Show promotion piece selection menu.
        
        Blocks until user selects a piece or timeout.
        
        Args:
            is_white: True if white pawn is promoting
            
        Returns:
            Promotion piece letter ('q', 'r', 'b', 'n')
        """

        
        # Create menu entries with chess piece icons
        color_suffix = "w" if is_white else "b"
        
        entries = [
            _IconMenuEntry(key="q", label="Queen", icon_name=f"Q{color_suffix}"),
            _IconMenuEntry(key="r", label="Rook", icon_name=f"R{color_suffix}"),
            _IconMenuEntry(key="b", label="Bishop", icon_name=f"B{color_suffix}"),
            _IconMenuEntry(key="n", label="Knight", icon_name=f"N{color_suffix}"),
        ]
        
        # Selection synchronization
        selection_event = threading.Event()
        selected_piece = ["q"]  # Default to queen
        
        def on_select(entry_key: str):
            selected_piece[0] = entry_key
            selection_event.set()
        
        # Create and display menu
        promotion_menu = _IconMenuWidget(
            0, 0, 128, 296, board.display_manager.update,
            entries=entries,
            on_select=on_select
        )
        promotion_menu.activate()
        self._menu_active = True
        self._current_menu = promotion_menu
        
        # Clear display and show menu
        if board.display_manager:
            board.display_manager.clear_widgets(addStatusBar=False)
            future = board.display_manager.add_widget(promotion_menu)
            if future:
                try:
                    future.result(timeout=2.0)
                except Exception:
                    pass
        
        # Route keys to menu
        self._original_key_callback = self._key_callback
        self._key_callback = lambda key: promotion_menu.handle_key(key)
        
        # Wait for selection
        selection_event.wait(timeout=60.0)
        
        # Restore key callback
        self._key_callback = self._original_key_callback
        
        # Cleanup
        promotion_menu.deactivate()
        self._menu_active = False
        self._current_menu = None
        
        # Restore game display
        self._init_widgets()
        
        log.info(f"[DisplayManager] Promotion selected: {selected_piece[0]}")
        return selected_piece[0]
    
    def show_back_menu(self, on_result: callable, is_two_player: bool = False):
        """Show the back button menu (resign/draw/cancel).
        
        Non-blocking - calls on_result when user makes a selection.
        
        Args:
            on_result: Callback function(result: str) with result:
                      'resign', 'resign_white', 'resign_black', 'draw', 'cancel', or 'exit'
            is_two_player: If True, show separate resign options for white and black
        """

        
        log.info(f"[DisplayManager] Showing back menu (two_player={is_two_player})")
        
        if is_two_player:
            # In 2-player mode, show separate resign options for each side
            # White flag (white fill, black border) for white resigns
            # Black flag (black fill, white border) for black resigns
            entries = [
                _IconMenuEntry(key="resign_white", label="White\nResigns", icon_name="resign_white"),
                _IconMenuEntry(key="resign_black", label="Black\nResigns", icon_name="resign_black"),
                _IconMenuEntry(key="draw", label="Draw", icon_name="draw"),
                _IconMenuEntry(key="cancel", label="Cancel", icon_name="cancel"),
            ]
        else:
            entries = [
                _IconMenuEntry(key="resign", label="Resign", icon_name="resign"),
                _IconMenuEntry(key="draw", label="Draw", icon_name="draw"),
                _IconMenuEntry(key="cancel", label="Cancel", icon_name="cancel"),
            ]
        
        # Create menu - default to Cancel (last item)
        back_menu = _IconMenuWidget(
            0, 0, 128, 296, board.display_manager.update,
            entries=entries,
            selected_index=len(entries) - 1  # Default to Cancel (last item)
        )
        
        self._menu_result_callback = on_result
        self._current_menu = back_menu
        self._menu_active = True
        
        # Clear display and show menu
        if board.display_manager:
            board.display_manager.clear_widgets(addStatusBar=False)
            future = board.display_manager.add_widget(back_menu)
            if future:
                try:
                    future.result(timeout=2.0)
                except Exception as e:
                    log.debug(f"[DisplayManager] Error displaying menu: {e}")
        
        # Activate menu
        back_menu.activate()
        
        # Start thread to wait for result
        def wait_for_result():
            try:
                back_menu._selection_event.wait()
                result = back_menu._selection_result or "BACK"
                
                log.info(f"[DisplayManager] Back menu result: {result}")
                
                # Cleanup
                self._menu_active = False
                back_menu.deactivate()
                self._current_menu = None
                
                # Map special keys
                if result == "BACK":
                    result = "cancel"
                elif result == "SHUTDOWN":
                    result = "exit"
                
                # Restore display for cancel, or let caller handle for resign/draw
                if result == "cancel":
                    self._init_widgets()
                
                # Call result callback
                if self._menu_result_callback:
                    self._menu_result_callback(result)
                    
            except Exception as e:
                log.error(f"[DisplayManager] Error in back menu: {e}")
                import traceback
                traceback.print_exc()
                self._menu_active = False
                self._current_menu = None
                if self._menu_result_callback:
                    self._menu_result_callback("cancel")
        
        wait_thread = threading.Thread(target=wait_for_result, daemon=True)
        wait_thread.start()
    
    def cancel_menu(self):
        """Cancel the active menu by simulating a BACK key press.
        
        This is called when an external event (like pieces being returned to position)
        should dismiss the menu. It uses the standard BACK key handling path to ensure
        proper cleanup and display restoration.
        """
        if self._menu_active and self._current_menu:
            log.info("[DisplayManager] Cancelling menu via simulated BACK key")
    
            self._current_menu.handle_key(board.Key.BACK)
    
    def show_king_lift_resign_menu(self, king_color, on_result: callable):
        """Show resign confirmation menu when king is held off board for 3+ seconds.
        
        Non-blocking - calls on_result when user makes a selection.
        
        Args:
            king_color: chess.WHITE or chess.BLACK - the color of the lifted king
            on_result: Callback function(result: str) with result:
                      'resign' or 'cancel'
        """

        
        color_name = "White" if king_color else "Black"
        # Use same resign icons as kings-in-center: resign_white for white, resign_black for black
        icon_name = "resign_white" if king_color else "resign_black"
        log.info(f"[DisplayManager] Showing king-lift resign menu for {color_name}")
        
        entries = [
            _IconMenuEntry(key="resign", label=f"Resign\n{color_name}?", icon_name=icon_name),
            _IconMenuEntry(key="cancel", label="No", icon_name="cancel"),
        ]
        
        # Create menu - default to No (cancel)
        resign_menu = _IconMenuWidget(
            0, 0, 128, 296, board.display_manager.update,
            entries=entries,
            selected_index=1  # Default to No (cancel)
        )
        
        self._menu_result_callback = on_result
        self._current_menu = resign_menu
        self._menu_active = True
        
        # Clear display and show menu
        if board.display_manager:
            board.display_manager.clear_widgets(addStatusBar=False)
            future = board.display_manager.add_widget(resign_menu)
            if future:
                try:
                    future.result(timeout=2.0)
                except Exception as e:
                    log.debug(f"[DisplayManager] Error displaying menu: {e}")
        
        # Play a beep to indicate the gesture was recognized
        board.beep(board.SOUND_GENERAL)
        
        # Activate menu
        resign_menu.activate()
        
        # Start thread to wait for result
        def wait_for_result():
            try:
                resign_menu._selection_event.wait()
                result = resign_menu._selection_result or "BACK"
                
                log.info(f"[DisplayManager] King-lift resign menu result: {result}")
                
                # Cleanup
                self._menu_active = False
                resign_menu.deactivate()
                self._current_menu = None
                
                # Map special keys
                if result == "BACK":
                    result = "cancel"
                elif result == "SHUTDOWN":
                    result = "cancel"  # Don't shutdown from this menu
                
                # Restore display for cancel, or let caller handle for resign
                if result == "cancel":
                    self._init_widgets()
                
                # Call result callback
                if self._menu_result_callback:
                    self._menu_result_callback(result)
                    
            except Exception as e:
                log.error(f"[DisplayManager] Error in king-lift resign menu: {e}")
                import traceback
                traceback.print_exc()
                self._menu_active = False
                self._current_menu = None
                if self._menu_result_callback:
                    self._menu_result_callback("cancel")
        
        wait_thread = threading.Thread(target=wait_for_result, daemon=True)
        wait_thread.start()
    
    def handle_key(self, key):
        """Route key events to active menu or external callback.
        
        Args:
            key: Key that was pressed (board.Key enum)
        """
        if self._menu_active and self._current_menu:
            self._current_menu.handle_key(key)
        elif self._key_callback:
            self._key_callback(key)
    
    def is_menu_active(self) -> bool:
        """Check if a menu is currently being displayed.
        
        Returns:
            True if a menu is active
        """
        return self._menu_active
    
    def show_splash(self, message: str):
        """Show a splash screen with a message.
        
        Args:
            message: Message to display
        """

        
        try:
            if board.display_manager:
                board.display_manager.clear_widgets(addStatusBar=False)
                splash = _SplashScreen(board.display_manager.update, message=message)
                future = board.display_manager.add_widget(splash)
                if future:
                    try:
                        future.result(timeout=2.0)
                    except Exception:
                        pass
        except Exception as e:
            log.debug(f"[DisplayManager] Error showing splash: {e}")
    
    
    def cleanup(self, for_shutdown: bool = False):
        """Clean up resources (analysis engine, widgets) and clear display.
        
        Args:
            for_shutdown: If True, skip creating new widgets (faster shutdown)
        """
        log.info(f"[DisplayManager] Starting cleanup (for_shutdown={for_shutdown})...")
        
        # Wait for engine init thread if still running (brief wait)
        log.info("[DisplayManager] Checking engine init thread...")
        if self._engine_init_thread is not None and self._engine_init_thread.is_alive():
            try:
                log.info("[DisplayManager] Engine init thread is alive, joining with 1s timeout...")
                self._engine_init_thread.join(timeout=1.0)
                if self._engine_init_thread.is_alive():
                    log.warning("[DisplayManager] Engine init thread did not exit within timeout")
                else:
                    log.info("[DisplayManager] Engine init thread joined")
            except Exception as e:
                log.error(f"[DisplayManager] Error joining engine init thread: {e}")
        else:
            log.info("[DisplayManager] Engine init thread not running")
        
        # Cleanup chess board widget (unsubscribes from game state)
        log.info("[DisplayManager] Cleaning up chess board widget...")
        if self.chess_board_widget:
            try:
                self.chess_board_widget.cleanup()
                log.info("[DisplayManager] Chess board widget cleaned up")
            except Exception as e:
                log.debug(f"[DisplayManager] Error cleaning up chess board widget: {e}")
        
        # Stop clock service
        log.info("[DisplayManager] Stopping clock service...")
        try:
            self._clock.stop()
            log.info("[DisplayManager] Clock service stopped")
        except Exception as e:
            log.error(f"[DisplayManager] Error stopping clock service: {e}", exc_info=True)
        
        # Stop analysis service
        log.info("[DisplayManager] Stopping analysis service...")
        try:
            from universalchess.services.analysis import get_analysis_service
            analysis_service = get_analysis_service()
            analysis_service.stop()
            log.info("[DisplayManager] Analysis service stopped")
        except Exception as e:
            log.error(f"[DisplayManager] Error stopping analysis service: {e}", exc_info=True)
        
        # Cleanup analysis widget (unsubscribe from state)
        if self.analysis_widget:
            try:
                self.analysis_widget.cleanup()
                log.info("[DisplayManager] Analysis widget cleaned up")
            except Exception as e:
                log.debug(f"[DisplayManager] Error cleaning up analysis widget: {e}")
        
        # Cleanup game over widget (unsubscribe from game state)
        if self.game_over_widget:
            try:
                self.game_over_widget.cleanup()
                log.info("[DisplayManager] Game over widget cleaned up")
            except Exception as e:
                log.debug(f"[DisplayManager] Error cleaning up game over widget: {e}")
        
        # Release analysis engine handle back to registry
        log.info("[DisplayManager] Releasing analysis engine...")
        if self._analysis_engine_handle:
            try:
                get_engine_registry().release(self._analysis_engine_handle)
                log.info("[DisplayManager] Analysis engine released")
            except Exception as e:
                log.debug(f"[DisplayManager] Error releasing analysis engine: {e}")
            self._analysis_engine_handle = None
        else:
            log.info("[DisplayManager] No analysis engine to release")
        
        # Clear widgets - skip creating status bar during shutdown
        log.info("[DisplayManager] Clearing widgets...")
        if board.display_manager:
            try:
                board.display_manager.clear_widgets(addStatusBar=not for_shutdown)
                log.info("[DisplayManager] Widgets cleared from display")
            except Exception as e:
                log.error(f"[DisplayManager] Error clearing widgets: {e}", exc_info=True)
        else:
            log.info("[DisplayManager] No display manager to clear widgets from")
        
        log.info("[DisplayManager] Cleanup complete")
