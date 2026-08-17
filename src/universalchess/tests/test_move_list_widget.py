"""Tests for the standalone MoveListWidget.

Why these tests exist
---------------------
UP/DOWN during a game highlights a played move in a paged move list. That list
used to live inside GameAnalysisWidget, so turning analysis off (Show Analysis
or Live Analysis) hid or never created it and the arrows did nothing. These
tests pin the cursor arithmetic, ply->row mapping/highlight, end-anchored
window, tail-follow, and selection-change callback on MoveListWidget itself,
independent of the analysis eval/graph widget and of pixel rendering.

How a regression manifests
--------------------------
Wrong ply count, broken wrap, off-by-one row mapping, the wrong half-move
highlighted, or a lost tail-follow would coach the wrong move with no other
signal. Importing GameAnalysisWidget here (or requiring analysis_state) would
mean the list is still coupled to analysis and disappears when analysis is off.
"""

from unittest.mock import MagicMock

from PIL import Image

from universalchess.epaper.move_list import MoveListWidget
from universalchess.state.chess_game import ChessGameState

# A 14-ply Ruy Lopez opening -> 7 move pairs. Chosen so several rows-per-page
# windows exist at the default widget height and every pairing (including a lone
# final white move after an extra move) is exercised.
RUY_LOPEZ_UCI = [
    "e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a4",
    "g8f6", "e1g1", "f8e7", "f1e1", "b7b5", "a4b3", "d7d6",
]

# Figurine strings for the three middle pairs (pairs 2-4), reused across tests.
PAIRS_2_TO_4 = [
    (2, "\u2658f3", "\u2658c6"),   # Nf3 / Nc6
    (3, "\u2657b5", "a6"),          # Bb5 / a6
    (4, "\u2657a4", "\u2658f6"),   # Ba4 / Nf6
]


def _widget(game_state, height=80):
    """Build a move-list widget over a real game state. No analysis involved."""
    return MoveListWidget(
        0, 216, 128, height, lambda full=False, immediate=False: None,
        notation="figurine",
        game_state=game_state,
        sprites=None,
    )


def _game(ucis):
    state = ChessGameState()
    for uci in ucis:
        state.push_uci(uci)
    return state


def test_no_moves_has_only_home_selection():
    # A fresh game has only the home (board) view: zero plies, and stepping must
    # not move off selection 0 (modulo-1 stays 0). Guards a divide/blank bug when
    # the move list is empty.
    widget = _widget(_game([]))
    assert widget.num_plies() == 0
    assert widget.total_selections() == 1
    assert widget.selection == 0
    assert widget.selected_ply() is None
    widget.step_selection(1)
    assert widget.selection == 0
    widget.step_selection(-1)
    assert widget.selection == 0


def test_ply_count_matches_half_moves():
    # 14 half-moves -> 14 plies -> 15 selectable slots (home + each ply). A
    # wrong count would desync the wrap range and select nonexistent moves.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    assert widget.num_plies() == 14
    assert widget.total_selections() == 15


def test_lone_final_white_move_counts_as_one_ply():
    # An odd number of half-moves ends in a lone white move (black is None on the
    # last pair); num_plies must count it as a single ply, not two. Regression:
    # counting pairs*2 would over-count by one and allow selecting a phantom ply.
    widget = _widget(_game(RUY_LOPEZ_UCI + ["d1e2"]))  # 15 half-moves
    assert widget.num_plies() == 15
    assert widget.selected_is_white() is None  # home view still selected


def test_step_selection_wraps_both_ends():
    # From home, UP (-1) wraps to the last ply and a following DOWN (+1) returns
    # to home; this restores the board when stepping past the end. A regression
    # using clamping instead of modulo would stick on the last ply.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    assert widget.total_selections() == 15

    widget.step_selection(-1)
    assert widget.selection == 14           # wrapped from home to last ply
    assert widget.selected_ply() == 14

    widget.step_selection(1)
    assert widget.selection == 0            # wrapped from last ply back to home
    assert widget.selected_ply() is None

    widget.step_selection(1)
    assert widget.selection == 1            # forward into the first ply


def test_selected_half_move_side_alternates():
    # ply 1 is white's move, ply 2 is black's; selected_is_white must alternate so
    # the highlight lands on the correct half-move cell. Off-by-one parity would
    # box the wrong side.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    widget.step_selection(1)                # ply 1
    assert widget.selected_is_white() is True
    widget.step_selection(1)                # ply 2
    assert widget.selected_is_white() is False


