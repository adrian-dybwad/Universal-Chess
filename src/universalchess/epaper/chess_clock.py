"""
Chess clock widget displaying game time for both players.

Layout:
- Timed mode: Remaining time for white and black players with turn indicator
- Untimed mode (compact): Just "White Turn" or "Black Turn" text

Turn indicator comes from ChessGameState (single source of truth for whose turn).
Time comes from ChessClockState (manages countdown).
"""

from PIL import Image, ImageDraw
from .framework.widget import Widget
from .text import TextWidget, Justify
from typing import Optional

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from universalchess.state import get_chess_clock as get_clock_state
from universalchess.state import get_chess_game as get_game_state
from universalchess.state.players import get_players_state
from universalchess.state.time_control import DelayMode, TimeControl


def format_increment_delay(time_control: TimeControl, color: str) -> str:
    """Compact per-side increment/delay annotation for the clock display.

    Summarizes the active control for one side so the board shows it inline next
    to the clock: ``"+N"`` for a Fischer increment, ``"dN"`` for a simple/US
    delay, ``"bN"`` for a Bronstein delay, combined with a space when both apply.
    Returns ``""`` for an untimed game or plain sudden death (nothing to annotate).

    Kept pure (derives only from the TimeControl) so it is unit-testable
    independently of the e-paper rendering that positions it. The increment is
    read per-side to stay correct for asymmetric / time-odds controls.

    Args:
        time_control: The resolved control for the game.
        color: 'white' or 'black' -- the side being drawn.

    Returns:
        The annotation string (possibly empty).
    """
    if not time_control.is_timed:
        return ""
    parts: list[str] = []
    increment = time_control.increment_after_move(color, 1)
    if increment:
        parts.append(f"+{increment}")
    if time_control.delay_seconds:
        if time_control.delay_mode is DelayMode.SIMPLE:
            parts.append(f"d{time_control.delay_seconds}")
        elif time_control.delay_mode is DelayMode.BRONSTEIN:
            parts.append(f"b{time_control.delay_seconds}")
    return " ".join(parts)


