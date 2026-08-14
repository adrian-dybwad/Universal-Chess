"""Tests for taking the live game back to a reviewed move-list ply.

Why these tests exist
---------------------
UP/DOWN during a game highlights a played move. OK then offers "Take back to
this position", which must undo every later half-move so the highlighted ply
becomes the live tip -- not a one-move physical takeback, and not a reset to
the start. These tests pin that truncation (count, remaining UCIs, DB deletes,
player notification, a single beep, and correction when the physical board
still shows the discarded tail) independently of the overlay menu.
"""

from unittest.mock import Mock

import chess
import pytest

import universalchess.managers.game.game_manager as gm_module
from universalchess.managers.game.game_manager import GameManager
from universalchess.managers.game.move_state import MoveState
from universalchess.state.chess_game import reset_chess_game

# Three plies so a mid-list selection (ply 1 of 3) is distinct from both the
# opening and the live tip. e4 e5 Nf3 is legal and unambiguous.
OPENING_UCI = ["e2e4", "e7e5", "g1f3"]


def _manager(moves_uci):
    """GameManager with a real game state and the collaborators takeback_to_ply uses.

    Built with ``__new__`` so hardware/DB/thread startup is skipped. The physical
    board state is injected via the patched ``board.getChessState*`` in each test.
    """
    state = reset_chess_game()
    for uci in moves_uci:
        state.push_uci(uci)

    mgr = GameManager.__new__(GameManager)
    mgr._game_state = state
    mgr.chess_board = state.board
    mgr.takeback_callback = Mock()
    mgr.move_state = MoveState()
    mgr.database_session = object()
    mgr.game_db_id = 7
    mgr.cached_result = "1-0"
    mgr._led = Mock()
    mgr._enter_correction_mode = Mock()
    mgr._provide_correction_guidance = Mock()
    return mgr


@pytest.fixture
def patched_board(monkeypatch):
    """Stub the physical board and the per-move DB helpers takeback_to_ply calls."""
    board_mod = Mock()
    board_mod.SOUND_GENERAL = 1
    board_mod.beep = Mock()
    # Default: physical board still shows the live (un-taken-back) position, so
    # a takeback to an earlier ply must enter correction. Individual tests
    # override getChessState* when they need a matching board.
    board_mod.getChessState = Mock(return_value=bytearray(64))
    board_mod.getChessStateLowPriority = Mock(return_value=bytearray(64))
    monkeypatch.setattr(gm_module, "board", board_mod)

    delete_last = Mock()
    clear_result = Mock()
    monkeypatch.setattr(gm_module, "delete_last_move", delete_last)
    monkeypatch.setattr(gm_module, "clear_game_result", clear_result)
    return board_mod, delete_last, clear_result


def test_history_position_at_ply_is_the_board_after_that_move():
    """New game from a highlighted ply must start from the position AFTER that move.

    Why: history_positions()[0] is the opening; [ply] is the board the
    highlighted move produced. Using [ply-1] would start from the position
    before the move (undoing it in the fork) and using the live fen would
    ignore the review cursor. How a regression manifests: the FEN after ply 1
    is still the starting position, or equals the 3-ply tip.
    """
    mgr = _manager(OPENING_UCI)
    positions = mgr._game_state.history_positions()
    after_e4 = chess.Board()
    after_e4.push_uci("e2e4")
    assert positions[1]["fen"] == after_e4.fen()
    assert positions[1]["uci"] == "e2e4"
    assert positions[3]["fen"] == mgr._game_state.fen


def test_pops_moves_after_the_highlighted_ply(patched_board):
    """The highlighted ply stays; every later half-move is undone.

    Why: "Take back to this position" means the reviewed move becomes the live
    last move, not that the reviewed move itself is undone. How a regression
    manifests: remaining UCIs still include Nf3 (popped too few) or drop e4
    (popped the highlighted ply too), or the return count is not 2.
    """
    mgr = _manager(OPENING_UCI)

    popped = mgr.takeback_to_ply(1)

    assert popped == 2
    assert [m.uci() for m in mgr.chess_board.move_stack] == ["e2e4"]


