"""
Game analysis widget displaying evaluation score, history, and move list.

Observes AnalysisState and renders the current score, annotation, and history
graph on its first page. UP/DOWN paging (driven by the physical board keys via
DisplayManager) turns the widget into a paged move-history view: page 0 is the
eval/graph analysis, pages 1..N list the played moves in the notation chosen by
the ``game.notation`` setting. Paging wraps around at both ends.

Horizontal split layout (page 0):
- Left column (44px): Score text, annotation symbol
- Right column (82px): Full-height history graph

The e-paper's bundled font has no figurine glyphs, so figurine notation is drawn
by compositing the board's piece sprites inline with the square text.
"""

import math

from PIL import Image, ImageDraw
from .framework.widget import Widget
from .text import TextWidget, Justify
from universalchess.utils.chess_notation import (
    format_move_history,
    normalize_notation,
)
from typing import List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from universalchess.state.analysis import AnalysisState
    from universalchess.state.chess_game import ChessGameState

# Figurine glyph -> piece letter, used to swap the glyph produced by the notation
# formatter for the matching piece sprite when rendering figurine on the board.
_FIGURINE_TO_LETTER = {
    "\u2654": "K",
    "\u2655": "Q",
    "\u2656": "R",
    "\u2657": "B",
    "\u2658": "N",
}

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

    # Move-history page layout constants
    MOVE_FONT_SIZE = 13
    MOVE_LINE_HEIGHT = 16
    MOVE_MARGIN = 3          # Inner top/left margin for the move list
    MOVE_HEADER_HEIGHT = 15  # Header line ("Moves p/P") above the move rows
    
    def __init__(self, x: int, y: int, width: int, height: int, update_callback,
                 bottom_color: str = "black", show_graph: bool = True,
                 analysis_state: 'AnalysisState' = None,
                 notation: str = "figurine",
                 game_state: 'ChessGameState' = None,
                 sprites: Image.Image = None):
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
        """
        super().__init__(x, y, width, height, update_callback)
        self.bottom_color = bottom_color
        self._show_graph = show_graph
        self._notation = normalize_notation(notation)
        self._page = 0  # 0 = analysis; 1..N = move-history pages
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

        A new move can add a move-history page and a new game removes them, so
        clamp the current page into the valid range before redrawing (otherwise a
        page that no longer exists would render blank after a takeback/new game).
        """
        self._clamp_page()
        self.invalidate_and_update()

    def set_notation(self, notation: str) -> None:
        """Change the move-history notation and redraw if it changed."""
        normalized = normalize_notation(notation)
        if normalized != self._notation:
            self._notation = normalized
            self.invalidate_and_update()

    # --- Paging -----------------------------------------------------------

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
        """Number of move-history pages (0 when there are no moves)."""
        pairs = len(self._move_pairs())
        if pairs == 0:
            return 0
        return math.ceil(pairs / self._rows_per_page())

    def total_pages(self) -> int:
        """Total pages including the analysis page (always at least 1)."""
        return 1 + self.num_move_pages()

    def _clamp_page(self) -> None:
        """Keep the current page within ``[0, total_pages - 1]``."""
        total = self.total_pages()
        if self._page >= total:
            self._page = total - 1
        if self._page < 0:
            self._page = 0

    @property
    def page(self) -> int:
        """The currently displayed page (0 = analysis)."""
        return self._page

    def turn_page(self, direction: int) -> None:
        """Advance the page by ``direction`` (+1 down, -1 up), wrapping around.

        Wrapping makes UP on the analysis page jump to the last move page and
        DOWN on the last move page return to analysis, matching the requested
        UP/DOWN paging behavior.
        """
        total = self.total_pages()
        self._page = (self._page + direction) % total
        self.invalidate_and_update(immediate=True)

    def current_page_rows(self) -> List[Tuple[int, str, Optional[str]]]:
        """Move rows visible on the current page ([] on the analysis page)."""
        if self._page == 0:
            return []
        pairs = self._move_pairs()
        per_page = self._rows_per_page()
        start = (self._page - 1) * per_page
        return pairs[start:start + per_page]
    
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
        """Render the current page.

        Page 0 is the eval score + history graph; pages 1..N are the move-history
        list. A stale page (e.g. after a takeback) is clamped before rendering.
        """
        self._clamp_page()
        if self._page != 0:
            self._render_moves(sprite)
            return
        self._render_analysis(sprite)

    def _render_analysis(self, sprite: Image.Image) -> None:
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

    # --- Move-history page rendering --------------------------------------

    # Piece letter -> x offset in the 16px sprite sheet (uppercase = white art,
    # lowercase = black art), mirroring ChessBoardWidget._piece_x.
    _PIECE_SPRITE_X = {
        "P": 16, "R": 32, "N": 48, "B": 64, "Q": 80, "K": 96,
        "p": 112, "r": 128, "n": 144, "b": 160, "q": 176, "k": 192,
    }

    # Move-list column origins (x) for the move number, white move, black move.
    _NUMBER_COL_X = 3
    _WHITE_COL_X = 26
    _BLACK_COL_X = 78

    def _sprite_sheet(self) -> Optional[Image.Image]:
        """The piece sprite sheet used for figurine rendering, or None."""
        if self._sprites is not None:
            return self._sprites
        from . import chess_board
        return chess_board._chess_sprites

    def _piece_glyph_image(self, letter: str, size: int) -> Optional[Image.Image]:
        """Crop (and scale) the piece sprite for ``letter`` from the light row."""
        sheet = self._sprite_sheet()
        if sheet is None:
            return None
        x = self._PIECE_SPRITE_X.get(letter)
        if x is None:
            return None
        crop = sheet.crop((x, 0, x + 16, 16))
        if size != 16:
            crop = crop.resize((size, size), Image.NEAREST)
        return crop

    def _draw_move_string(self, sprite, draw, x, y, text, white_side, font, glyph_size) -> int:
        """Draw a move string, compositing piece sprites for figurine glyphs.

        Non-figurine notations contain no glyphs, so the whole string draws as one
        text run. For figurine, each glyph is swapped for the matching piece
        sprite (white art for white's move, black art for black's) while the
        surrounding square text is drawn normally. Returns the advanced x.
        """
        run = ""

        def flush(cur_x: int) -> int:
            nonlocal run
            if run:
                draw.text((cur_x, y), run, font=font, fill=0)
                cur_x += int(draw.textlength(run, font=font))
                run = ""
            return cur_x

        for ch in text:
            letter = _FIGURINE_TO_LETTER.get(ch)
            if letter is None:
                run += ch
                continue
            x = flush(x)
            if not white_side:
                letter = letter.lower()
            img = self._piece_glyph_image(letter, glyph_size)
            if img is not None:
                sprite.paste(img, (int(x), int(y)))
                x += glyph_size + 1
            else:
                # No sprite sheet available: fall back to the piece letter so the
                # move is still legible rather than dropping the piece entirely.
                fallback = letter.upper()
                draw.text((x, y), fallback, font=font, fill=0)
                x += int(draw.textlength(fallback, font=font)) + 1
        return flush(x)

    def _render_moves(self, sprite: Image.Image) -> None:
        """Render a move-history page: a header line plus paired move rows."""
        from universalchess.resources import get_font

        draw = ImageDraw.Draw(sprite)
        self.draw_background_on_sprite(sprite)
        draw.rectangle([(0, 0), (self.width - 1, self.height - 1)], fill=None, outline=0)

        font = get_font(self.MOVE_FONT_SIZE)

        header = f"Moves  {self._page}/{self.num_move_pages()}"
        draw.text((self.MOVE_MARGIN, self.MOVE_MARGIN), header, font=font, fill=0)
        separator_y = self.MOVE_MARGIN + self.MOVE_HEADER_HEIGHT - 2
        draw.line([(2, separator_y), (self.width - 2, separator_y)], fill=0, width=1)

        glyph_size = self.MOVE_FONT_SIZE + 1
        y = self.MOVE_MARGIN + self.MOVE_HEADER_HEIGHT
        for number, white, black in self.current_page_rows():
            draw.text((self._NUMBER_COL_X, y), f"{number}.", font=font, fill=0)
            self._draw_move_string(
                sprite, draw, self._WHITE_COL_X, y, white, True, font, glyph_size
            )
            if black is not None:
                self._draw_move_string(
                    sprite, draw, self._BLACK_COL_X, y, black, False, font, glyph_size
                )
            y += self.MOVE_LINE_HEIGHT

    def render_red(self, sprite: Image.Image) -> None:
        """Render the RED overlay: the losing-side (negative) history bars in red.

        Reuses the exact bar geometry from render() (via _iter_graph_bars) and
        fills only the bars whose adjusted score is negative -- i.e. when the
        bottom player is worse off -- with red. Positive bars and the rest of the
        widget contribute no red. Only the analysis page has a graph, so move
        pages contribute no red.
        """
        if self._page != 0:
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
