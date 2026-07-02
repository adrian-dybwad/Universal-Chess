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


def test_pages_are_end_anchored_with_oldest_partial_first():
    # Pages anchor to the END of the move list: page 1 holds the oldest moves
    # (the only page allowed to be partial), higher pages hold newer moves.
    # Figurine must convert the knight/bishop leading letters to glyphs while
    # leaving pawn moves and disambiguation intact. A wrong slice would show the
    # wrong moves; a notation regression would leave "Nf3" instead of the glyph.
    widget = _widget(_game(RUY_LOPEZ_UCI))   # 7 pairs, 3 rows/page, 3 pages
    widget.turn_page(1)  # page 1 (oldest)
    assert widget.page == 1
    assert widget.current_page_rows() == [(1, "e4", "e5")]  # lone oldest pair

    widget.turn_page(1)  # page 2 (middle, full)
    assert widget.page == 2
    assert widget.current_page_rows() == [
        (2, "\u2658f3", "\u2658c6"),   # Nf3 / Nc6
        (3, "\u2657b5", "a6"),          # Bb5 / a6
        (4, "\u2657a4", "\u2658f6"),   # Ba4 / Nf6
    ]


def test_tail_page_fills_space_with_newest_on_bottom_row():
    # The last (tail) page is a full window of the most recent pairs with the
    # newest move on the bottom row -- the "scroll so the last is at the bottom"
    # behavior. A top-anchored final page would instead strand the leftover moves
    # (here just pair 7) at the top of an otherwise empty page.
    widget = _widget(_game(RUY_LOPEZ_UCI))   # 7 pairs, 3 rows/page
    widget.turn_page(-1)  # UP from analysis wraps to the last page (the tail)
    assert widget.page == widget.num_move_pages()
    rows = widget.current_page_rows()
    assert len(rows) == widget._rows_per_page()   # full page: space is filled
    assert rows == [
        (5, "O-O", "\u2657e7"),        # O-O / Be7
        (6, "\u2656e1", "b5"),          # Re1 / b5
        (7, "\u2657b3", "d6"),          # Bb3 / d6 (newest, bottom row)
    ]


def test_first_page_partial_slice_and_uci_notation():
    # The oldest page is the only partial one (end-anchored); its start index is
    # clamped to 0 so it never slices before the list. UCI switching must reflow
    # the same rows in coordinate form.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    widget.set_notation("uci")
    widget.turn_page(1)  # page 1 (oldest)
    assert widget.page == 1
    assert widget.current_page_rows() == [(1, "e2e4", "e7e5")]


def test_view_follows_tail_when_move_played_while_browsing():
    # While a move page is shown, a new move must snap the view to the tail page
    # so the latest move stays visible on the bottom row, even if the user had
    # paged back to an earlier page. Regression: staying on the stale page would
    # hide live play behind older moves.
    game = _game(RUY_LOPEZ_UCI)   # 7 pairs
    widget = _widget(game)         # created AFTER setup so setup moves don't follow
    widget.turn_page(1)            # browse back to page 1 (oldest)
    assert widget.page == 1

    game.push_uci("d1e2")          # white's 8th move (Qe2) -> a new pair appears

    assert widget.page == widget.num_move_pages()   # snapped to the tail page
    rows = widget.current_page_rows()
    assert rows[-1] == (8, "\u2655e2", None)         # newest move on the bottom row


def test_page_change_callback_fires_on_paging():
    # The clock widget's compact turn indicator is driven by a page-change
    # callback; it must fire with the new page index each time the user pages.
    # A regression that mutated _page without notifying would leave the turn
    # circle stuck (shown on a move page or hidden on the analysis page).
    widget = _widget(_game(RUY_LOPEZ_UCI))
    seen: List[int] = []
    widget.set_page_change_callback(seen.append)

    widget.turn_page(1)   # 0 -> 1
    widget.turn_page(1)   # 1 -> 2
    widget.turn_page(-1)  # 2 -> 1
    assert seen == [1, 2, 1]


def test_page_change_callback_silent_when_page_unchanged():
    # With no moves there is only the analysis page, so paging is a no-op and the
    # callback must not fire. Guards against spuriously toggling the turn
    # indicator (and redrawing the clock) when nothing changed.
    widget = _widget(_game([]))
    seen: List[int] = []
    widget.set_page_change_callback(seen.append)

    widget.turn_page(1)
    widget.turn_page(-1)
    assert seen == []


def test_page_change_callback_fires_when_new_game_clamps_page():
    # Paging onto a move page then starting a new game drops the move pages, so
    # the page clamps back to 0. The callback must fire with 0 so the clock's
    # turn-indicator circle is restored; without this notification the indicator
    # would stay hidden (compact) into the new game.
    game = _game(RUY_LOPEZ_UCI)
    widget = _widget(game)
    seen: List[int] = []

    widget.turn_page(-1)  # to the last move page (3)
    assert widget.page == 3
    widget.set_page_change_callback(seen.append)

    game.reset()  # new game -> no move pages -> clamp 3 -> 0
    assert widget.page == 0
    assert seen == [0]


def test_five_rows_fit_the_compact_timed_height():
    # The compact clock layout gives the analysis widget height 100 in timed
    # mode; the move list must use that space for five rows per page. Guards the
    # MOVE_LINE_HEIGHT tuning -- a regression back to 16px would only fit four,
    # wasting nearly a full row of space.
    widget = _widget(_game(RUY_LOPEZ_UCI), height=100)
    assert widget._rows_per_page() == 5


def test_taller_widget_fits_more_move_rows_per_page():
    # The compact clock layout grows the analysis widget; a taller widget must
    # fit more move rows per page (so the same game needs no more pages). Guards
    # the "analysis grows -> more moves shown" benefit of the compact layout.
    short = _widget(_game(RUY_LOPEZ_UCI), height=80)
    tall = _widget(_game(RUY_LOPEZ_UCI), height=132)
    assert tall._rows_per_page() > short._rows_per_page()
    assert tall.num_move_pages() <= short.num_move_pages()


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