def test_first_ply_window_is_oldest_partial_page():
    # Selecting ply 1 shows the oldest end-anchored window (only the first pair)
    # with the selection on its first row. A wrong window/mapping would show the
    # wrong moves or highlight the wrong row.
    widget = _widget(_game(RUY_LOPEZ_UCI))   # 7 pairs, 3 rows/page
    widget.step_selection(1)                 # ply 1
    assert widget.visible_rows() == [(1, "e4", "e5")]
    assert widget.selected_row_index() == 0


def test_middle_ply_window_and_row_index():
    # Selecting ply 7 (white of pair 4) shows the middle full window (pairs 2-4)
    # with the selection on the bottom row. Guards the page-for-ply computation
    # and the row index within the window.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    for _ in range(7):
        widget.step_selection(1)             # ply 7
    assert widget.selected_ply() == 7
    assert widget.visible_rows() == PAIRS_2_TO_4
    assert widget.selected_row_index() == 2
    assert widget.selected_is_white() is True


def test_last_ply_window_fills_space_newest_on_bottom():
    # Selecting the last ply shows a full tail window with the newest move on the
    # bottom row (the "scroll so the last is at the bottom" behavior), and the
    # selection lands on that bottom row's black half-move.
    widget = _widget(_game(RUY_LOPEZ_UCI))   # 7 pairs, 3 rows/page
    widget.step_selection(-1)                # last ply (14)
    rows = widget.visible_rows()
    assert len(rows) == widget._rows_per_page()   # full window: space is filled
    assert rows == [
        (5, "O-O", "\u2657e7"),        # O-O / Be7
        (6, "\u2656e1", "b5"),          # Re1 / b5
        (7, "\u2657b3", "d6"),          # Bb3 / d6 (newest, bottom row)
    ]
    assert widget.selected_row_index() == 2
    assert widget.selected_is_white() is False    # ply 14 is black's move


def test_view_follows_tail_when_move_played_while_reviewing():
    # While a move is selected, a new move must snap the selection to the newest
    # ply so live play stays visible even if the user had stepped back. Regression:
    # staying on the stale ply would keep coaching an old move during live play.
    game = _game(RUY_LOPEZ_UCI)              # 7 pairs / 14 plies
    widget = _widget(game)
    widget.step_selection(1)                 # review ply 1
    assert widget.selected_ply() == 1

    game.push_uci("d1e2")                    # white's 8th move -> ply 15 appears

    assert widget.num_plies() == 15
    assert widget.selected_ply() == widget.num_plies()   # snapped to newest ply
    assert widget.visible_rows()[-1] == (8, "\u2655e2", None)  # newest on bottom


def test_selection_change_callback_fires_on_stepping():
    # The board/coach swap and clock compact mode are driven by the selection
    # callback; it must fire with the new selection each step. A regression that
    # mutated the selection without notifying would leave the board/coach and the
    # clock indicator out of sync with the highlighted move.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    seen = []
    widget.set_selection_change_callback(seen.append)

    widget.step_selection(1)   # 0 -> 1
    widget.step_selection(1)   # 1 -> 2
    widget.step_selection(-1)  # 2 -> 1
    assert seen == [1, 2, 1]


def test_select_ply_sets_selection_and_notifies():
    # select_ply restores a coach selection without a key press (startup restore).
    # It must set the exact ply and fire the selection-change callback so the
    # board/coach swap happens. Regression: a restore that mutated selection
    # without notifying would rebuild the game screen on the board, not the coach
    # panel the user was viewing.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    seen = []
    widget.set_selection_change_callback(seen.append)

    widget.select_ply(7)

    assert widget.selection == 7
    assert widget.selected_ply() == 7
    assert seen == [7]


def test_select_ply_clamps_beyond_last_move():
    # A stale saved ply larger than the move count must clamp to the last played
    # ply, never selecting a phantom move. 99 is well past the 14 plies present.
    # Regression: an unclamped restore would index a nonexistent move and either
    # blank the coach panel or raise.
    widget = _widget(_game(RUY_LOPEZ_UCI))   # 14 plies

    widget.select_ply(99)

    assert widget.selection == widget.num_plies()
    assert widget.selected_ply() == 14


def test_select_ply_zero_selects_home_view():
    # Restoring selection 0 must return to the board (home) view. Starting from
    # a selected ply, select_ply(0) must notify with 0 so the board is restored.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    widget.select_ply(5)
    seen = []
    widget.set_selection_change_callback(seen.append)

    widget.select_ply(0)

    assert widget.selection == 0
    assert widget.selected_ply() is None
    assert seen == [0]