def test_no_op_when_highlighted_ply_is_already_the_tip(patched_board):
    """Highlighting the last played move has nothing to take back.

    Why: the live position already is "this position"; popping would undo the
    highlighted move itself, which is a different action. How a regression
    manifests: move_stack shrinks, takeback_callback fires, or a non-zero
    count is returned.
    """
    board_mod, delete_last, _ = patched_board
    mgr = _manager(OPENING_UCI)

    popped = mgr.takeback_to_ply(3)

    assert popped == 0
    assert [m.uci() for m in mgr.chess_board.move_stack] == OPENING_UCI
    mgr.takeback_callback.assert_not_called()
    delete_last.assert_not_called()
    board_mod.beep.assert_not_called()


def test_no_op_for_non_played_ply(patched_board):
    """A ply that was never played (0, or past the tip) must not mutate the game.

    Why: selection 0 is the analysis view, not a move, and a stale/out-of-range
    ply must not empty the game. How a regression manifests: move_stack is
    cleared or takeback_callback fires for ply 0 / ply 99.
    """
    mgr = _manager(OPENING_UCI)

    assert mgr.takeback_to_ply(0) == 0
    assert mgr.takeback_to_ply(99) == 0
    assert [m.uci() for m in mgr.chess_board.move_stack] == OPENING_UCI
    mgr.takeback_callback.assert_not_called()


def test_deletes_one_db_row_per_popped_move(patched_board):
    """Each undone ply must drop its GameMove row so resume cannot replay it.

    Why: the single-move physical takeback deletes one row; a multi-ply takeback
    that deleted only once would leave trailing moves in the database and a
    restart would restore the discarded tail. How a regression manifests:
    delete_last_move call count is not equal to the number of popped plies.
    """
    _, delete_last, clear_result = patched_board
    mgr = _manager(OPENING_UCI)

    mgr.takeback_to_ply(1)

    assert delete_last.call_count == 2
    clear_result.assert_called()
    assert mgr.cached_result is None


def test_notifies_takeback_once_per_popped_move(patched_board):
    """Analysis scores and coach cache are keyed per ply; each pop must notify.

    Why: the existing takeback callback removes one analysis score and
    invalidates one coach ply. Firing it once for a 2-ply takeback would leave
    the discarded move's eval/coach on the now-shorter game. How a regression
    manifests: takeback_callback call count is 1 (or 0) instead of 2.
    """
    mgr = _manager(OPENING_UCI)

    mgr.takeback_to_ply(1)

    assert mgr.takeback_callback.call_count == 2


def test_beeps_once_for_a_multi_ply_takeback(patched_board):
    """A 2-ply takeback must not beep twice.

    Why: each physical takeback beeps once as confirmation; looping that for
    every popped ply would chirp for each discarded move. How a regression
    manifests: beep call count is 2 (or 0) instead of 1.
    """
    board_mod, _, _ = patched_board
    mgr = _manager(OPENING_UCI)

    mgr.takeback_to_ply(1)

    assert board_mod.beep.call_count == 1


def test_enters_correction_when_physical_board_still_shows_discarded_moves(patched_board):
    """After truncating the logical game, mismatched pieces must be guided back.

    Why: reviewing does not move the physical pieces, so taking back to an
    earlier ply always leaves the board showing the discarded tail. Skipping
    correction would leave play on a position the pieces do not match. How a
    regression manifests: _enter_correction_mode is not called.
    """
    mgr = _manager(OPENING_UCI)
    # A 64-byte empty board cannot match the post-e4 position.
    patched_board[0].getChessStateLowPriority.return_value = bytearray(64)

    mgr.takeback_to_ply(1)

    mgr._enter_correction_mode.assert_called_once()
    mgr._provide_correction_guidance.assert_called_once()


def test_skips_correction_when_physical_board_already_matches(patched_board):
    """If the pieces already show the target, do not enter correction.

    Why: a one-ply takeback after the user has already replaced the piece
    (the physical-takeback path) must not flash correction LEDs. How a
    regression manifests: _enter_correction_mode is called even though
    getChessStateLowPriority matches the truncated position.
    """
    mgr = _manager(OPENING_UCI)
    # Occupancy of the position after 1. e4 -- what the physical board must
    # already show for correction to be skipped after popping back to ply 1.
    preview = chess.Board()
    preview.push_uci("e2e4")
    matching = bytearray(64)
    for square in range(64):
        matching[square] = 1 if preview.piece_at(square) is not None else 0
    patched_board[0].getChessStateLowPriority.return_value = matching

    mgr.takeback_to_ply(1)

    mgr._enter_correction_mode.assert_not_called()
