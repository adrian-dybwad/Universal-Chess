"""Correction while waiting for a Lichess/engine ply.

Background / why these tests exist
----------------------------------
When it is the remote side's turn, lifting one of their pieces looks like a
legal move start (that colour has legal moves). Occupancy that matches a legal
resulting position was then accepted as a local ply, so an opponent pawn
nudged e7-e5 never entered correction. When the server later sent a different
(or the same) move, from-to LEDs overwrote any remaining mismatch, the user
transcribed that ply, and correction compared to the *pre-move* board: it
asked them to put the correctly moved piece back and never pointed at the
piece that was actually out of place.
"""

from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock

import chess

from universalchess.managers.game.correction_mode import CorrectionMode
from universalchess.managers.game.field_events import FieldEventContext, process_field_event
from universalchess.managers.game.move_state import MoveState
from universalchess.players.base import PlayerType


def _presence(board: chess.Board) -> bytearray:
    state = bytearray(64)
    for sq in chess.SQUARES:
        state[sq] = 1 if board.piece_at(sq) is not None else 0
    return state


class _RemotePlayerManager:
    """Side to move is a remote/engine player waiting for (or holding) a pending ply."""

    def __init__(
        self,
        pending_move: Optional[chess.Move] = None,
        player_type: PlayerType = PlayerType.REMOTE,
    ):
        self._pending_move = pending_move
        self._player = SimpleNamespace(
            player_type=player_type, pending_move=pending_move
        )

    def get_current_pending_move(self, _board: chess.Board):
        return self._pending_move

    def get_current_player(self, _board: chess.Board):
        return self._player


def _after_e4() -> chess.Board:
    board = chess.Board()
    board.push(chess.Move.from_uci("e2e4"))
    return board


