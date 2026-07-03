"""Tests for GameAnalysisWidget per-ply move selection.

Why these tests exist
---------------------
The analysis widget replaced page-level move paging with a single selection
cursor over [analysis, ply 1 .. ply N] driven by the physical UP/DOWN keys:
selecting a ply highlights that half-move in the move list (while the board area
is swapped for the move's coach statement), and stepping past the last ply wraps
back to the analysis view (restoring the board). These tests pin the cursor
arithmetic, the ply->row mapping/highlight, the end-anchored window, tail-follow
on a new move, and the selection-change callback independently of pixel
rendering, because a regression (wrong ply count, broken wrap, off-by-one row
mapping, wrong highlighted half-move, or a lost tail-follow) would otherwise only
show as the wrong move being coached with no other signal.
"""

from unittest.mock import MagicMock

from PIL import Image

from universalchess.epaper.game_analysis import GameAnalysisWidget
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
    """Build a widget over a real game state with a stubbed analysis state."""
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


def test_no_moves_has_only_analysis_selection():
    # A fresh game has only the analysis view: zero plies, and stepping must not
    # move off selection 0 (modulo-1 stays 0). Guards a divide/blank bug when the
    # move list is empty.
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
    # 14 half-moves -> 14 plies -> 15 selectable slots (analysis + each ply). A
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
    assert widget.selected_is_white() is None  # analysis view still selected


def test_step_selection_wraps_both_ends():
    # From the analysis view, UP (-1) wraps to the last ply and a following DOWN
    # (+1) returns to the analysis view; this restores the board when stepping
    # past the end. A regression using clamping instead of modulo would stick.
    widget = _widget(_game(RUY_LOPEZ_UCI))
    assert widget.total_selections() == 15

    widget.step_selection(-1)
    assert widget.selection == 14           # wrapped from analysis to last ply
    assert widget.selected_ply() == 14

    widget.step_selection(1)
    assert widget.selection == 0            # wrapped from last ply back to analysis
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


def test_selection_change_callback_silent_when_unchanged():
    # With no moves there is only the analysis view, so stepping is a no-op and
    # the callback must not fire (no spurious board/coach swaps).
    widget = _widget(_game([]))
    seen = []
    widget.set_selection_change_callback(seen.append)

    widget.step_selection(1)
    widget.step_selection(-1)
    assert seen == []


def test_selection_change_callback_fires_when_new_game_clamps():
    # Selecting a move then starting a new game drops all plies, so the selection
    # clamps back to 0 (analysis view). The callback must fire with 0 so the board
    # is restored and the clock indicator returns; without it the board would stay
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


def test_render_score_change_does_not_request_a_refresh():
    # Rendering a changed eval score must not trigger an extra display refresh:
    # the score/annotation TextWidgets are render-only helpers whose set_text() is
    # called only inside render(). Forwarding their request_update() to the Manager
    # would double the analysis refresh rate and starve the clock. Regression: the
    # update_callback would be invoked purely as a side effect of rendering.
    analysis_state = MagicMock()
    analysis_state.is_mate = False
    analysis_state.mate_in = None
    analysis_state.annotation = ""
    analysis_state.history = []
    analysis_state.score = 1.0
    analysis_state.score_text = "+1.0"

    update_callback = MagicMock()
    widget = GameAnalysisWidget(
        0, 216, 128, 80, update_callback,
        analysis_state=analysis_state,
        notation="figurine",
        game_state=_game([]),
        sprites=None,
    )

    img = Image.new("1", (widget.width, widget.height), 255)
    widget.render(img)              # first render sets the score text to "+1.0"

    analysis_state.score = 2.0      # change so the next set_text() actually changes
    update_callback.reset_mock()

    widget.render(img)
    update_callback.assert_not_called()


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
    # The compact clock layout gives the analysis widget height 100 in timed mode;
    # the move list must use that for five rows. Guards MOVE_LINE_HEIGHT tuning --
    # a regression to 16px would fit only four, wasting nearly a row.
    widget = _widget(_game(RUY_LOPEZ_UCI), height=100)
    assert widget._rows_per_page() == 5
