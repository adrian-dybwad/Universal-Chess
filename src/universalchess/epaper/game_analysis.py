"""
Game analysis widget displaying evaluation score, history, and move list.

Observes AnalysisState and renders the current score, annotation, and history
graph on its first page. UP/DOWN paging (driven by the physical board keys via
DisplayManager) turns the widget into a paged move-history view: page 0 is the
eval/graph analysis, pages 1..N list the played moves in the notation chosen by
the ``game.notation`` setting. Paging wraps around at both ends.

Analysis layout (page 0):
- Top quality bar (full width): the last move's quality word ("Blunder",
  "Brilliant", ...) on the left and that move's accuracy % on the right. The bar
  is filled in the last mover's colour -- white background/black text for a white
  move, black background/white text for a black move -- so the colour tells you
  who just moved at a glance.
- Left column (44px, below the bar): the opponent's running accuracy % on top,
  the evaluation score in the middle (vertically centred on the graph's centre
  line), and the bottom player's running accuracy % below.
- Right column (82px, below the bar): the history graph (shortened to make room
  for the quality bar).

The e-paper's bundled font has no figurine glyphs, so figurine notation is drawn
by compositing the board's piece sprites inline with the square text.
"""

import math

from PIL import Image, ImageDraw
from .framework.widget import Widget
from .text_scale import DEFAULT_TEXT_SIZE, scale_font
from universalchess.utils.chess_notation import (
    format_move_history,
    normalize_notation,
)
from typing import List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from universalchess.state.analysis import AnalysisState
    from universalchess.state.chess_game import ChessGameState

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