class ChessClockWidget(Widget):
    """Widget displaying chess clock times or turn indicator.
    
    Has two display modes:
    - Timed mode: Shows remaining time for both players with turn indicator
    - Compact/untimed mode: Shows "White Turn" or "Black Turn" centered
    """
    
    # Position directly below the board (board is at y=16, height=128)
    DEFAULT_Y = 144
    DEFAULT_HEIGHT = 72
    
    def __init__(self, x: int, y: int, width: int, height: int, update_callback,
                 timed_mode: bool = True, flip: bool = False,
                 on_tick_refresh: Optional[callable] = None):
        """Initialize chess clock widget.
        
        Args:
            x: X position
            y: Y position
            width: Widget width
            height: Widget height
            update_callback: Callback to trigger display updates. Must not be None.
            timed_mode: Whether to show times (True) or just turn indicator (False)
            flip: If True, show Black on top (matching flipped board perspective)
            on_tick_refresh: Optional heartbeat callback invoked once per clock
                tick to drive a single full-stack refresh (the Manager's
                flush_now). When wired, the tick is the sole panel refresher
                while the clock runs, giving a steady cadence; other widgets'
                routine changes are deferred by the Manager and fold into it.
                When None (e.g. tests, untimed play), ticks fall back to the
                normal deferred update path.
        """
        super().__init__(x, y, width, height, update_callback)
        self._timed_mode = timed_mode
        self._flip = flip
        self._on_tick_refresh = on_tick_refresh
        # Compact turn indicator, driven by the analysis widget's paging so
        # move-history pages show a smaller clock; restored on the analysis page.
        # Untimed mode: the large turn-indicator circle is omitted and the
        # "…'s Turn" text is pulled to the top. Timed mode: the two clock sections
        # (circles + times kept) are halved and packed into the top of the widget.
        # See set_hide_turn_indicator.
        self._hide_turn_circle = False
        
        # Clock state for time management
        self._clock = get_clock_state()
        self._clock.on_state_change(self._on_clock_state_change)
        self._clock.on_tick(self._on_clock_tick)
        
        # Game state for turn indicator and game over detection
        self._game = get_game_state()
        self._game.on_position_change(self._on_game_state_change)
        self._game.on_game_over(self._on_game_over)
        
        # Players state for names (observes player swaps)
        self._players = get_players_state()
        self._players.on_names_change(self._on_player_names_change)
        
        self._on_flag_callback: Optional[callable] = None
        
        # Create TextWidgets for timed mode - use parent handler for child updates
        # White label (left aligned, after indicator)
        self._white_label = TextWidget(20, 0, 40, 16, self._handle_child_update,
                                        text="White", font_size=10, 
                                        justify=Justify.LEFT, transparent=True)
        # White player name (smaller, under the label)
        self._white_name_text = TextWidget(20, 0, 60, 12, self._handle_child_update,
                                           text="", font_size=8,
                                           justify=Justify.LEFT, transparent=True)
        # White time (right aligned)
        self._white_time_text = TextWidget(60, 0, 64, 20, self._handle_child_update,
                                           text="00:00", font_size=16,
                                           justify=Justify.RIGHT, transparent=True)
        # Black label
        self._black_label = TextWidget(20, 0, 40, 16, self._handle_child_update,
                                       text="Black", font_size=10,
                                       justify=Justify.LEFT, transparent=True)
        # Black player name (smaller, under the label)
        self._black_name_text = TextWidget(20, 0, 60, 12, self._handle_child_update,
                                           text="", font_size=8,
                                           justify=Justify.LEFT, transparent=True)
        # Black time
        self._black_time_text = TextWidget(60, 0, 64, 20, self._handle_child_update,
                                           text="00:00", font_size=16,
                                           justify=Justify.RIGHT, transparent=True)
        # Increment/delay annotation (small, right-aligned under each time). Shows
        # "+3"/"d3"/"b3" for the active control, or the live simple-delay
        # countdown for the side on move; blank for plain sudden death/untimed.
        self._white_annot_text = TextWidget(60, 0, 64, 10, self._handle_child_update,
                                            text="", font_size=8,
                                            justify=Justify.RIGHT, transparent=True)
        self._black_annot_text = TextWidget(60, 0, 64, 10, self._handle_child_update,
                                            text="", font_size=8,
                                            justify=Justify.RIGHT, transparent=True)
        
        # Create TextWidgets for compact mode
        # Turn indicator text (color)
        self._turn_text = TextWidget(0, 0, width, 20, self._handle_child_update,
                                     text="White's Turn", font_size=16,
                                     justify=Justify.CENTER, transparent=True)
        # Player name text (below turn indicator)
        self._turn_name_text = TextWidget(0, 0, width, 14, self._handle_child_update,
                                          text="", font_size=10,
                                          justify=Justify.CENTER, transparent=True)
        
        # Track last state to avoid unnecessary updates
        self._last_white_time = None
        self._last_black_time = None
        self._last_active = None
        self._last_timed_mode = None
        
        # Hand-brain hints: piece letter to display for each player
        # When set, shows the letter to the left of the timer (replacing indicator)
        self._white_brain_hint: str = ""
        self._black_brain_hint: str = ""
        
        # TextWidgets for brain hints (large letter)
        self._white_hint_text = TextWidget(0, 0, 20, 20, self._handle_child_update,
                                           text="", font_size=16,
                                           justify=Justify.CENTER, transparent=True)
        self._black_hint_text = TextWidget(0, 0, 20, 20, self._handle_child_update,
                                           text="", font_size=16,
                                           justify=Justify.CENTER, transparent=True)
    
    def _handle_child_update(self, full: bool = False, immediate: bool = False):
        """No-op update callback for the clock's render-only child text widgets.

        The label/name/time/brain-hint TextWidgets are not autonomous: their
        set_text() is called only from within this widget's own render(), which
        already draws the new text into the clock sprite. TextWidget.set_text()
        calls request_update() on a change; forwarding that to the Manager fired a
        re-entrant second display refresh per clock tick (Manager defers the
        re-entrant update and replays it), so on the slow e-paper panel every tick
        produced two full-screen refreshes -- saturating the panel and making the
        once-per-second clock drift/beat (the erratic cadence). Returning None
        keeps set_text()'s cache invalidation (so the child re-renders with the
        new text on the next draw_on) while suppressing the redundant refresh. The
        clock drives its own single refresh from its tick/state/game observers.
        """
        return None
    
    def _on_clock_state_change(self) -> None:
        """Called when ChessClockState changes (times, running state)."""
        self.invalidate_and_update()
    
    def _on_clock_tick(self) -> None:
        """Called every second when the clock is running.

        The tick is the display heartbeat while a timed game runs. It invalidates
        this widget's cache first (so the new time re-renders), then drives a
        single full-stack refresh via the heartbeat callback. Routing the
        once-per-second refresh through here -- rather than each widget refreshing
        on its own change -- is what gives the clock a steady cadence: other
        widgets' routine changes are deferred by the Manager and folded into this
        same refresh, and no move/engine event preempts the beat. Falls back to
        the normal deferred/coalesced path when no heartbeat is wired.
        """
        if self._on_tick_refresh is not None:
            self.invalidate_cache()
            if self.visible:
                self._on_tick_refresh()
            return
        self.invalidate_and_update()
    
    def _on_game_state_change(self) -> None:
        """Called when ChessGameState changes (turn changes after moves).
        
        Also handles showing clock when a new game starts after game over.
        If game was over (widget hidden) and now is_game_over is False,
        the widget shows itself for the new game.
        """
        # If game is no longer over and we're hidden, show ourselves
        if not self._game.is_game_over and not self.visible:
            log.debug("[ChessClockWidget] Game reset detected - showing clock")
            self.show()
        
        self.invalidate_and_update()

    def _on_game_over(self, result: str, termination: str) -> None:
        """Called when game ends (checkmate, resignation, flag, etc.).
        
        Hides the clock widget so the game over display can be shown.
        
        Args:
            result: Game result ('1-0', '0-1', '1/2-1/2').
            termination: How game ended ('checkmate', 'resignation', etc.).
        """
        log.debug(f"[ChessClockWidget] Game over ({result}, {termination}) - hiding clock")
        self.hide()

    def _on_player_names_change(self, white_name: str, black_name: str) -> None:
        """Called when PlayersState names change (player swap).
        
        Args:
            white_name: New white player name.
            black_name: New black player name.
        """
        self.invalidate_and_update()

    def stop(self) -> None:
        """Called when widget is removed from display.
        
        Unregisters callbacks from state objects. Does NOT stop the clock -
        the service continues running independently.
        """
        self._clock.remove_observer(self._on_clock_state_change)
        self._clock.remove_observer(self._on_clock_tick)
        self._game.remove_observer(self._on_game_state_change)
        self._game.remove_observer(self._on_game_over)
        self._players.remove_observer(self._on_player_names_change)
        log.debug("[ChessClockWidget] Unregistered from state observers")
    
    # -------------------------------------------------------------------------
    # Properties (read from ChessClock)
    # -------------------------------------------------------------------------
    
    @property
    def timed_mode(self) -> bool:
        """Whether the widget is in timed mode (showing clocks) or compact mode."""
        return self._timed_mode
    
    @timed_mode.setter
    def timed_mode(self, value: bool) -> None:
        """Set timed mode."""
        if self._timed_mode != value:
            self._timed_mode = value
            self.invalidate_and_update()

    @property
    def hide_turn_indicator(self) -> bool:
        """Whether the turn-indicator color circle is currently hidden."""
        return self._hide_turn_circle

    def set_hide_turn_indicator(self, hide: bool, refresh: bool = True) -> None:
        """Enable or disable the compact turn indicator (move-history paging).

        Compact makes the clock take less room while the board shows a
        move-history page; the analysis page restores the normal layout. In
        untimed mode compact omits the large color circle and centers the turn
        text in the (DisplayManager-shrunk) widget. In timed mode the sections
        simply shrink with the widget -- the circles and times are kept.

        No-op when unchanged. When it does change the sprite cache is always
        invalidated; ``refresh`` controls whether a display refresh is also
        requested. DisplayManager passes ``refresh=False`` because it resizes
        both the clock and analysis widgets together and lets the analysis
        widget's page-change update drive a single coordinated redraw.

        Args:
            hide: True for the compact indicator, False for the normal layout.
            refresh: If True, request a display refresh after the change.
        """
        if self._hide_turn_circle != hide:
            self._hide_turn_circle = hide
            self.invalidate_cache()
            if refresh:
                self.request_update()
    
    @property
    def white_time(self) -> int:
        """White's remaining time in seconds (from service)."""
        return self._clock.white_time
    
    @property
    def black_time(self) -> int:
        """Black's remaining time in seconds (from service)."""
        return self._clock.black_time
    
    @property
    def active_color(self) -> Optional[str]:
        """Which player's turn it is (from game state - single source of truth)."""
        return self._game.turn_name
    
    @property
    def white_name(self) -> str:
        """White player's name (from PlayersState)."""
        return self._players.white_name
    
    @property
    def black_name(self) -> str:
        """Black player's name (from PlayersState)."""
        return self._players.black_name
    
    @property
    def on_flag(self) -> Optional[callable]:
        """Callback for when time expires."""
        return self._on_flag_callback
    
    @on_flag.setter
    def on_flag(self, callback: Optional[callable]) -> None:
        """Set flag callback. Registers with ChessClock."""
        self._on_flag_callback = callback
        if callback:
            self._clock.on_flag(callback)
    
    # -------------------------------------------------------------------------
    # Hand-Brain hints
    # -------------------------------------------------------------------------
    
    def set_brain_hint(self, color: str, piece_letter: str) -> None:
        """Set the brain hint piece letter for a player.

        In hand-brain mode, shows the suggested piece type to the left of
        that player's clock timer. The turn indicator circle remains visible.
        The hint may overlap the player name label.

        Args:
            color: 'white' or 'black'
            piece_letter: Single letter (K, Q, R, B, N, P) or empty to clear
        """
        hint = piece_letter.upper() if piece_letter else ""
        
        if color == 'white':
            if self._white_brain_hint != hint:
                self._white_brain_hint = hint
                self.invalidate_and_update()
        elif color == 'black':
            if self._black_brain_hint != hint:
                self._black_brain_hint = hint
                self.invalidate_and_update()
    
    def clear_brain_hint(self, color: str) -> None:
        """Clear the brain hint for a player.
        
        Args:
            color: 'white' or 'black'
        """
        self.set_brain_hint(color, "")
    
    # -------------------------------------------------------------------------
    # Rendering
    # -------------------------------------------------------------------------
    
    def _format_time(self, seconds: int) -> str:
        """
        Format time in seconds to display string.
        
        Returns MM:SS for times under an hour, H:MM:SS for longer times.
        Returns "FLAG" if time is 0.
        
        Args:
            seconds: Time in seconds
            
        Returns:
            Formatted time string
        """
        if seconds <= 0:
            return "FLAG"
        
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes:02d}:{secs:02d}"
    
    def _annotation_for(self, color: str, active_color: Optional[str]) -> str:
        """Annotation string for one side's clock (increment/delay or live delay).

        For the side on the move in SIMPLE (US) delay mode with delay still
        remaining, shows the live delay countdown (``"delay N"``) so the frozen
        main clock has visible feedback; otherwise the static increment/delay
        summary from :func:`format_increment_delay`.
        """
        time_control = self._clock.time_control
        if (color == active_color
                and time_control.delay_mode is DelayMode.SIMPLE
                and self._clock.delay_remaining > 0):
            return f"delay {self._clock.delay_remaining}"
        return format_increment_delay(time_control, color)

    def render(self, sprite: Image.Image) -> None:
        """
        Render the chess clock widget onto the sprite image.
        
        Reads state from ChessClock and renders it.
        """
        draw = ImageDraw.Draw(sprite)
        
        # Draw background
        self.draw_background_on_sprite(sprite)
        
        # Draw 1px border around widget extent
        draw.rectangle([(0, 0), (self.width - 1, self.height - 1)], fill=None, outline=0)
        
        if self._timed_mode:
            self._render_timed_mode(sprite, draw)
        else:
            self._render_compact_mode(sprite, draw)
    
    def _render_timed_mode(self, sprite: Image.Image, draw: ImageDraw.Draw) -> None:
        """
        Render timed mode: stacked layout matching board orientation.
        
        Reads times and active color from ChessClock.
        """
        # Two equal sections fill the widget, split at the middle. The compact
        # move-history layout is produced by DisplayManager shrinking the widget
        # height (not by repacking here), so the sections stay centered and just
        # get shorter. Player names are drawn only when a section is tall enough
        # to fit a name line under the color label.
        section_height = (self.height - 4) // 2  # -4 for top/middle separators
        top_y = 4
        separator_y = self.height // 2
        bottom_y = separator_y + 4
        show_names = section_height >= 28
        
        # Get times from clock state, turn from game state, names from players state
        white_time = self._clock.white_time
        black_time = self._clock.black_time
        active_color = self._game.turn_name  # Single source of truth for turn
        white_name = self._players.white_name
        black_name = self._players.black_name
        
        # Determine which color goes on top based on flip setting
        if self._flip:
            top_color = 'white'
            bottom_color = 'black'
            top_label = self._white_label
            bottom_label = self._black_label
            top_name_widget = self._white_name_text
            bottom_name_widget = self._black_name_text
            top_name = white_name
            bottom_name = black_name
            top_time_widget = self._white_time_text
            bottom_time_widget = self._black_time_text
            top_time = white_time
            bottom_time = black_time
            top_brain_hint = self._white_brain_hint
            bottom_brain_hint = self._black_brain_hint
            top_hint_widget = self._white_hint_text
            bottom_hint_widget = self._black_hint_text
            top_annot_widget = self._white_annot_text
            bottom_annot_widget = self._black_annot_text
        else:
            top_color = 'black'
            bottom_color = 'white'
            top_label = self._black_label
            bottom_label = self._white_label
            top_name_widget = self._black_name_text
            bottom_name_widget = self._white_name_text
            top_name = black_name
            bottom_name = white_name
            top_time_widget = self._black_time_text
            bottom_time_widget = self._white_time_text
            top_time = black_time
            bottom_time = white_time
            top_brain_hint = self._black_brain_hint
            bottom_brain_hint = self._white_brain_hint
            top_hint_widget = self._black_hint_text
            bottom_hint_widget = self._white_hint_text
            top_annot_widget = self._black_annot_text
            bottom_annot_widget = self._white_annot_text
        
        # === TOP SECTION ===
        # Turn indicator circle (always drawn). In timed mode the circle is the
        # only turn cue next to each clock, so it is kept even on move-history
        # pages; compact mode shrinks the sections but keeps the circles.
        indicator_size = 12
        indicator_y = top_y + (section_height - indicator_size) // 2
        if active_color == top_color:
            draw.ellipse([(4, indicator_y), (4 + indicator_size, indicator_y + indicator_size)], 
                        fill=0, outline=0)
        else:
            draw.ellipse([(4, indicator_y), (4 + indicator_size, indicator_y + indicator_size)], 
                        fill=255, outline=0)
        
        # Top label using TextWidget - draw directly onto sprite
        top_label.draw_on(sprite, 20, top_y)
        
        # Top player name (if set) - drawn below the color label. Dropped when
        # the section is too short (compact layout) to fit a name line.
        if top_name and show_names:
            display_name = top_name[:10] if len(top_name) > 10 else top_name
            top_name_widget.set_text(display_name)
            top_name_widget.draw_on(sprite, 20, top_y + 12)
        
        # Brain hint letter (to the left of clock, may overlap player name)
        if top_brain_hint:
            top_hint_widget.set_text(top_brain_hint)
            top_hint_widget.draw_on(sprite, self.width - 88, top_y + 4)
        
        # Top time using TextWidget - draw directly onto sprite
        top_time_widget.set_text(self._format_time(top_time))
        top_time_widget.draw_on(sprite, self.width - 68, top_y + 6)
        
        # Increment/delay annotation under the time (only when the section is
        # tall enough; compact move-history layout drops it to avoid overlap).
        top_annot = self._annotation_for(top_color, active_color)
        if top_annot and show_names:
            top_annot_widget.set_text(top_annot)
            top_annot_widget.draw_on(sprite, self.width - 68, top_y + 24)
        
        # Horizontal separator (between the two sections)
        draw.line([(0, separator_y), (self.width, separator_y)], fill=0, width=1)
        
        # === BOTTOM SECTION ===
        # Turn indicator circle (always drawn; see top-section note).
        indicator_y = bottom_y + (section_height - indicator_size) // 2
        if active_color == bottom_color:
            draw.ellipse([(4, indicator_y), (4 + indicator_size, indicator_y + indicator_size)], 
                        fill=0, outline=0)
        else:
            draw.ellipse([(4, indicator_y), (4 + indicator_size, indicator_y + indicator_size)], 
                        fill=255, outline=0)
        
        # Bottom label using TextWidget - draw directly onto sprite
        bottom_label.draw_on(sprite, 20, bottom_y)
        
        # Bottom player name (if set) - drawn below the color label. Dropped when
        # the section is too short (compact layout) to fit a name line.
        if bottom_name and show_names:
            display_name = bottom_name[:10] if len(bottom_name) > 10 else bottom_name
            bottom_name_widget.set_text(display_name)
            bottom_name_widget.draw_on(sprite, 20, bottom_y + 12)
        
        # Brain hint letter (to the left of clock, may overlap player name)
        if bottom_brain_hint:
            bottom_hint_widget.set_text(bottom_brain_hint)
            bottom_hint_widget.draw_on(sprite, self.width - 88, bottom_y + 4)
        
        # Bottom time using TextWidget - draw directly onto sprite
        bottom_time_widget.set_text(self._format_time(bottom_time))
        bottom_time_widget.draw_on(sprite, self.width - 68, bottom_y + 6)
        
        # Increment/delay annotation under the time (see top-section note).
        bottom_annot = self._annotation_for(bottom_color, active_color)
        if bottom_annot and show_names:
            bottom_annot_widget.set_text(bottom_annot)
            bottom_annot_widget.draw_on(sprite, self.width - 68, bottom_y + 24)
    
    # Height of the turn-text line (matches self._turn_text height), used to
    # vertically center the text in the compact (circle-less) layout.
    _TURN_TEXT_HEIGHT = 20

    def _render_compact_mode(self, sprite: Image.Image, draw: ImageDraw.Draw) -> None:
        """
        Render compact mode: large centered turn indicator.
        
        Reads active color from clock state.
        """
        # Get turn from game state, names from players state
        active_color = self._game.turn_name
        white_name = self._players.white_name
        black_name = self._players.black_name
        
        # Determine text and player name
        if active_color == 'black':
            turn_text = "Black's Turn"
            player_name = black_name
        else:
            # Default to white if None or 'white'
            turn_text = "White's Turn"
            player_name = white_name

        # Compact move-history layout: DisplayManager has shrunk the widget, so
        # drop the circle and player name and center just the turn text in the
        # reduced height.
        if self._hide_turn_circle:
            self._turn_text.set_text(turn_text)
            text_y = max(0, (self.height - self._TURN_TEXT_HEIGHT) // 2)
            self._turn_text.draw_on(sprite, 0, text_y)
            return

        # Large indicator circle at top center.
        indicator_size = 28
        indicator_x = (self.width - indicator_size) // 2
        indicator_y = 8

        if active_color == 'black':
            # Filled circle for black
            draw.ellipse([(indicator_x, indicator_y), 
                         (indicator_x + indicator_size, indicator_y + indicator_size)], 
                        fill=0, outline=0)
        else:
            # Empty circle for white (with thick border)
            draw.ellipse([(indicator_x, indicator_y), 
                         (indicator_x + indicator_size, indicator_y + indicator_size)], 
                        fill=255, outline=0, width=2)
        text_y = indicator_y + indicator_size + 4

        # Turn text below indicator using TextWidget
        self._turn_text.set_text(turn_text)
        self._turn_text.draw_on(sprite, 0, text_y)
        
        # Player name below turn text (if set)
        if player_name:
            display_name = player_name[:15] if len(player_name) > 15 else player_name
            self._turn_name_text.set_text(display_name)
            name_y = text_y + 18
            self._turn_name_text.draw_on(sprite, 0, name_y)
