"""
Game analysis widget displaying evaluation score and history.

Observes AnalysisState and renders the current score, annotation, and
history graph. All analysis logic is handled by AnalysisService - this
widget is purely for display.

Horizontal split layout:
- Left column (44px): Score text, annotation symbol
- Right column (82px): Full-height history graph
"""

from PIL import Image, ImageDraw
from .framework.widget import Widget
from .text import TextWidget, Justify
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from universalchess.state.analysis import AnalysisState

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


class GameAnalysisWidget(Widget):
    """Widget displaying chess game analysis with horizontal split layout.
    
    Observes AnalysisState and updates display when score or history changes.
    
    Layout:
    - Left column (44px wide): Score text (large), annotation symbol
    - Right column (82px wide): Full-height history graph
    """
    
    # Default position: below the chess clock widget
    DEFAULT_Y = 216
    DEFAULT_HEIGHT = 80
    
    # Layout constants
    SCORE_COLUMN_WIDTH = 44  # Score text and annotation
    GRAPH_WIDTH = 82  # History graph
    
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
        
        # Create TextWidgets for score and annotation
        self._score_text_widget = TextWidget(
            0, 4, self.SCORE_COLUMN_WIDTH, 26, self._handle_child_update,
            text="+0.0", font_size=20, 
            justify=Justify.CENTER, transparent=True
        )
        self._annotation_text_widget = TextWidget(
            0, 30, self.SCORE_COLUMN_WIDTH, 24, self._handle_child_update,
            text="", font_size=22,
            justify=Justify.CENTER, transparent=True
        )
    
    def cleanup(self) -> None:
        """Unsubscribe from analysis state when widget is destroyed."""
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
    
    def _handle_child_update(self, full: bool = False, immediate: bool = False):
        """Handle update requests from child widgets by forwarding to parent callback."""
        return self._update_callback(full, immediate)

    def _graph_geometry(self) -> dict:
        """Pixel geometry of the history graph, shared by render and render_red.

        Single source of truth for the graph rectangle and center line so the
        B/W bars and their red overlay are computed identically (any drift would
        offset the red highlight from the bar it marks).
        """
        graph_x = self.SCORE_COLUMN_WIDTH + 2
        graph_right = graph_x + self.GRAPH_WIDTH
        graph_top = 4
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
        """Render analysis widget with horizontal split layout.
        
        Layout:
        - Left column (44px): Score text, annotation
        - Right column (82px): Full-height history graph
        """
        # Get current values from state
        score_value = self._analysis_state.score
        score_text = self._analysis_state.score_text
        annotation = self._analysis_state.annotation
        history = self._analysis_state.history
        
        log.debug(f"[GameAnalysisWidget] Rendering: y={self.y}, height={self.height}, "
                  f"history_len={len(history)}, graph={self._show_graph}")
        
        draw = ImageDraw.Draw(sprite)
        
        # Draw background
        self.draw_background_on_sprite(sprite)
        
        # Draw 1px border around widget extent
        draw.rectangle([(0, 0), (self.width - 1, self.height - 1)], fill=None, outline=0)
        
        # Adjust score for display based on bottom color
        # Score is always from white's perspective (positive = white advantage)
        display_score_value = -score_value if self.bottom_color == "black" else score_value
        
        # Calculate layout: Score text/annotation on left, graph on right
        left_col_width = self.SCORE_COLUMN_WIDTH
        graph_x = left_col_width + 2
        graph_width = self.GRAPH_WIDTH
        graph_right = graph_x + graph_width
        
        # === LEFT COLUMN: Score text, annotation (center-justified) ===
        # Draw vertical separator between score column and graph
        draw.line([(left_col_width, 2), (left_col_width, self.height - 2)], fill=0, width=1)
        
        # Format score text for display
        if self._analysis_state.is_mate:
            mate_in = self._analysis_state.mate_in
            if mate_in is not None:
                display_score_text = f"M{abs(mate_in)}"
            else:
                display_score_text = "M"
        elif abs(display_score_value) > 999:
            display_score_text = "M"
        else:
            if display_score_value >= 0:
                display_score_text = f"+{display_score_value:.1f}"
            else:
                display_score_text = f"{display_score_value:.1f}"
        
        # Draw score text directly onto sprite (center-justified)
        self._score_text_widget.set_text(display_score_text)
        self._score_text_widget.draw_on(sprite, 0, 4)
        
        # Draw annotation directly onto sprite (center-justified, below score)
        if annotation:
            self._annotation_text_widget.set_text(annotation)
            self._annotation_text_widget.draw_on(sprite, 0, 30)
        
        # === RIGHT SECTION: History graph ===
        if self._show_graph and len(history) > 0:
            # Center line
            chart_y = self._graph_geometry()["chart_y"]
            draw.line([(graph_x, chart_y), (graph_right, chart_y)], fill=0, width=1)

            for x0, y0, x1, y1, adjusted_score in self._iter_graph_bars(history):
                # Positive scores (bottom-player advantage) go up, use white fill;
                # negative scores (bottom-player worse) go down, use black fill.
                color = 255 if adjusted_score >= 0 else 0
                draw.rectangle([(x0, y0), (x1, y1)], fill=color, outline=0)
        elif self._show_graph:
            # Still draw the center line even if no history yet
            chart_y = self._graph_geometry()["chart_y"]
            draw.line([(graph_x, chart_y), (graph_right, chart_y)], fill=0, width=1)

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
