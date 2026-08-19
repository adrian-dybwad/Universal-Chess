"""PLACE on a board that already matches must not form a move.

Background / why these tests exist
----------------------------------
The Centaur sometimes emits a PLACE with no preceding LIFT: a reed bounce after
the piece is already seated, a trailing duplicate after occupancy already
accepted the move, or a ghost PLACE on an occupied square. Player.on_piece_event
treats an empty lift buffer as a destination-only move. LichessPlayer then
rejects that unless the square is the pending destination, which a bounce on
the source or a bounce after turn-switch never is, and the board enters
correction mode. From the last Lichess game on dgt-64: PLACE c4 0.8s after
a5c4 was already accepted; PLACE e7 with the pawn still on e7; PLACE d2 while
pending d2e4 and the knight still on d2.

A PLACE that does not change occupancy, with no move in progress, is noise.
A real missed-lift move vacates a source square, so occupancy no longer matches
and must still be forwarded for destination-only recovery.
"""

from typing import Optional
from unittest.mock import Mock

import chess

from universalchess.managers.game.correction_mode import CorrectionMode
from universalchess.managers.game.field_events import FieldEventContext, process_field_event
from universalchess.managers.game.move_state import INVALID_SQUARE, MoveState


def _presence(board: chess.Board) -> bytearray:
    state = bytearray(64)
    for sq in chess.SQUARES:
        state[sq] = 1 if board.piece_at(sq) is not None else 0
    return state


class _PlayerManagerStub:
    def __init__(self, pending_move: Optional[chess.Move] = None):
        self._pending_move = pending_move

    def get_current_pending_move(self, _board: chess.Board):
        return self._pending_move


def _ctx(
    chess_board: chess.Board,
    *,
    pending_move: Optional[chess.Move] = None,
    physical: Optional[bytearray] = None,
    move_state: Optional[MoveState] = None,
) -> tuple[FieldEventContext, Mock, Mock, Mock]:
    on_piece_event_fn = Mock()
    enter_correction_mode_fn = Mock()
    execute_pending_move_fn = Mock()
    on_player_move_fn = Mock(return_value=False)

    board_module = Mock()
    board_module.getChessState.return_value = (
        physical if physical is not None else _presence(chess_board)
    )
    board_module.beep = Mock()
    board_module.SOUND_WRONG_MOVE = 0

    ctx = FieldEventContext(
        chess_board=chess_board,
        move_state=move_state if move_state is not None else MoveState(),
        correction_mode=CorrectionMode(),
        player_manager=_PlayerManagerStub(pending_move),
        board_module=board_module,
        led=Mock(),
        event_callback=None,
        enter_correction_mode_fn=enter_correction_mode_fn,
        provide_correction_guidance_fn=Mock(),
        handle_field_event_in_correction_mode_fn=Mock(),
        handle_piece_event_without_player_fn=Mock(),
        on_piece_event_fn=on_piece_event_fn,
        on_player_move_fn=on_player_move_fn,
        handle_king_lift_resign_fn=Mock(),
        execute_pending_move_fn=execute_pending_move_fn,
        check_takeback_fn=Mock(return_value=False),
        get_kings_in_center_menu_active_fn=lambda: False,
        set_kings_in_center_menu_active_fn=lambda _v: None,
        on_kings_in_center_cancel_fn=None,
        get_king_lift_resign_menu_active_fn=lambda: False,
        set_king_lift_resign_menu_active_fn=lambda _v: None,
        on_king_lift_resign_cancel_fn=None,
        chess_board_to_state_fn=_presence,
    )
    return ctx, on_piece_event_fn, enter_correction_mode_fn, execute_pending_move_fn


def test_trailing_place_after_accepted_move_is_not_forwarded() -> None:
    """A second PLACE on the destination after the move is already in chess_board is ignored.

    Why: occupancy accepted a5c4, then 0.8s later the reed fired PLACE c4 again
    with an empty lift buffer. Turn had already switched, so LichessPlayer
    formed c4c4 with no pending server move and entered correction.

    How a regression manifests: on_piece_event_fn is called with place/c4, which
    is the destination-only path that becomes move_mismatch.
    """
    board = chess.Board("r2k1q1r/2p4p/p3pp2/n7/3PB2Q/2P2P2/PP4PP/R3K1NR b KQ - 0 17")
    board.push(chess.Move.from_uci("a5c4"))

    ctx, on_piece_event_fn, enter_correction_mode_fn, execute_pending_move_fn = _ctx(board)

    process_field_event(ctx, piece_event=1, field=chess.C4, time_in_seconds=0.0)

    on_piece_event_fn.assert_not_called()
    enter_correction_mode_fn.assert_not_called()
    execute_pending_move_fn.assert_not_called()


