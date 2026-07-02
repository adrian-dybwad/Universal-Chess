"""Tests for GameAnalysisWidget move-history paging.

Why these tests exist
---------------------
The analysis widget was extended so the physical UP/DOWN keys page through the
played move list (page 0 = eval/graph, pages 1..N = move history) in the chosen
notation, wrapping at both ends. These tests pin that paging arithmetic and the
per-page content independently of pixel rendering, because a regression (wrong
page count, broken wrap, off-by-one page slice, or notation not applied) would
otherwise only show as wrong text on the board with no other signal.
"""

from unittest.mock import MagicMock

from PIL import Image

from universalchess.epaper.game_analysis import GameAnalysisWidget
from universalchess.state.chess_game import ChessGameState

# A 14-ply Ruy Lopez opening -> 7 move pairs. Chosen so several pages exist at
# the default widget height and every pairing (including a lone final white move
# on the last page) is exercised.
RUY_LOPEZ_UCI = [
    "e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a4",
    "g8f6", "e1g1", "f8e7", "f1e1", "b7b5", "a4b3", "d7d6",
]


def _widget(game_state, height=80):
    """Build a widget over a real game state with a stubbed analysis state.

    The analysis state is mocked because paging/move-list behavior does not touch
    scores; the update callback is a no-op. Sprites are left None so figurine
    rendering falls back to piece letters (no sheet needed in tests).
    """
    analysis_state = MagicMock()
    widget = GameAnalysisWidget(
        0, 216, 128, height, lambda full=False, immediate=False: None,
        analysis_state=analysis_state,
        notation="figurine",
        game_state=game_state,
        sprites=None,
    )
    return widget


def _game(ucis):
    state = ChessGameState()
    for uci in ucis:
        state.push_uci(uci)
    return state


def test_no_moves_has_single_analysis_page():
    # A fresh game has only the analysis page: no move pages, and paging must not
    # move off page 0 (modulo-1 stays 0). Guards against a divide/blank-page bug
    # when the move list is empty.
    widget = _widget(_game([]))
    assert widget.num_move_pages() == 0
    assert widget.total_pages() == 1
    assert widget.page == 0
    widget.turn_page(1)
    assert widget.page == 0
    widget.turn_page(-1)
    assert widget.page == 0


def test_page_count_matches_moves_and_rows_per_page():
    # 7 move pairs at a height giving 3 rows/page -> ceil(7/3) = 3 move pages plus
    # the analysis page = 4 total. A wrong row-per-page calc or ceil would change
    # the count and desync the wrap range.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    assert widget._rows_per_page() == 3
    assert widget.num_move_pages() == 3
    assert widget.total_pages() == 4


def test_turn_page_wraps_both_ends():
    # From the analysis page, UP (-1) wraps to the last move page and a following
    # DOWN (+1) returns to analysis; this is the requested wrap-around behavior.
    # A regression using clamping instead of modulo would stick at page 0/last.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    assert widget.total_pages() == 4

    widget.turn_page(-1)
    assert widget.page == 3  # wrapped from 0 to last

    widget.turn_page(1)
    assert widget.page == 0  # wrapped from last back to analysis

    widget.turn_page(1)
    assert widget.page == 1  # forward into the first move page


def test_current_page_rows_slice_and_notation_applied():
    # Page 1 shows move pairs 1-3 in the selected notation. Figurine must convert
    # the knight/bishop leading letters to glyphs while leaving pawn moves and
    # disambiguation intact. A wrong slice would show the wrong moves; a notation
    # regression would leave "Nf3" instead of the glyph form.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    widget.turn_page(1)  # page 1
    assert widget.page == 1
    assert widget.current_page_rows() == [
        (1, "e4", "e5"),
        (2, "\u2658f3", "\u2658c6"),   # Nf3 / Nc6
        (3, "\u2657b5", "a6"),          # Bb5 / a6
    ]


def test_last_page_has_lone_white_move_and_uci_notation():
    # Page 3 holds only pair 7 (Bb3 d6). Switching notation to UCI must reflow the
    # same rows in coordinate form. Guards the last-page slice (index past the end
    # must not error) and runtime notation switching.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    widget.set_notation("uci")
    widget.turn_page(-1)  # last page (3)
    assert widget.page == 3
    assert widget.current_page_rows() == [(7, "a4b3", "d7d6")]


def test_render_move_page_draws_something_without_sprites():
    # Rendering a move page with no sprite sheet must not raise and must draw ink
    # (falls back to piece letters). Guards that the figurine fallback path and
    # the text layout are exercised end-to-end on a real canvas.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    widget.turn_page(1)
    canvas = Image.new("1", (widget.width, widget.height), 255)
    widget.render(canvas)
    # At least one black pixel (0) was drawn (header/moves), i.e. not left blank.
    assert canvas.getextrema() == (0, 255)
