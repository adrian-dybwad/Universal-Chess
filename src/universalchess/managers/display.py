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
from universalchess.managers.game_layout import compute_clock_analysis_layout
from universalchess.utils.chess_notation import format_move

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
_SetupStatusWidget = None
_CoachTextWidget = None


def _load_widgets():
    """Lazily load widget classes."""
    global _widgets_loaded, _ChessBoardWidget, _GameAnalysisWidget, _ChessClockWidget
    global _IconMenuWidget, _IconMenuEntry, _SplashScreen
    global _GameOverWidget, _AlertWidget, _SetupStatusWidget, _CoachTextWidget
    
    if _widgets_loaded:
        return
    
    from universalchess.epaper import (
        ChessBoardWidget, GameAnalysisWidget, ChessClockWidget,
        IconMenuWidget, IconMenuEntry, SplashScreen,
        AlertWidget, SetupStatusWidget
    )
    from universalchess.epaper.game_over import GameOverWidget
    from universalchess.epaper.coach_text import CoachTextWidget
    _ChessBoardWidget = ChessBoardWidget
    _GameAnalysisWidget = GameAnalysisWidget
    _ChessClockWidget = ChessClockWidget
    _IconMenuWidget = IconMenuWidget
    _IconMenuEntry = IconMenuEntry
    _SplashScreen = SplashScreen
    _GameOverWidget = GameOverWidget
    _AlertWidget = AlertWidget
    _SetupStatusWidget = SetupStatusWidget
    _CoachTextWidget = CoachTextWidget
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

    # Header labels for the shared board-area coach panel, distinguishing a
    # move-review comment from an on-demand hint tip so the reader knows which is
    # which when a tip is shown on top of a review.
    _REVIEW_HEADER = "Coach"
    _TIP_HEADER = "Coach's Tip"

    
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
        # True while a hint's coach statement occupies the board-area coach panel
        # (board hidden). Kept separate from analysis-review's use of the same
        # panel so a played move / ? toggle clears only the hint's panel and
        # restores the board (or the review comment it interrupted). Cleared on
        # alert-clear (a move resolves the hint).
        self._hint_coach_active = False
        # The last move-review statement pushed to the panel. Saved so that a hint
        # tip shown on top of an active review can restore the review comment when
        # the tip is dismissed, rather than only the board.
        self._review_coach_text = ""
        self._game_state.on_alert_clear(self._on_hint_alert_cleared)
        # Move-history notation for the analysis widget's paged move list;
        # refreshed from settings in _reload_display_settings before each rebuild.
        self._notation = "figurine"
        
        # Widgets
        self.chess_board_widget = None
        self.clock_widget = None
        self.analysis_widget = None
        self.coach_text_widget = None
        self._analysis_engine_handle = None  # EngineHandle from registry
        self.alert_widget = None
        self.game_over_widget = None
        # Invoked with the selected ply (or None) so the coach coordinator can
        # lazily fetch/show a statement; set via set_coach_selection_callback.
        self._coach_selection_callback = None
        self.setup_status_widget = None
        
        # Suspend/resume state. The game can be suspended back to the full menu
        # (clock paused, LEDs off) while its managers stay alive; resume() then
        # rebuilds the board and restores LEDs via this callback.
        self._on_resume_callback = None
        
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
        self._notation = Settings.read('game', 'notation', 'figurine')

        # Re-apply the selected chess sprite sheet so the board widget rebuilt by
        # _init_widgets() reflects a sprite change made in the display menu (hot
        # reload). The board widget reads the module-level sprites at construction.
        self._reload_chess_sprites()

        log.info(f"[DisplayManager] Reloaded display settings: board={self._show_board}, "
                 f"clock={self._show_clock}, analysis={self._show_analysis}, "
                 f"graph={self._show_graph}")

    def _reload_chess_sprites(self):
        """Load the sprite sheet selected in settings and set it module-wide.

        No-op if the resource loader is unavailable or the selected sheet cannot
        be loaded (the previously set sprites remain in effect).
        """
        from universalchess.board.settings import Settings
        from universalchess import resources as resources_module
        from universalchess.epaper import chess_board as chess_board_module

        loader = resources_module.get_resource_loader()
        if loader is None:
            return

        selected_sheet = Settings.read('game', 'chess_sprites', loader.DEFAULT_SPRITE_SHEET)
        sprites = loader.get_chess_sprites(selected_sheet)
        if sprites is None and selected_sheet != loader.DEFAULT_SPRITE_SHEET:
            log.warning(f"[DisplayManager] Sprite sheet '{selected_sheet}' not found, using default")
            sprites = loader.get_chess_sprites(loader.DEFAULT_SPRITE_SHEET)
        if sprites is not None:
            chess_board_module.set_chess_sprites(sprites)
            log.info(f"[DisplayManager] Chess sprites set to '{selected_sheet}'")
    
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

        # Store the normal geometry plus the compact move-history geometry so
        # paging can shrink the clock and grow the analysis widget at runtime
        # (see _apply_compact_layout). The compact clock height fits two clock
        # rows in timed mode or a single turn-text line in untimed mode.
        self._clock_y = clock_y
        self._normal_clock_height = clock_height
        self._display_bottom = analysis_y + analysis_height
        self._compact_clock_height = 52 if self._time_control > 0 else 24
        
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
        
        # The clock's tick drives the display heartbeat (flush_now) so the panel
        # refreshes on a steady once-per-second cadence while a timed game runs;
        # other widgets' routine changes are deferred by the Manager and fold
        # into this refresh instead of preempting it.
        self.clock_widget = _ChessClockWidget(
            0, clock_y, 128, clock_height, board.display_manager.update,
            timed_mode=timed_mode, flip=self._flip_board,
            on_tick_refresh=board.display_manager.flush_now
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
                show_graph=self._show_graph,
                notation=self._notation,
                game_state=self._game_state,
            )
            
            if not self._show_analysis:
                self.analysis_widget.hide()
            
            board.display_manager.add_widget(self.analysis_widget)
            log.info(f"[DisplayManager] Analysis widget initialized (visible={self._show_analysis}, graph={self._show_graph})")

            # Coach-text widget occupies the board area; shown while a move is
            # selected (board hidden) and hidden on the analysis view. Created
            # alongside the analysis widget so the selection callback can swap
            # them. Starts hidden.
            self.coach_text_widget = _CoachTextWidget(
                0, 16, 128, 128, board.display_manager.update
            )
            board.display_manager.add_widget(self.coach_text_widget)

            # A move-history selection shrinks the clock and grows the analysis
            # widget (compact layout), hides the board, and shows the coach text
            # for the selected ply; the analysis view (selection 0) restores the
            # full layout and the board. Both widgets are recreated together on a
            # settings rebuild, so this wires the current pair each time.
            self.analysis_widget.set_selection_change_callback(
                self._on_analysis_selection_change
            )
        else:
            self.analysis_widget = None
            self.coach_text_widget = None
            log.info("[DisplayManager] Analysis mode disabled - no analysis widget created")
        
        # Create alert widget for CHECK/QUEEN warnings (y=144, overlays clock widget)
        # Alert widget is hidden by default and shown when check or queen threat occurs
        self.alert_widget = _AlertWidget(
            0, 144, 128, 40, board.display_manager.update,
            led_from_to_hint_callback=self._led_from_to_hint
        )
        # Alerts (check/queen/hint) are time-sensitive: they must appear at once
        # rather than waiting for the next clock tick, so they refresh immediately
        # even while the clock is the sole refresher for routine updates.
        self.alert_widget.refresh_priority = True
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
        # Game over is a terminal, time-sensitive result screen: refresh at once
        # (the clock is stopped on game over, but this keeps the panel current
        # even before the defer-to-clock flag is cleared).
        self.game_over_widget.refresh_priority = True
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
            # Format the hint in the selected notation. format_move needs the
            # position before the move; the hint is for the current position, so
            # the authoritative game board is that position and also gives the
            # mover's color for figurine piece art.
            board_obj = self._game_state.board
            move_text = format_move(board_obj, move, self._notation)
            white_side = board_obj.turn == chess.WHITE
            from_sq = move.from_square
            to_sq = move.to_square
            
            self.alert_widget.show_hint(move_text, from_sq, to_sq, white_side=white_side)
            log.info(f"[DisplayManager] Showing hint: {move_text}")

    def is_hint_showing(self) -> bool:
        """True when a move hint is currently displayed on the alert widget."""
        return self.alert_widget is not None and self.alert_widget.is_showing_hint()

    def hide_hint(self) -> None:
        """Hide a currently displayed move hint (the ? key toggles it off).

        Only hides when a hint is showing, so a concurrent CHECK/QUEEN alert is
        never dismissed by the ? key. Also clears the hint's coach panel so the
        board is restored together with the hint move.
        """
        if self.alert_widget is not None and self.alert_widget.is_showing_hint():
            self.alert_widget.hide()
            self.hide_hint_coach()
            log.info("[DisplayManager] Hint toggled off")

    def show_hint_coach(self, text: str) -> None:
        """Show a hinted move's coach statement in the board-area coach panel.

        Reuses the coach-text panel (also used for move review) to display the
        coaching remark for the hinted move, hiding the chess board while shown -
        the physical board still shows the move via the hint LEDs and the alert
        strip still shows the move text. The panel header is switched to the tip
        label so it reads clearly as the hint's tip, distinct from a move-review
        comment. It is shown even while a move is selected in the analysis review
        (which shares the panel): the review comment is saved and restored when
        the tip is dismissed. Thread-safe: the text blit and show are display-safe
        and this is called from the coach worker thread.
        """
        if not text or self.coach_text_widget is None:
            return
        self._hint_coach_active = True
        self.coach_text_widget.set_header(self._TIP_HEADER)
        self.coach_text_widget.set_text(text)
        if self.chess_board_widget is not None:
            self.chess_board_widget.hide()
        self.coach_text_widget.show()

    def hide_hint_coach(self) -> None:
        """Hide the hint's coach panel, restoring the board or review comment.

        No-op unless a hint coach panel is active. When an analysis-review
        selection is active (owns the same panel), the saved review comment is
        restored under the review header and the board stays hidden. Otherwise
        the panel is hidden and the board is restored (subject to the board being
        enabled in settings).
        """
        if not self._hint_coach_active:
            return
        self._hint_coach_active = False
        review_active = (
            self.analysis_widget is not None
            and self.analysis_widget.selected_ply() is not None
        )
        if review_active:
            # Restore the review comment that the tip interrupted; the review owns
            # the hidden-board layout, so the panel stays visible.
            if self.coach_text_widget is not None:
                self.coach_text_widget.set_header(self._REVIEW_HEADER)
                self.coach_text_widget.set_text(self._review_coach_text)
                self.coach_text_widget.show()
            return
        if self.coach_text_widget is not None:
            self.coach_text_widget.set_header(self._REVIEW_HEADER)
            self.coach_text_widget.hide()
        if self._show_board and self.chess_board_widget is not None:
            self.chess_board_widget.show()

    def _on_hint_alert_cleared(self) -> None:
        """Clear the hint coach panel when the alert is cleared (e.g. a move made).

        The alert widget hides itself on this same event; mirroring it here keeps
        the hint's coach panel from lingering over the board after the hinted
        position is left.
        """
        self.hide_hint_coach()

    def step_analysis_selection(self, direction: int) -> bool:
        """Step the analysis widget's move selection via the UP/DOWN keys.

        Selection 0 is the eval/graph view; 1..N select an individual played move
        (ply), whose row is highlighted while the board area is replaced by that
        move's coach statement. UP (direction -1) and DOWN (direction +1) wrap
        around, so stepping past the last move returns to the analysis view and
        restores the board. No-op (returns False) when there is no analysis
        widget or it is hidden, so the caller can fall back to normal key routing.

        Args:
            direction: -1 to step up, +1 to step down.

        Returns:
            True if the key was consumed by the selection, False otherwise.
        """
        if self.analysis_widget is None or not self.analysis_widget.visible:
            return False
        self.analysis_widget.step_selection(direction)
        return True

    def set_coach_selection_callback(self, callback) -> None:
        """Register a callback invoked with the selected ply (or None).

        The coach coordinator uses this to lazily fetch/persist and display the
        coach statement for the selected move. Called with the 1-based ply when a
        move is selected and with None when the analysis view is selected.
        """
        self._coach_selection_callback = callback

    def _on_analysis_selection_change(self, selection: int) -> None:
        """Swap board/coach and clock layout when the analysis selection changes.

        selection 0 (analysis view): restore the full clock layout and the chess
        board, hide the coach text. A selected ply: shrink the clock / grow the
        move list, hide the board, show the coach-text panel, and notify the
        coach coordinator so it resolves the statement for that ply.
        """
        self._apply_compact_layout(selection != 0)

        ply = self.analysis_widget.selected_ply() if self.analysis_widget else None
        coach = self.coach_text_widget
        if ply is None:
            if coach is not None:
                coach.hide()
            # Only restore the board if the board is enabled in settings.
            if self._show_board and self.chess_board_widget is not None:
                self.chess_board_widget.show()
        else:
            if self.chess_board_widget is not None:
                self.chess_board_widget.hide()
            if coach is not None:
                coach.show()

        if self._coach_selection_callback is not None:
            self._coach_selection_callback(ply)

    def set_coach_text(self, text: str) -> None:
        """Push a move-review coach statement to the coach-text panel.

        The text is recorded so a hint tip shown on top of the review can restore
        it on dismiss. While a hint tip occupies the panel the text is only
        recorded (not blitted) so a late-arriving review result does not overwrite
        the visible tip; it is restored when the tip is hidden. Thread-safe blit.
        """
        self._review_coach_text = text
        if self._hint_coach_active:
            return
        if self.coach_text_widget is not None:
            self.coach_text_widget.set_header(self._REVIEW_HEADER)
            self.coach_text_widget.set_text(text)

    def _apply_compact_layout(self, compact: bool) -> None:
        """Resize the clock and analysis widgets for the compact page layout.

        Called on every analysis page change: when a move-history page is shown
        (compact=True) the clock shrinks and the analysis widget grows into the
        reclaimed space so more moves fit; the analysis page (compact=False)
        restores the full layout.

        Only geometry is mutated here and caches are invalidated -- no refresh is
        requested. The refresh is driven by the analysis widget's own
        page-change update (turn_page/clamp call invalidate_and_update right
        after this callback), so paging performs a single coordinated redraw that
        re-renders both resized widgets.
        """
        clock = self.clock_widget
        analysis = self.analysis_widget
        if clock is None or analysis is None:
            return

        layout = compute_clock_analysis_layout(
            compact=compact,
            clock_y=self._clock_y,
            normal_clock_height=self._normal_clock_height,
            compact_clock_height=self._compact_clock_height,
            display_bottom=self._display_bottom,
        )

        if clock.height != layout.clock_height:
            clock.height = layout.clock_height
            clock.invalidate_cache()

        if analysis.y != layout.analysis_y or analysis.height != layout.analysis_height:
            analysis.y = layout.analysis_y
            analysis.height = layout.analysis_height
            analysis.invalidate_cache()

        # Untimed clock also drops its indicator circle when compact. Suppress its
        # own refresh; the analysis page-change update repaints both widgets.
        clock.set_hide_turn_indicator(compact, refresh=False)

    def set_clock_times(self, white_seconds: int, black_seconds: int) -> None:
        """Set the chess clock times for both players.
        
        Args:
            white_seconds: White's time in seconds
            black_seconds: Black's time in seconds
        """
        self._clock.set_times(white_seconds, black_seconds)
    
    def _sync_clock_refresh_mode(self) -> None:
        """Point the Manager at clock-driven refresh iff the clock is counting.

        Clock-driven mode (routine widget updates only mark the framebuffer dirty
        and ride the clock's tick) is correct only while a timed game's clock is
        actually counting -- i.e. running and not paused, so a tick is guaranteed
        to flush the deferred content. When the clock is paused, stopped, or the
        game is untimed, no tick fires, so routine updates must refresh normally
        (coalesced) to stay responsive. Called after every clock transition; the
        Manager flushes any pending content when the mode turns off, so pausing
        never leaves the screen stale.
        """
        if not board.display_manager:
            return
        counting = (self._time_control > 0
                    and self._clock.is_running
                    and not self._clock.is_paused)
        board.display_manager.set_defer_to_clock(counting)

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
            self._sync_clock_refresh_mode()
    
    def pause_clock(self) -> None:
        """Pause the chess clock."""
        self._clock.pause()
        self._sync_clock_refresh_mode()
    
    def stop_clock(self) -> None:
        """Stop the chess clock completely."""
        self._clock.stop()
        self._sync_clock_refresh_mode()
    
    def reset_clock(self) -> None:
        """Reset the chess clock to initial time and stop it.
        
        Called when a new game starts to reset clock state.
        The clock will not start until the first move is made.
        """
        self._clock.reset()
        self._sync_clock_refresh_mode()
        log.info(f"[DisplayManager] Clock reset to {self._time_control} min per player")
    
    def suspend(self) -> None:
        """Suspend the running game so the full menu can take over the screen.

        Pauses the clock and turns LEDs off, but shows no overlay widget - the
        caller replaces the screen with the full menu. The game managers stay
        alive so resume() can restore the board and clock. This replaces the old
        in-place pause overlay; "suspended" state is tracked by the caller (the
        game managers being alive while the menu shows), not by this manager.
        """
        self._clock.pause()
        self._sync_clock_refresh_mode()
        if self._led_off:
            self._led_off()
        else:
            log.warning("[DisplayManager] LED off callback not set, skipping LED off")
        log.info("[DisplayManager] Game suspended")

    def resume(self) -> None:
        """Resume a suspended game and rebuild the game screen.

        Recreates the board/clock/analysis widgets via _init_widgets() (which
        performs a full refresh to clear menu ghosting), resumes the clock for
        the previously active player, and restores LEDs via the resume callback.
        """
        self._init_widgets()
        self._clock.resume()
        self._sync_clock_refresh_mode()
        if self._on_resume_callback:
            try:
                self._on_resume_callback()
            except Exception as e:
                log.warning(f"[DisplayManager] Error in resume callback: {e}")
        log.info("[DisplayManager] Game resumed")
    
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
                except Exception as e:
                    # Rendering the promotion menu is best-effort; a timeout or
                    # render failure must not block the move flow. Log for
                    # diagnostics rather than surfacing to the player.
                    log.debug(f"[DisplayManager] Promotion menu render wait failed: {e}")
        
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
    
    def _finalize_menu_selection(self, menu, *, shutdown_result: str) -> None:
        """Finalize a non-blocking game menu after the user makes a selection.

        Shared completion path for the BACK menu and the king-lift resign menu.
        Deactivates the menu, maps the special BACK/SHUTDOWN keys to result
        strings, rebuilds the board, then invokes the stored result callback.

        The board is rebuilt for every outcome that returns to the board (all
        results except the shutdown "exit"), and the rebuild happens BEFORE the
        result callback runs. This ordering is the fix for menu-driven game
        endings: a resign/draw selection sets the game result inside its
        callback, and the rebuilt board carries a GameOverWidget freshly
        subscribed to the game state, so it catches the game_over event and
        shows the end-of-game screen over the board - mirroring the natural
        checkmate flow (where the board, and its GameOverWidget, is already on
        screen). If the board were not rebuilt first (the previous behaviour,
        which restored it only for "cancel"), resign/draw would set the result
        while no GameOverWidget existed, so the end screen never appeared.

        Args:
            menu: The active menu widget that produced the selection.
            shutdown_result: Result to report when the SHUTDOWN key was pressed.
                "exit" powers the device off (BACK menu); "cancel" ignores it
                (king-lift resign menu, which must never trigger a shutdown).
        """
        result = menu._selection_result or "BACK"
        log.info(f"[DisplayManager] Menu result: {result}")

        self._menu_active = False
        menu.deactivate()
        self._current_menu = None

        if result == "BACK":
            result = "cancel"
        elif result == "SHUTDOWN":
            result = shutdown_result

        # Rebuild the board (and its GameOverWidget) before the callback for
        # every on-board outcome; only a shutdown skips it. See docstring.
        if result != "exit":
            self._init_widgets()

        if self._menu_result_callback:
            self._menu_result_callback(result)

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
                # SHUTDOWN powers the device off from the BACK menu.
                self._finalize_menu_selection(back_menu, shutdown_result="exit")
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
                # This menu must never power the device off, so SHUTDOWN is
                # treated as cancel rather than exit.
                self._finalize_menu_selection(resign_menu, shutdown_result="cancel")
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
                # Fill the whole screen (no status-bar gap); the status bar was
                # not added above, so the splash covers the full 296px height.
                splash = _SplashScreen(
                    board.display_manager.update,
                    message=message,
                    leave_room_for_status_bar=False,
                )
                future = board.display_manager.add_widget(splash)
                if future:
                    try:
                        future.result(timeout=2.0)
                    except Exception as e:
                        # Splash render is best-effort; a timeout or render
                        # failure must not block startup/shutdown flow.
                        log.debug(f"[DisplayManager] Splash render wait failed: {e}")
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
        
        # Unsubscribe the hint-coach alert-clear observer so a rebuilt manager
        # does not accumulate stale observers on the game-state singleton.
        try:
            self._game_state.remove_observer(self._on_hint_alert_cleared)
        except Exception as e:
            log.debug(f"[DisplayManager] Error removing hint-coach observer: {e}")

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