def test_ghost_place_on_occupied_square_is_not_forwarded() -> None:
    """PLACE on a square that already has a piece, with no lift in progress, is ignored.

    Why: five seconds after g7g5 the board emitted PLACE e7 with no LIFT e7.
    The pawn was still on e7. Destination-only e7e7 had no pending Lichess move
    (White to play) and entered correction.

    How a regression manifests: on_piece_event_fn is called for place/e7.
    """
    board = chess.Board("rnbqkbnr/ppp1pp1p/8/3p2p1/3P1B2/8/PPP1PPPP/RN1QKBNR w KQkq - 0 3")
    ctx, on_piece_event_fn, enter_correction_mode_fn, _ = _ctx(board)

    process_field_event(ctx, piece_event=1, field=chess.E7, time_in_seconds=0.0)

    on_piece_event_fn.assert_not_called()
    enter_correction_mode_fn.assert_not_called()


def test_ghost_place_on_pending_source_does_not_mismatch() -> None:
    """PLACE on the pending source while the piece is still there is ignored.

    Why: pending capture d2e4, user had just put the d3 bishop back, then PLACE
    d2 arrived with no LIFT. Destination-only d2 does not match pending to-square
    e4, so LichessPlayer reported move_mismatch. The real LIFT d2 arrived 100ms
    later, into correction mode.

    How a regression manifests: on_piece_event_fn is called (destination-only d2)
    or execute_pending_move_fn runs (the knight has not moved).
    """
    board = chess.Board("r1bk1q1r/2p4p/p3pp2/n7/3Pp2Q/2PB4/PP1N1PPP/R3K1NR w KQ - 0 15")
    pending = chess.Move.from_uci("d2e4")
    ctx, on_piece_event_fn, enter_correction_mode_fn, execute_pending_move_fn = _ctx(
        board, pending_move=pending
    )

    process_field_event(ctx, piece_event=1, field=chess.D2, time_in_seconds=0.0)

    on_piece_event_fn.assert_not_called()
    enter_correction_mode_fn.assert_not_called()
    execute_pending_move_fn.assert_not_called()


def test_place_back_on_lifted_source_still_forwards() -> None:
    """Putting the lifted piece back must still reach the player as piece_returned.

    Why: a PLACE that matches occupancy is cancel, not noise, when a move is in
    progress. Swallowing it would leave the player's lift buffer holding the
    source, so the next PLACE anywhere would form a move from that stale square.

    How a regression manifests: on_piece_event_fn is not called after lift e2
    and place e2, so the buffer is never cleared.
    """
    board = chess.Board()
    move_state = MoveState()
    ctx, on_piece_event_fn, enter_correction_mode_fn, _ = _ctx(board, move_state=move_state)

    process_field_event(ctx, piece_event=0, field=chess.E2, time_in_seconds=0.0)
    assert move_state.source_square == chess.E2

    process_field_event(ctx, piece_event=1, field=chess.E2, time_in_seconds=0.1)

    on_piece_event_fn.assert_called_with("place", chess.E2, board)
    enter_correction_mode_fn.assert_not_called()
    assert move_state.source_square == INVALID_SQUARE


def test_missed_lift_place_on_changed_board_is_still_forwarded() -> None:
    """A PLACE that actually changed occupancy is still a destination-only recovery.

    Why: the ignore path is occupancy already matching. A missed source lift
    leaves the source empty, so occupancy does not match, and GameManager must
    still see the PLACE to infer the source.

    How a regression manifests: on_piece_event_fn is not called for place/e4
    when e2 is vacant and e4 occupied, so missed-lift e2e4 is never formed.
    """
    board = chess.Board()
    after = board.copy()
    after.push(chess.Move.from_uci("e2e4"))
    ctx, on_piece_event_fn, enter_correction_mode_fn, _ = _ctx(
        board, physical=_presence(after)
    )

    process_field_event(ctx, piece_event=1, field=chess.E4, time_in_seconds=0.0)

    on_piece_event_fn.assert_called_with("place", chess.E4, board)
    enter_correction_mode_fn.assert_not_called()


def test_pending_destination_place_still_executes_when_occupancy_matches_after() -> None:
    """Completing a pending move must not be swallowed by the matching-board guard.

    Why: the guard compares against the current (pre-move) logical board. A
    PLACE on the pending destination matches the post-move board, not the
    current one, and must still execute.

    How a regression manifests: execute_pending_move_fn is not called for e3e4
    when occupancy already shows the pawn on e4.
    """
    board = chess.Board("r1bk1q1r/2p4p/p3pp2/n2p4/3P3Q/2PBP3/PP1N1PPP/R3K1NR w KQ - 0 14")
    pending = chess.Move.from_uci("e3e4")
    after = board.copy()
    after.push(pending)
    ctx, on_piece_event_fn, enter_correction_mode_fn, execute_pending_move_fn = _ctx(
        board, pending_move=pending, physical=_presence(after)
    )

    process_field_event(ctx, piece_event=1, field=chess.E4, time_in_seconds=0.0)

    execute_pending_move_fn.assert_called_once_with(pending)
    on_piece_event_fn.assert_not_called()
    enter_correction_mode_fn.assert_not_called()