def _ctx(
    chess_board: chess.Board,
    *,
    pending_move: Optional[chess.Move] = None,
    physical: Optional[bytearray] = None,
    move_state: Optional[MoveState] = None,
    correction_mode: Optional[CorrectionMode] = None,
    player_type: PlayerType = PlayerType.REMOTE,
    execute_pending_move_fn=None,
):
    on_player_move_fn = Mock(return_value=False)
    enter_correction_mode_fn = Mock()
    provide_correction_guidance_fn = Mock()
    if execute_pending_move_fn is None:
        execute_pending_move_fn = Mock()
    handle_field_event_in_correction_mode_fn = Mock()
    on_piece_event_fn = Mock()

    board_module = Mock()
    board_module.getChessState.return_value = (
        physical if physical is not None else _presence(chess_board)
    )
    board_module.beep = Mock()
    board_module.SOUND_WRONG_MOVE = 0

    ctx = FieldEventContext(
        chess_board=chess_board,
        move_state=move_state if move_state is not None else MoveState(),
        correction_mode=correction_mode if correction_mode is not None else CorrectionMode(),
        player_manager=_RemotePlayerManager(pending_move, player_type),
        board_module=board_module,
        led=Mock(),
        event_callback=None,
        enter_correction_mode_fn=enter_correction_mode_fn,
        provide_correction_guidance_fn=provide_correction_guidance_fn,
        handle_field_event_in_correction_mode_fn=handle_field_event_in_correction_mode_fn,
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
    return (
        ctx,
        on_player_move_fn,
        enter_correction_mode_fn,
        provide_correction_guidance_fn,
        execute_pending_move_fn,
        handle_field_event_in_correction_mode_fn,
        board_module,
    )


def test_waiting_remote_legal_pawn_nudge_enters_correction_not_a_local_move() -> None:
    """Nudging an opponent pawn to a legal square while waiting must not play it.

    Why: occupancy of e7e5 matches a legal resulting position, so the wait for
    Lichess accepted that layout as a local ply and never entered correction.
    How a regression manifests: on_player_move_fn is called with e7e5, or
    enter_correction_mode_fn is not called, so the logical board has a ply
    Lichess never sent.
    """
    chess_board = _after_e4()
    physical = _presence(chess_board)
    ctx, on_player_move_fn, enter_correction, guidance, *_rest, board_module = (
        _ctx(chess_board, physical=physical)
    )

    physical[chess.E7] = 0
    board_module.getChessState.return_value = physical
    process_field_event(ctx, piece_event=0, field=chess.E7, time_in_seconds=0.0)

    physical[chess.E5] = 1
    board_module.getChessState.return_value = physical
    process_field_event(ctx, piece_event=1, field=chess.E5, time_in_seconds=0.1)

    enter_correction.assert_called_once()
    on_player_move_fn.assert_not_called()
    assert chess_board.piece_at(chess.E7) is not None
    assert chess_board.piece_at(chess.E5) is None
    guidance.assert_called_once()
    current, expected = guidance.call_args[0]
    assert current[chess.E5] == 1
    assert expected[chess.E7] == 1
    assert expected[chess.E5] == 0


def test_waiting_remote_illegal_displacement_enters_correction() -> None:
    """An opponent knight parked on a non-destination must enter correction.

    Why: that colour is to move, so the lift has legal moves and was not the
    'opponent piece / no legal moves' path. The PLACE then has to be the
    mismatch, not a formed remote move. How a regression manifests:
    enter_correction_mode_fn is not called after PLACE b6.
    """
    chess_board = _after_e4()
    physical = _presence(chess_board)
    ctx, on_player_move_fn, enter_correction, guidance, *_rest, board_module = (
        _ctx(chess_board, physical=physical)
    )

    physical[chess.B8] = 0
    board_module.getChessState.return_value = physical
    process_field_event(ctx, piece_event=0, field=chess.B8, time_in_seconds=0.0)

    physical[chess.B6] = 1
    board_module.getChessState.return_value = physical
    process_field_event(ctx, piece_event=1, field=chess.B6, time_in_seconds=0.1)

    enter_correction.assert_called_once()
    on_player_move_fn.assert_not_called()
    current, expected = guidance.call_args[0]
    assert current[chess.B6] == 1
    assert expected[chess.B8] == 1
    assert expected[chess.B6] == 0


def test_waiting_remote_put_back_does_not_enter_correction() -> None:
    """Putting the lifted opponent piece back on its square is not a mismatch.

    Why: a lift-and-replace while waiting must stay a no-op. How a regression
    manifests: enter_correction_mode_fn is called after PLACE e7.
    """
    chess_board = _after_e4()
    physical = _presence(chess_board)
    ctx, on_player_move_fn, enter_correction, _guidance, *_rest, board_module = (
        _ctx(chess_board, physical=physical)
    )

    physical[chess.E7] = 0
    board_module.getChessState.return_value = physical
    process_field_event(ctx, piece_event=0, field=chess.E7, time_in_seconds=0.0)

    physical[chess.E7] = 1
    board_module.getChessState.return_value = physical
    process_field_event(ctx, piece_event=1, field=chess.E7, time_in_seconds=0.1)

    enter_correction.assert_not_called()
    on_player_move_fn.assert_not_called()


def test_correction_transcribe_of_pending_with_other_mismatch_executes_pending() -> None:
    """Transcribing the indicated ply while another piece is out of place must apply it.

    Why: correction compares to the pre-move board. Occupancy after e7e5 with
    the knight still on a6 does not match post-pending, so the PLACE was
    treated as a remaining mismatch and Hungarian guided e5 back to e7.
    How a regression manifests: the pending ply is not on the logical board, or
    guidance still has e5 extra / e7 missing (undoing the transcribed ply)
    instead of a6 extra / b8 missing.
    """
    chess_board = _after_e4()
    pending = chess.Move.from_uci("e7e5")
    after = chess_board.copy()
    after.push(pending)
    physical = _presence(after)
    physical[chess.B8] = 0
    physical[chess.A6] = 1

    def execute(move: chess.Move) -> None:
        chess_board.push(move)

    correction_mode = CorrectionMode()
    correction_mode.enter(_presence(chess_board))
    move_state = MoveState()
    ctx, _on_move, _enter, guidance, execute_pending, handle_correction, board_module = (
        _ctx(
            chess_board,
            pending_move=pending,
            physical=physical,
            move_state=move_state,
            correction_mode=correction_mode,
            execute_pending_move_fn=execute,
        )
    )

    process_field_event(ctx, piece_event=0, field=chess.E7, time_in_seconds=0.0)
    assert move_state.pending_move_source_lifted == chess.E7

    process_field_event(ctx, piece_event=1, field=chess.E5, time_in_seconds=0.1)

    assert chess_board.peek() == pending
    assert all(call.args[0] != 1 for call in handle_correction.call_args_list)
    current, expected = guidance.call_args[0]
    assert expected[chess.E5] == 1
    assert expected[chess.E7] == 0
    assert current[chess.A6] == 1
    assert expected[chess.B8] == 1
    assert expected[chess.A6] == 0


def test_human_turn_still_accepts_legal_occupancy() -> None:
    """A human-to-move occupancy match must still play the move.

    Why: the remote-wait guard is only for engine/Lichess turns. How a
    regression manifests: enter_correction_mode_fn is called for e2e4, or
    on_player_move_fn is not.
    """
    chess_board = chess.Board()
    after = chess_board.copy()
    after.push(chess.Move.from_uci("e2e4"))
    physical = _presence(chess_board)
    ctx, on_player_move_fn, enter_correction, _g, *_rest, board_module = _ctx(
        chess_board,
        physical=physical,
        player_type=PlayerType.HUMAN,
    )

    physical[chess.E2] = 0
    board_module.getChessState.return_value = physical
    process_field_event(ctx, piece_event=0, field=chess.E2, time_in_seconds=0.0)

    physical[chess.E4] = 1
    board_module.getChessState.return_value = physical
    on_player_move_fn.return_value = True
    process_field_event(ctx, piece_event=1, field=chess.E4, time_in_seconds=0.1)

    on_player_move_fn.assert_called_once()
    enter_correction.assert_not_called()
