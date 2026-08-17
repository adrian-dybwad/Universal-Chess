"""
Game analysis widget displaying evaluation score and history graph.

Observes AnalysisState and renders the current score, annotation, and history
graph. The played-move scoresheet is a separate widget
(:class:`~universalchess.epaper.move_list.MoveListWidget`); this one is only
the eval panel and is hidden when Show Analysis is off.

Analysis layout:
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
"""

from PIL import Image, ImageDraw
from .framework.widget import Widget
from typing import Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from universalchess.state.analysis import AnalysisState

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
    
    def __init__(self, x: int, y: int, width: int, height: int, update_callback,
                 bottom_color: str = "black", show_graph: bool = True,
                 analysis_state: 'AnalysisState' = None):
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
        """
        super().__init__(x, y, width, height, update_callback)
        self.bottom_color = bottom_color
        self._show_graph = show_graph
        
        # Get or use provided analysis state
        if analysis_state is None:
            from universalchess.state.analysis import get_analysis
            self._analysis_state = get_analysis()
        else:
            self._analysis_state = analysis_state
        
        # Subscribe to state changes
        self._analysis_state.on_score_change(self._on_score_change)
        self._analysis_state.on_history_change(self._on_history_change)
    
    def cleanup(self) -> None:
        """Unsubscribe from observed state when widget is destroyed."""
        if self._analysis_state:
            self._analysis_state.remove_observer(self._on_score_change)
            self._analysis_state.remove_observer(self._on_history_change)
    
    def _on_score_change(self) -> None:
        """Handle score change from analysis state."""
        self.invalidate_and_update()

    def _on_history_change(self) -> None:
        """Handle history change from analysis state."""
        self.invalidate_and_update()

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
        """Render the eval score, quality bar, and history graph."""
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

    def render_red(self, sprite: Image.Image) -> None:
        """Render the RED overlay: the losing-side (negative) history bars in red.

        Reuses the exact bar geometry from render() (via _iter_graph_bars) and
        fills only the bars whose adjusted score is negative -- i.e. when the
        bottom player is worse off -- with red. Positive bars and the rest of the
        widget contribute no red.
        """
        if not self._show_graph:
            return
        history = self._analysis_state.history
        if not history:
            return

        draw = ImageDraw.Draw(sprite)
        for x0, y0, x1, y1, adjusted_score in self._iter_graph_bars(history):
            if adjusted_score < 0:
                draw.rectangle([(x0, y0), (x1, y1)], fill=0, outline=0)