class GameAnalysisWidget(Widget):
    """Widget displaying chess game analysis with a quality bar and split layout.
    
    Observes AnalysisState and updates display when score or history changes.
    
    Layout (see the module docstring for details):
    - Top quality bar (full width): last move's quality word + accuracy %,
      filled in the last mover's colour.
    - Left column (44px): opponent accuracy %, score, bottom-player accuracy %.
    - Right column (82px): history graph, shortened to sit below the bar.
    """
    
    # Default position: below the chess clock widget
    DEFAULT_Y = 216
    DEFAULT_HEIGHT = 80
    
    # Layout constants
    SCORE_COLUMN_WIDTH = 44  # Score text and running accuracies
    GRAPH_WIDTH = 82  # History graph

    # Quality bar (full-width header) height, and the fixed font sizes for the
    # analysis view. The score keeps its original size (20); the bar word/percent
    # and the running accuracy figures are smaller so they fit the reclaimed
    # space without shrinking the score.
    QUALITY_BAR_HEIGHT = 18
    SCORE_FONT_SIZE = 20
    QUALITY_WORD_FONT_SIZE = 13
    ACCURACY_FONT_SIZE = 14
    # Ink gap (px) between the top accuracy % and the divider above it, and between
    # the bottom accuracy % and the widget's bottom border below it. The bottom gap
    # is larger so the lower figure sits tucked up toward the score rather than
    # hugging the border.
    ACCURACY_TOP_GAP = 6
    ACCURACY_BOTTOM_GAP = 8

    # Move-history page layout constants at the base (medium) text size.
    # MOVE_LINE_HEIGHT is 15 (not the font's natural ~16) so five rows fit the
    # 100px the analysis widget gets in the compact clock layout:
    # (100 - 2*MOVE_MARGIN - MOVE_HEADER_HEIGHT) // 15 = 5. The font, line, and
    # header sizes are scaled per instance by the Display > Text Size setting
    # (see __init__); MOVE_MARGIN is a fixed inset and does not scale.
    MOVE_FONT_SIZE = 13
    MOVE_LINE_HEIGHT = 15
    MOVE_MARGIN = 3          # Inner top/left margin for the move list
    MOVE_HEADER_HEIGHT = 15  # Header line ("Moves p/P") above the move rows
    
    def __init__(self, x: int, y: int, width: int, height: int, update_callback,
                 bottom_color: str = "black", show_graph: bool = True,
                 analysis_state: 'AnalysisState' = None,
                 notation: str = "figurine",
                 game_state: 'ChessGameState' = None,
                 sprites: Image.Image = None,
                 text_size: str = DEFAULT_TEXT_SIZE):
        """Initialize the analysis widget.
        
        Args:
            x: X position on display
            y: Y position on display
            width: Widget width
            height: Widget height
            update_callback: Callback to trigger display updates.
            bottom_color: Color at bottom of board ("white" or "black")
            show_graph: If True, show the history graph
            analysis_state: AnalysisState to observe. If None, uses singleton.
            notation: Move-history notation (figurine/san/lan/uci).
            game_state: ChessGameState to read the move list from. If None, uses
                the singleton.
            sprites: Piece sprite sheet for figurine rendering. If None, uses the
                module-level sheet set by the app at startup.
            text_size: Display text-size name (small/medium/large) scaling the
                move-list font, line height, and header height. Medium is the
                identity (unchanged) layout; larger sizes fit fewer rows per page
                (paging absorbs the difference).
        """
        super().__init__(x, y, width, height, update_callback)
        self.bottom_color = bottom_color
        self._show_graph = show_graph
        self._notation = normalize_notation(notation)
        # Scale the move-list metrics for the selected text size. Instance
        # attributes shadow the class-level base (medium) constants so every
        # self.MOVE_* reference below picks up the scaled value while the base
        # values remain the documented medium defaults.
        self.MOVE_FONT_SIZE = scale_font(GameAnalysisWidget.MOVE_FONT_SIZE, text_size)
        self.MOVE_LINE_HEIGHT = scale_font(GameAnalysisWidget.MOVE_LINE_HEIGHT, text_size)
        self.MOVE_HEADER_HEIGHT = scale_font(GameAnalysisWidget.MOVE_HEADER_HEIGHT, text_size)
        # Selection cursor over [analysis, ply 1 .. ply N]: 0 = the analysis /
        # eval-graph view (chess board shown); 1..N select an individual played
        # move (ply), whose row is highlighted in the move list while the board
        # area is replaced by that move's coach statement. UP/DOWN step this
        # cursor and it wraps at both ends, so stepping down past the last ply
        # returns to the analysis view (restoring the board).
        self._selection = 0
        # Invoked with the new selection index whenever it changes; drives the
        # clock widget's compact mode and the board/coach-text swap (see
        # set_selection_change_callback).
        self._selection_change_callback: Optional[callable] = None
        self._sprites = sprites
        
        # Get or use provided analysis state
        if analysis_state is None:
            from universalchess.state.analysis import get_analysis
            self._analysis_state = get_analysis()
        else:
            self._analysis_state = analysis_state

        # Get or use provided game state (source of the move history)
        if game_state is None:
            from universalchess.state import get_chess_game
            self._game_state = get_chess_game()
        else:
            self._game_state = game_state
        
        # Subscribe to state changes
        self._analysis_state.on_score_change(self._on_score_change)
        self._analysis_state.on_history_change(self._on_history_change)
        self._game_state.on_position_change(self._on_position_change)
    
    def cleanup(self) -> None:
        """Unsubscribe from observed state when widget is destroyed."""
        if self._analysis_state:
            self._analysis_state.remove_observer(self._on_score_change)
            self._analysis_state.remove_observer(self._on_history_change)
        if self._game_state:
            self._game_state.remove_observer(self._on_position_change)
    
    def _on_score_change(self) -> None:
        """Handle score change from analysis state."""
        self.invalidate_and_update()

    def _on_history_change(self) -> None:
        """Handle history change from analysis state."""
        self.invalidate_and_update()

    def _on_position_change(self) -> None:
        """Handle a new move / new game.

        A new move adds a ply and a new game removes them, so clamp the current
        selection into the valid range before redrawing (otherwise a selection
        that no longer exists would highlight nothing / render blank after a
        takeback or new game). Clamping goes through _set_selection so that if a
        takeback/new game drops the selection back to the analysis view,
        observers (the clock's compact turn indicator and the board/coach swap)
        are notified and don't stay stuck on a move.

        While a move is selected the view follows the live tail: it snaps to the
        last ply (newest move) so play stays visible even if the user had stepped
        back. The analysis view (selection 0) is left alone so it is not yanked
        into the move list on every move.
        """
        if self._selection != 0 and self.num_plies() > 0:
            self._set_selection(self.num_plies())
        self._clamp_selection()
        self.invalidate_and_update()

    def set_notation(self, notation: str) -> None:
        """Change the move-history notation and redraw if it changed."""
        normalized = normalize_notation(notation)
        if normalized != self._notation:
            self._notation = normalized
            self.invalidate_and_update()

    # --- Selection & move list --------------------------------------------

    def _move_pairs(self) -> List[Tuple[int, str, Optional[str]]]:
        """Group the formatted move history into (number, white, black) rows.

        Reads the notation-formatted move list from the game state and pairs
        white/black half-moves the way a scoresheet does. The move number is the
        real chess move number derived from the root position's fullmove counter,
        so a game set up from a custom FEN numbers correctly.
        """
        board = self._game_state.board
        formatted = format_move_history(board, self._notation)
        if not formatted:
            return []
        first_number = board.root().fullmove_number
        rows: List[Tuple[int, str, Optional[str]]] = []
        for i in range(0, len(formatted), 2):
            number = first_number + (i // 2)
            white = formatted[i]
            black = formatted[i + 1] if i + 1 < len(formatted) else None
            rows.append((number, white, black))
        return rows

    def _rows_per_page(self) -> int:
        """How many move-pair rows fit on one move-history page."""
        usable = self.height - 2 * self.MOVE_MARGIN - self.MOVE_HEADER_HEIGHT
        return max(1, usable // self.MOVE_LINE_HEIGHT)

    def num_move_pages(self) -> int:
        """Number of move-history pages (0 when there are no moves).

        Retained as the internal unit for laying out the move list into
        end-anchored windows; the user-facing navigation unit is the ply
        (see :meth:`num_plies`).
        """
        pairs = len(self._move_pairs())
        if pairs == 0:
            return 0
        return math.ceil(pairs / self._rows_per_page())

    def num_plies(self) -> int:
        """Number of played half-moves (plies) available to select."""
        pairs = self._move_pairs()
        if not pairs:
            return 0
        # Each pair is (number, white, black); black is None for a lone final
        # white move, so the last pair may contribute one ply instead of two.
        last_number, last_white, last_black = pairs[-1]
        return (len(pairs) - 1) * 2 + (2 if last_black is not None else 1)

    def total_selections(self) -> int:
        """Total selectable slots: the analysis view plus one per ply."""
        return 1 + self.num_plies()

    def _clamp_selection(self) -> None:
        """Keep the current selection within ``[0, num_plies]``."""
        target = self._selection
        max_selection = self.num_plies()
        if target > max_selection:
            target = max_selection
        if target < 0:
            target = 0
        self._set_selection(target)

    def _set_selection(self, new_selection: int) -> None:
        """Set the selection, notifying observers only on an actual change.

        Centralizes the mutation so the selection-change callback fires from both
        user stepping (step_selection) and automatic clamping/tail-follow
        (takeback/new game/new move).
        """
        if new_selection != self._selection:
            self._selection = new_selection
            if self._selection_change_callback is not None:
                self._selection_change_callback(self._selection)

    def set_selection_change_callback(self, callback: Optional[callable]) -> None:
        """Register a callback invoked with the new selection on every change.

        DisplayManager uses this to (a) switch the clock to compact mode while a
        move is selected, (b) hide the chess board and show the coach-text widget
        for the selected ply (restoring the board on the analysis view), and
        (c) drive the lazy coach-statement fetch for the selected ply.
        """
        self._selection_change_callback = callback

    @property
    def selection(self) -> int:
        """The current selection index (0 = analysis view; 1..N = ply)."""
        return self._selection

    def selected_ply(self) -> Optional[int]:
        """The selected ply (1-based), or None when the analysis view is shown."""
        return None if self._selection == 0 else self._selection

    def step_selection(self, direction: int) -> None:
        """Advance the selection by ``direction`` (+1 down, -1 up), wrapping.

        Wrapping makes UP on the analysis view jump to the last ply and DOWN on
        the last ply return to the analysis view, matching the requested UP/DOWN
        behavior (stepping past the end reselects the analysis view and restores
        the board).
        """
        total = self.total_selections()
        self._set_selection((self._selection + direction) % total)
        self.invalidate_and_update(immediate=True)

    def select_ply(self, ply: int) -> None:
        """Select a specific ply (1-based), clamped to the valid range.

        Restores a previously shown coach selection without a key press (e.g.
        rebuilding the game screen on startup so the coach panel reappears on the
        move the user was reviewing). ``ply <= 0`` selects the analysis/board
        view; a ply beyond the last played move is clamped to the last ply so a
        stale saved value never selects a non-existent move. Repaints like
        :meth:`step_selection`.
        """
        target = ply
        if target < 0:
            target = 0
        max_selection = self.num_plies()
        if target > max_selection:
            target = max_selection
        self._set_selection(target)
        self.invalidate_and_update(immediate=True)

    def _selected_pair_index(self) -> Optional[int]:
        """Index into ``_move_pairs`` of the selected ply's row, or None."""
        ply = self.selected_ply()
        if ply is None:
            return None
        return (ply - 1) // 2

    def selected_is_white(self) -> Optional[bool]:
        """Whether the selected ply is the white half-move, or None if analysis."""
        ply = self.selected_ply()
        if ply is None:
            return None
        return (ply - 1) % 2 == 0

    def _current_page(self) -> int:
        """1-based move page (end-anchored window) holding the selected ply.

        Returns 0 when the analysis view is selected. Pages are anchored to the
        end of the move list so the last page is the tail window with the newest
        move on its bottom row; this maps the selected pair to the page that
        contains it under that anchoring.
        """
        pair_index = self._selected_pair_index()
        if pair_index is None:
            return 0
        pairs = self._move_pairs()
        per_page = self._rows_per_page()
        pages_from_end = (len(pairs) - 1 - pair_index) // per_page
        return self.num_move_pages() - pages_from_end

    def _page_window(self, page: int) -> Tuple[List[Tuple[int, str, Optional[str]]], int]:
        """Return ``(rows, start_pair_index)`` for a 1-based end-anchored page."""
        pairs = self._move_pairs()
        per_page = self._rows_per_page()
        pages_from_end = self.num_move_pages() - page
        end = len(pairs) - pages_from_end * per_page
        start = max(0, end - per_page)
        return pairs[start:end], start

    def visible_rows(self) -> List[Tuple[int, str, Optional[str]]]:
        """Move rows shown for the current selection ([] on the analysis view)."""
        if self._selection == 0:
            return []
        rows, _start = self._page_window(self._current_page())
        return rows

    def selected_row_index(self) -> Optional[int]:
        """Index of the selected ply's row within :meth:`visible_rows`, or None."""
        pair_index = self._selected_pair_index()
        if pair_index is None:
            return None
        _rows, start = self._page_window(self._current_page())
        return pair_index - start

    def set_show_graph(self, show: bool) -> None:
        """Set whether to show the history graph.
        
        Args:
            show: If True, show the graph; if False, hide it
        """
        if self._show_graph != show:
            self._show_graph = show
            self.invalidate_and_update()
    
    def _graph_geometry(self) -> dict:
        """Pixel geometry of the history graph, shared by render and render_red.

        Single source of truth for the graph rectangle and center line so the
        B/W bars and their red overlay are computed identically (any drift would
        offset the red highlight from the bar it marks). The graph top sits below
        the quality bar so the two never overlap.
        """
        graph_x = self.SCORE_COLUMN_WIDTH + 2
        graph_right = graph_x + self.GRAPH_WIDTH
        graph_top = self.QUALITY_BAR_HEIGHT + 4
        graph_bottom = self.height - 4
        chart_y = graph_top + (graph_bottom - graph_top) // 2
        return {
            "graph_x": graph_x, "graph_right": graph_right,
            "graph_top": graph_top, "graph_bottom": graph_bottom,
            "chart_y": chart_y,
        }

    def _iter_graph_bars(self, history):
        """Yield ``(x0, y0, x1, y1, adjusted_score)`` for each history bar.

        Encapsulates the bar layout (width cap, score scaling, right-justified
        offset, center-line clamping) so render() draws the bars and render_red()
        can redraw the losing-side ones in red without duplicating the maths.
        ``adjusted_score`` is from the bottom player's perspective: negative means
        the bottom player is worse off (the bars rendered in red).
        """
        if not history:
            return

        geo = self._graph_geometry()
        chart_y = geo["chart_y"]
        graph_top = geo["graph_top"]
        graph_bottom = geo["graph_bottom"]
        graph_height = graph_bottom - graph_top

        bar_width = self.GRAPH_WIDTH / len(history)
        if bar_width > 6:
            bar_width = 6
        # Graph fills its half-height at +/-12 pawns and saturates beyond that.
        # This is a fixed visualization range (kept small so ordinary evals are
        # legible) and is deliberately independent of the larger score-text clamp.
        scale = (graph_height // 2) / 12.0
        bar_offset = geo["graph_right"] - bar_width * len(history)

        for score in history:
            adjusted_score = -score if self.bottom_color == "black" else score
            y_calc = chart_y - adjusted_score * scale
            y_calc = max(graph_top, min(graph_bottom, y_calc))
            y0 = min(chart_y, int(y_calc))
            y1 = max(chart_y, int(y_calc))
            yield (int(bar_offset), y0, int(bar_offset + bar_width), y1, adjusted_score)
            bar_offset += bar_width
    
    def stop(self) -> None:
        """Stop the widget and perform cleanup tasks."""
        self.cleanup()
    
    def __del__(self):
        """Cleanup when widget is destroyed."""
        self.cleanup()
    
    def render(self, sprite: Image.Image) -> None:
        """Render the current selection.

        Selection 0 is the eval score + history graph; a selected ply renders the
        move-history window containing it with that move highlighted. A stale
        selection (e.g. after a takeback) is clamped before rendering.
        """
        self._clamp_selection()
        if self._selection != 0:
            self._render_moves(sprite)
            return
        self._render_analysis(sprite)

    def _render_analysis(self, sprite: Image.Image) -> None:
        """Render the analysis view: quality bar, score column and history graph.

        See the module docstring for the layout. The score is vertically centred
        on the graph's centre line; the two running accuracies sit above and
        below it (opponent on top, bottom player below), matching the board's
        orientation.
        """
        score_value = self._analysis_state.score
        history = self._analysis_state.history
        summary = self._analysis_state.accuracy_summary()

        log.debug(f"[GameAnalysisWidget] Rendering: y={self.y}, height={self.height}, "
                  f"history_len={len(history)}, graph={self._show_graph}")

        draw = ImageDraw.Draw(sprite)
        self.draw_background_on_sprite(sprite)
        draw.rectangle([(0, 0), (self.width - 1, self.height - 1)], fill=None, outline=0)

        geo = self._graph_geometry()
        graph_x = geo["graph_x"]
        graph_right = geo["graph_right"]
        chart_y = geo["chart_y"]

        # === TOP: quality bar (last move) ===
        self._render_quality_bar(draw, summary)

        # Permanent horizontal divider between the quality bar and the content
        # below it. Drawn regardless of the bar's fill so the boundary is always
        # visible -- a white (white-move) or blank (no-move) bar would otherwise
        # merge into the graph area with no separating edge.
        draw.line([(1, self.QUALITY_BAR_HEIGHT + 1),
                   (self.width - 2, self.QUALITY_BAR_HEIGHT + 1)], fill=0, width=1)

        # Vertical separator between the score column and the graph, below the bar
        draw.line([(self.SCORE_COLUMN_WIDTH, self.QUALITY_BAR_HEIGHT + 2),
                   (self.SCORE_COLUMN_WIDTH, self.height - 2)], fill=0, width=1)

        # === LEFT COLUMN: opponent accuracy, score, bottom-player accuracy ===
        from universalchess.resources import get_font

        # Top % ink starts ACCURACY_TOP_GAP below the divider; bottom % ink ends
        # ACCURACY_BOTTOM_GAP above the bottom border.
        top_accuracy, bottom_accuracy = self._column_accuracies(summary)
        accuracy_font = get_font(self.ACCURACY_FONT_SIZE)
        divider_y = self.QUALITY_BAR_HEIGHT + 1
        if top_accuracy is not None:
            self._draw_column_text(draw, accuracy_font, f"{top_accuracy:.0f}%",
                                   divider_y + self.ACCURACY_TOP_GAP, anchor="top")
        if bottom_accuracy is not None:
            self._draw_column_text(draw, accuracy_font, f"{bottom_accuracy:.0f}%",
                                   self.height - 1 - self.ACCURACY_BOTTOM_GAP, anchor="bottom")

        self._draw_column_text_vcentered(
            draw, get_font(self.SCORE_FONT_SIZE),
            self._format_score_text(score_value), chart_y)

        # === RIGHT SECTION: history graph ===
        if self._show_graph and len(history) > 0:
            draw.line([(graph_x, chart_y), (graph_right, chart_y)], fill=0, width=1)
            for x0, y0, x1, y1, adjusted_score in self._iter_graph_bars(history):
                # Positive scores (bottom-player advantage) go up, use white fill;
                # negative scores (bottom-player worse) go down, use black fill.
                color = 255 if adjusted_score >= 0 else 0
                draw.rectangle([(x0, y0), (x1, y1)], fill=color, outline=0)
        elif self._show_graph:
            # Still draw the center line even if no history yet
            draw.line([(graph_x, chart_y), (graph_right, chart_y)], fill=0, width=1)

    def _format_score_text(self, score_value: float) -> str:
        """Format the evaluation for the score column from the bottom player's view.

        The stored score is from White's perspective; it is negated when Black is
        at the bottom so a positive number always means the bottom player is
        better. Mate and out-of-range values collapse to an "M" marker.
        """
        display_value = -score_value if self.bottom_color == "black" else score_value
        if self._analysis_state.is_mate:
            mate_in = self._analysis_state.mate_in
            return f"M{abs(mate_in)}" if mate_in is not None else "M"
        if abs(display_value) > 999:
            return "M"
        return f"+{display_value:.1f}" if display_value >= 0 else f"{display_value:.1f}"

    def _column_accuracies(self, summary) -> Tuple[Optional[float], Optional[float]]:
        """Return ``(top, bottom)`` accuracy % for the score column.

        The bottom player's colour is fixed by ``bottom_color``; its accuracy is
        shown below the score and the opponent's above, so the two figures track
        the board's orientation.
        """
        if self.bottom_color == "black":
            return summary.white, summary.black
        return summary.black, summary.white

    def _render_quality_bar(self, draw: ImageDraw.ImageDraw, summary) -> None:
        """Draw the full-width quality bar for the last move.

        The bar is filled in the last mover's colour (white background/black text
        for a white move, black background/white text for a black move). The
        move-quality word and that move's accuracy % are drawn next to each other
        as a single group, centred in the bar. Before any move has been played
        there is no last mover, so the bar is left blank on a white background.
        """
        from universalchess.resources import get_font

        mover_white = summary.last_mover_white
        background = 0 if mover_white is False else 255
        foreground = 255 if mover_white is False else 0
        draw.rectangle([(1, 1), (self.width - 2, self.QUALITY_BAR_HEIGHT)], fill=background)

        if mover_white is None:
            return

        parts = []
        if summary.last_word:
            parts.append(summary.last_word)
        if summary.last_accuracy is not None:
            parts.append(f"{summary.last_accuracy:.0f}%")
        if not parts:
            return

        # Word and percent sit side by side as one group, centred horizontally and
        # vertically within the bar.
        text = "  ".join(parts)
        font = get_font(self.QUALITY_WORD_FONT_SIZE)
        x = max(3, (self.width - self._text_width(draw, text, font)) // 2)
        self._draw_text_vcentered(
            draw, font, text, x, (1 + self.QUALITY_BAR_HEIGHT) / 2, foreground)

    @staticmethod
    def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
        """Pixel width of ``text`` in ``font``."""
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]

    @staticmethod
    def _draw_text_vcentered(draw: ImageDraw.ImageDraw, font, text: str,
                             x: int, y_center: float, fill: int) -> None:
        """Draw ``text`` left-anchored at ``x`` and vertically centred on ``y_center``."""
        bbox = draw.textbbox((0, 0), text, font=font)
        height = bbox[3] - bbox[1]
        y = int(round(y_center - bbox[1] - height / 2))
        draw.text((x, y), text, font=font, fill=fill)

    def _column_pen_x(self, font, text: str) -> int:
        """Pen x that optically centres ``text``'s ink in the score column.

        The e-paper bitmap font reports a zero left side-bearing from
        ``textbbox`` even when the rendered ink is inset, so different strings
        (e.g. "100%" vs "71%") would otherwise land at slightly different optical
        centres. ``font.getmask(text).getbbox()`` exposes the true ink extent;
        centring on it keeps every value visually centred in the column.
        """
        ink = font.getmask(text).getbbox()
        if ink is None:  # whitespace / empty: nothing to centre, fall back to origin
            return 0
        ink_left, ink_width = ink[0], ink[2] - ink[0]
        return (self.SCORE_COLUMN_WIDTH - ink_width) // 2 - ink_left

    def _draw_column_text(self, draw: ImageDraw.ImageDraw, font, text: str, y: int,
                          anchor: str = "top") -> None:
        """Draw ``text`` ink-centred in the score column, vertically anchored at ``y``.

        ``anchor`` selects whether ``y`` is the first ink row ("top") or the last
        ink row ("bottom"). ``textbbox`` gives the true vertical ink extent for
        this font, so the two accuracy figures can be positioned by what is
        actually seen rather than by the glyph cell.
        """
        bbox = draw.textbbox((0, 0), text, font=font)
        ink_reference = bbox[3] - 1 if anchor == "bottom" else bbox[1]
        draw.text((self._column_pen_x(font, text), y - ink_reference),
                  text, font=font, fill=0)

    def _draw_column_text_vcentered(self, draw: ImageDraw.ImageDraw, font, text: str,
                                    y_center: int) -> None:
        """Draw ``text`` ink-centred in the score column, vertically about ``y_center``."""
        bbox = draw.textbbox((0, 0), text, font=font)
        y = y_center - bbox[1] - (bbox[3] - bbox[1]) // 2
        draw.text((self._column_pen_x(font, text), y), text, font=font, fill=0)

    # --- Move-history page rendering --------------------------------------

    # Move-list column origins (x) for the move number, white move, black move.
    _NUMBER_COL_X = 3
    _WHITE_COL_X = 26
    _BLACK_COL_X = 78

    def _draw_move_string(self, sprite, draw, x, y, text, white_side, font, glyph_size, fill=0) -> int:
        """Draw a move string, compositing piece sprites for figurine glyphs.

        Thin wrapper over the shared move_render helper, passing this widget's
        sprite sheet (falling back to the app's global sheet). ``fill`` is the text
        color (255 for an inverted, selected move). Returns the advanced x.
        """
        from . import move_render
        sheet = move_render.sprite_sheet(self._sprites)
        return move_render.draw_move_string(
            sprite, draw, x, y, text, white_side, font, glyph_size, sheet, fill=fill
        )

    def _render_moves(self, sprite: Image.Image) -> None:
        """Render the move-history window for the selection, highlighting the ply.

        A header line ("Move k/N") tops the window; the selected ply's half-move
        is drawn inverted (white on black) so the user sees exactly which move the
        coach statement (shown in the board area) refers to.
        """
        from universalchess.resources import get_font

        draw = ImageDraw.Draw(sprite)
        self.draw_background_on_sprite(sprite)

        font = get_font(self.MOVE_FONT_SIZE)

        header = f"Move  {self.selected_ply()}/{self.num_plies()}"
        draw.text((self.MOVE_MARGIN, self.MOVE_MARGIN), header, font=font, fill=0)

        selected_row = self.selected_row_index()
        selected_is_white = self.selected_is_white()

        glyph_size = self.MOVE_FONT_SIZE + 1
        top = self.MOVE_MARGIN + self.MOVE_HEADER_HEIGHT
        y = top
        for row_index, (number, white, black) in enumerate(self.visible_rows()):
            row_selected = row_index == selected_row and selected_is_white is not None
            draw.text((self._NUMBER_COL_X, y), f"{number}.", font=font, fill=0)
            self._draw_move_cell(
                sprite, draw, self._WHITE_COL_X, y, white, True, font, glyph_size,
                selected=row_selected and selected_is_white,
            )
            if black is not None:
                self._draw_move_cell(
                    sprite, draw, self._BLACK_COL_X, y, black, False, font, glyph_size,
                    selected=row_selected and not selected_is_white,
                )
            y += self.MOVE_LINE_HEIGHT

    def _selection_cell_bounds(self, is_white: bool, y: int):
        """Pixel bounds ``(x0, y0, x1, y1)`` of a half-move cell for highlighting."""
        if is_white:
            x0 = self._WHITE_COL_X - 2
            x1 = self._BLACK_COL_X - 3
        else:
            x0 = self._BLACK_COL_X - 2
            x1 = self.width - 3
        return x0, y - 1, x1, y + self.MOVE_LINE_HEIGHT - 2

    def _draw_move_cell(self, sprite, draw, x, y, text, white_side, font, glyph_size,
                        selected: bool) -> None:
        """Draw a half-move, inverting it (white on black) when it is the selection.

        The selected move is highlighted by filling its cell black and drawing the
        move -- text and composited piece glyphs -- in white, instead of boxing it,
        so the active move reads as a solid highlight. The figurine glyphs are
        inverted to match (see :func:`move_render.draw_move_string`).
        """
        if selected:
            x0, y0, x1, y1 = self._selection_cell_bounds(white_side, y)
            draw.rectangle([(x0, y0), (x1, y1)], fill=0, outline=0)
            self._draw_move_string(sprite, draw, x, y, text, white_side, font, glyph_size, fill=255)
        else:
            self._draw_move_string(sprite, draw, x, y, text, white_side, font, glyph_size)

    def render_red(self, sprite: Image.Image) -> None:
        """Render the RED overlay: the losing-side (negative) history bars in red.

        Reuses the exact bar geometry from render() (via _iter_graph_bars) and
        fills only the bars whose adjusted score is negative -- i.e. when the
        bottom player is worse off -- with red. Positive bars and the rest of the
        widget contribute no red. Only the analysis view has a graph, so a
        selected move contributes no red.
        """
        if self._selection != 0:
            return
        if not self._show_graph:
            return
        history = self._analysis_state.history
        if not history:
            return

        draw = ImageDraw.Draw(sprite)
        for x0, y0, x1, y1, adjusted_score in self._iter_graph_bars(history):
            if adjusted_score < 0:
                draw.rectangle([(x0, y0), (x1, y1)], fill=0, outline=0)
