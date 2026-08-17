"""Move-list widget: paged scoresheet of the live game.

Independent of the analysis eval/graph widget. UP/DOWN (driven by the physical
board keys via DisplayManager) steps a selection cursor over [home, ply 1 ..
ply N]: selection 0 is the board view (this widget hidden); 1..N highlight a
played half-move while the board area shows that move's coach statement.
Stepping wraps at both ends so DOWN past the last ply returns home.

The list is always created during a game, including when Show Analysis or Live
Analysis is off -- those settings only control the eval panel.
"""

import math
from typing import List, Optional, Tuple, TYPE_CHECKING

from PIL import Image, ImageDraw

from .framework.widget import Widget
from .text_scale import DEFAULT_TEXT_SIZE, scale_font
from universalchess.utils.chess_notation import (
    format_move_history,
    normalize_notation,
)

if TYPE_CHECKING:
    from universalchess.state.chess_game import ChessGameState


class MoveListWidget(Widget):
    """Paged move-history scoresheet with a per-ply selection highlight.

    Observes ChessGameState for the played moves. Hidden until a ply is
    selected; DisplayManager shows it for the duration of move review.
    """

    # Move-history page layout constants at the base (medium) text size.
    # MOVE_LINE_HEIGHT is 15 (not the font's natural ~16) so five rows fit the
    # 100px the widget gets in the compact clock layout:
    # (100 - 2*MOVE_MARGIN - MOVE_HEADER_HEIGHT) // 15 = 5. The font, line, and
    # header sizes are scaled per instance by the Display > Text Size setting
    # (see __init__); MOVE_MARGIN is a fixed inset and does not scale.
    MOVE_FONT_SIZE = 13
    MOVE_LINE_HEIGHT = 15
    MOVE_MARGIN = 3          # Inner top/left margin for the move list
    MOVE_HEADER_HEIGHT = 15  # Header line ("Move k/N") above the move rows

    # Column origins (x) for the move number, white move, black move.
    _NUMBER_COL_X = 3
    _WHITE_COL_X = 26
    _BLACK_COL_X = 78

    def __init__(self, x: int, y: int, width: int, height: int, update_callback,
                 notation: str = "figurine",
                 game_state: 'ChessGameState' = None,
                 sprites: Image.Image = None,
                 text_size: str = DEFAULT_TEXT_SIZE):
        """Initialize the move-list widget (hidden until a ply is selected).

        Args:
            x: X position on display
            y: Y position on display
            width: Widget width
            height: Widget height
            update_callback: Callback to trigger display updates.
            notation: Move-history notation (figurine/san/lan/uci).
            game_state: ChessGameState to read the move list from. If None, uses
                the singleton.
            sprites: Piece sprite sheet for figurine rendering. If None, uses the
                module-level sheet set by the app at startup.
            text_size: Display text-size name (small/medium/large) scaling the
                font, line height, and header height. Medium is the identity
                (unchanged) layout; larger sizes fit fewer rows per page
                (paging absorbs the difference).
        """
        super().__init__(x, y, width, height, update_callback)
        # Hidden by default: it only appears while a ply is selected. Set the
        # flag directly (not via hide()) so no refresh is requested before the
        # widget is even added to the manager.
        self.visible = False
        self._notation = normalize_notation(notation)
        # Scale the move-list metrics for the selected text size. Instance
        # attributes shadow the class-level base (medium) constants so every
        # self.MOVE_* reference below picks up the scaled value while the base
        # values remain the documented medium defaults.
        self.MOVE_FONT_SIZE = scale_font(MoveListWidget.MOVE_FONT_SIZE, text_size)
        self.MOVE_LINE_HEIGHT = scale_font(MoveListWidget.MOVE_LINE_HEIGHT, text_size)
        self.MOVE_HEADER_HEIGHT = scale_font(MoveListWidget.MOVE_HEADER_HEIGHT, text_size)
        # Selection cursor over [home, ply 1 .. ply N]: 0 = the board view
        # (this widget hidden); 1..N select an individual played move (ply),
        # whose row is highlighted while the board area is replaced by that
        # move's coach statement. UP/DOWN step this cursor and it wraps at both
        # ends, so stepping down past the last ply returns home.
        self._selection = 0
        # Invoked with the new selection index whenever it changes; drives the
        # clock widget's compact mode and the board/coach-text swap (see
        # set_selection_change_callback).
        self._selection_change_callback: Optional[callable] = None
        self._sprites = sprites

        if game_state is None:
            from universalchess.state import get_chess_game
            self._game_state = get_chess_game()
        else:
            self._game_state = game_state

        self._game_state.on_position_change(self._on_position_change)

    def cleanup(self) -> None:
        """Unsubscribe from observed state when widget is destroyed."""
        if self._game_state:
            self._game_state.remove_observer(self._on_position_change)

    def _on_position_change(self) -> None:
        """Handle a new move / new game.

        A new move adds a ply and a new game removes them, so clamp the current
        selection into the valid range before redrawing (otherwise a selection
        that no longer exists would highlight nothing / render blank after a
        takeback or new game). Clamping goes through _set_selection so that if a
        takeback/new game drops the selection back to home, observers (the
        clock's compact turn indicator and the board/coach swap) are notified
        and don't stay stuck on a move.

        While a move is selected the view follows the live tail: it snaps to the
        last ply (newest move) so play stays visible even if the user had stepped
        back. Home (selection 0) is left alone so it is not yanked into the move
        list on every move.
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
        last_black = pairs[-1][2]
        return (len(pairs) - 1) * 2 + (2 if last_black is not None else 1)

    def total_selections(self) -> int:
        """Total selectable slots: home plus one per ply."""
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
        for the selected ply (restoring the board on home), and (c) drive the
        lazy coach-statement fetch for the selected ply.
        """
        self._selection_change_callback = callback

    @property
    def selection(self) -> int:
        """The current selection index (0 = home/board view; 1..N = ply)."""
        return self._selection

    def selected_ply(self) -> Optional[int]:
        """The selected ply (1-based), or None when the home view is shown."""
        return None if self._selection == 0 else self._selection

    def step_selection(self, direction: int) -> None:
        """Advance the selection by ``direction`` (+1 down, -1 up), wrapping.

        Wrapping makes UP on home jump to the last ply and DOWN on the last ply
        return home, matching the requested UP/DOWN behavior (stepping past the
        end restores the board).
        """
        total = self.total_selections()
        self._set_selection((self._selection + direction) % total)
        self.invalidate_and_update(immediate=True)

    def select_ply(self, ply: int) -> None:
        """Select a specific ply (1-based), clamped to the valid range.

        Restores a previously shown coach selection without a key press (e.g.
        rebuilding the game screen on startup so the coach panel reappears on the
        move the user was reviewing). ``ply <= 0`` selects home; a ply beyond
        the last played move is clamped to the last ply so a stale saved value
        never selects a non-existent move. Repaints like :meth:`step_selection`.
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
        """Whether the selected ply is the white half-move, or None if home."""
        ply = self.selected_ply()
        if ply is None:
            return None
        return (ply - 1) % 2 == 0

    def _current_page(self) -> int:
        """1-based move page (end-anchored window) holding the selected ply.

        Returns 0 when home is selected. Pages are anchored to the end of the
        move list so the last page is the tail window with the newest move on
        its bottom row; this maps the selected pair to the page that contains
        it under that anchoring.
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
        """Move rows shown for the current selection ([] on home)."""
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

    def stop(self) -> None:
        """Stop the widget and perform cleanup tasks."""
        self.cleanup()

    def __del__(self):
        """Cleanup when widget is destroyed."""
        self.cleanup()

    def render(self, sprite: Image.Image) -> None:
        """Render the current selection.

        A selected ply renders the move-history window containing it with that
        move highlighted. Home (selection 0) is not shown; DisplayManager hides
        this widget there. A stale selection (e.g. after a takeback) is clamped
        before rendering.
        """
        self._clamp_selection()
        if self._selection == 0:
            self.draw_background_on_sprite(sprite)
            return
        self._render_moves(sprite)

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