def test_selection_change_callback_silent_when_unchanged():
    # With no moves there is only the home view, so stepping is a no-op and
    # the callback must not fire (no spurious board/coach swaps).
    widget = _widget(_game([]))
    seen = []
    widget.set_selection_change_callback(seen.append)

    widget.step_selection(1)
    widget.step_selection(-1)
    assert seen == []


def test_selection_change_callback_fires_when_new_game_clamps():
    # Selecting a move then starting a new game drops all plies, so the selection
    # clamps back to 0 (home). The callback must fire with 0 so the board is
    # restored and the clock indicator returns; without it the board would stay
    # hidden behind the coach panel into the new game.
    game = _game(RUY_LOPEZ_UCI)
    widget = _widget(game)
    widget.step_selection(-1)                # to the last ply
    assert widget.selected_ply() == 14
    seen = []
    widget.set_selection_change_callback(seen.append)

    game.reset()                             # new game -> no plies -> clamp to 0
    assert widget.selection == 0
    assert seen == [0]


def test_render_selected_move_draws_highlight_without_sprites():
    # Rendering a selected move with no sprite sheet must not raise and must draw
    # ink (falls back to piece letters on the inverted highlight). Guards the
    # figurine fallback and highlight layout end-to-end on a real canvas.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    widget.step_selection(1)                 # select ply 1
    canvas = Image.new("1", (widget.width, widget.height), 255)
    widget.render(canvas)
    assert canvas.getextrema() == (0, 255)   # at least one black pixel drawn


def test_five_rows_fit_the_compact_timed_height():
    # The compact clock layout gives the move-list widget height 100 in timed
    # mode; the list must use that for five rows. Guards MOVE_LINE_HEIGHT tuning
    # -- a regression to 16px would fit only four, wasting nearly a row.
    widget = _widget(_game(RUY_LOPEZ_UCI), height=100)
    assert widget._rows_per_page() == 5


def _sized_widget(game_state, text_size, height=80):
    """A widget over game_state at a given Display text size."""
    return MoveListWidget(
        0, 216, 128, height, lambda full=False, immediate=False: None,
        notation="figurine",
        game_state=game_state,
        sprites=None,
        text_size=text_size,
    )


def test_text_size_scales_move_list_metrics_default_medium():
    # The Display > Text Size setting scales the move-list font, line height, and
    # header height. The default (no arg) must equal the class-level medium base so
    # the existing layout is unchanged; small shrinks and large grows. A regression
    # ignoring text_size would render every size at the medium metrics.
    from universalchess.epaper.text_scale import scale_font

    default = _widget(_game(RUY_LOPEZ_UCI))
    assert default.MOVE_FONT_SIZE == MoveListWidget.MOVE_FONT_SIZE
    assert default.MOVE_LINE_HEIGHT == MoveListWidget.MOVE_LINE_HEIGHT
    assert default.MOVE_HEADER_HEIGHT == MoveListWidget.MOVE_HEADER_HEIGHT

    small = _sized_widget(_game(RUY_LOPEZ_UCI), "small")
    large = _sized_widget(_game(RUY_LOPEZ_UCI), "large")
    assert small.MOVE_FONT_SIZE == scale_font(MoveListWidget.MOVE_FONT_SIZE, "small")
    assert large.MOVE_FONT_SIZE == scale_font(MoveListWidget.MOVE_FONT_SIZE, "large")
    assert small.MOVE_FONT_SIZE < default.MOVE_FONT_SIZE < large.MOVE_FONT_SIZE


def test_larger_text_size_fits_fewer_move_rows_per_page():
    # A larger move-list font increases the line height, so fewer rows fit the same
    # widget height and the move history spans more pages. This is the visible
    # effect of the setting; a regression scaling the font without the line height
    # would keep rows_per_page constant and overlap rows.
    small = _sized_widget(_game(RUY_LOPEZ_UCI), "small", height=100)
    large = _sized_widget(_game(RUY_LOPEZ_UCI), "large", height=100)
    assert large._rows_per_page() < small._rows_per_page()


def test_widget_does_not_depend_on_analysis_state():
    # Construction must not take or look up AnalysisState. Requiring it would
    # couple the list to live analysis and recreate the original bug (no list
    # when analysis is off). MagicMock is unused here on purpose: if the
    # constructor grows an analysis_state argument this call fails.
    widget = MoveListWidget(
        0, 216, 128, 80, lambda full=False, immediate=False: None,
        game_state=_game(RUY_LOPEZ_UCI),
    )
    assert widget.num_plies() == 14
    assert not hasattr(widget, "_analysis_state")
