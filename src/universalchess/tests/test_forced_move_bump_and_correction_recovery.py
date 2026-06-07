from typing import Optional

import chess
from unittest.mock import Mock

from universalchess.managers.game.field_events import FieldEventContext, process_field_event
from universalchess.managers.game.move_state import MoveState
from universalchess.managers.game.correction_mode import CorrectionMode


def _piece_presence_state(board: chess.Board) -> bytearray:
    state = bytearray(64)
    for sq in chess.SQUARES:
        state[sq] = 1 if board.piece_at(sq) is not None else 0
    return state


class _PlayerManagerStub:
    def __init__(self, pending_move: Optional[chess.Move]):
        self._pending_move = pending_move

    def get_current_pending_move(self, _board: chess.Board):
        return self._pending_move


def test_forced_move_bump_after_source_lift_does_not_enter_correction_mode() -> None:
    # Expected failure message if broken: "enter_correction_mode_fn called on bump during forced move"
    # Why: After lifting the forced move source piece, bumping another piece must not trigger correction mode.
    chess_board = chess.Board("rnbqkb1r/pppp1ppp/8/4p3/2P1n3/PP6/3P1PPP/RNBQKBNR b KQkq - 0 4")
    pending_move = chess.Move.from_uci("f8c5")

    move_state = MoveState()
    correction_mode = CorrectionMode()

    board_module = Mock()
    board_module.getChessState.return_value = None
    board_module.beep = Mock()
    board_module.SOUND_WRONG_MOVE = 0

    enter_correction_mode_fn = Mock()
    provide_correction_guidance_fn = Mock()

    ctx = FieldEventContext(
        chess_board=chess_board,
        move_state=move_state,
        correction_mode=correction_mode,
        player_manager=_PlayerManagerStub(pending_move),
        board_module=board_module,
        led=Mock(),
        event_callback=None,
        enter_correction_mode_fn=enter_correction_mode_fn,
        provide_correction_guidance_fn=provide_correction_guidance_fn,
        handle_field_event_in_correction_mode_fn=Mock(),
        handle_piece_event_without_player_fn=Mock(),
        on_piece_event_fn=Mock(),
        on_player_move_fn=Mock(return_value=False),
        handle_king_lift_resign_fn=Mock(),
        execute_pending_move_fn=Mock(),
        check_takeback_fn=Mock(return_value=False),
        get_kings_in_center_menu_active_fn=lambda: False,
        set_kings_in_center_menu_active_fn=lambda _v: None,
        on_kings_in_center_cancel_fn=None,
        get_king_lift_resign_menu_active_fn=lambda: False,
        set_king_lift_resign_menu_active_fn=lambda _v: None,
        on_king_lift_resign_cancel_fn=None,
        chess_board_to_state_fn=lambda _b: None,
    )

    # Lift forced move source (f8)
    process_field_event(ctx, piece_event=0, field=chess.F8, time_in_seconds=0.0)
    assert move_state.pending_move_source_lifted == chess.F8

    # Bump a3 (white pawn) - must NOT trigger correction mode even though pawn has no legal moves on black turn.
    process_field_event(ctx, piece_event=0, field=chess.A3, time_in_seconds=0.1)

    enter_correction_mode_fn.assert_not_called()


def test_pending_move_executes_even_if_correction_mode_active_when_board_matches_post_move() -> None:
    # Expected failure message if broken: "execute_pending_move_fn not called while in correction mode"
    # Why: If correction mode is active and the forced move is physically completed, the move must be accepted.
    chess_board = chess.Board("rnbqkb1r/pppp1ppp/8/4p3/2P1n3/PP6/3P1PPP/RNBQKBNR b KQkq - 0 4")
    pending_move = chess.Move.from_uci("f8c5")

    expected_after_board = chess_board.copy()
    expected_after_board.push(pending_move)
    expected_after_state = _piece_presence_state(expected_after_board)

    move_state = MoveState()
    correction_mode = CorrectionMode()
    correction_mode.enter(_piece_presence_state(chess_board))

    board_module = Mock()
    board_module.getChessState.return_value = expected_after_state
    board_module.beep = Mock()

    execute_pending_move_fn = Mock()
    handle_field_event_in_correction_mode_fn = Mock()

    ctx = FieldEventContext(
        chess_board=chess_board,
        move_state=move_state,
        correction_mode=correction_mode,
        player_manager=_PlayerManagerStub(pending_move),
        board_module=board_module,
        led=Mock(),
        event_callback=None,
        enter_correction_mode_fn=Mock(),
        provide_correction_guidance_fn=Mock(),
        handle_field_event_in_correction_mode_fn=handle_field_event_in_correction_mode_fn,
        handle_piece_event_without_player_fn=Mock(),
        on_piece_event_fn=Mock(),
        on_player_move_fn=Mock(return_value=False),
        handle_king_lift_resign_fn=Mock(),
        execute_pending_move_fn=execute_pending_move_fn,
        check_takeback_fn=Mock(return_value=False),
        get_kings_in_center_menu_active_fn=lambda: False,
        set_kings_in_center_menu_active_fn=lambda _v: None,
        on_kings_in_center_cancel_fn=None,
        get_king_lift_resign_menu_active_fn=lambda: False,
        set_king_lift_resign_menu_active_fn=lambda _v: None,
        on_king_lift_resign_cancel_fn=None,
        chess_board_to_state_fn=lambda b: _piece_presence_state(b),
    )

    # While in correction mode, PLACE on c5 should accept the pending move if the full physical state matches post-move.
    process_field_event(ctx, piece_event=1, field=chess.C5, time_in_seconds=1.0)

    execute_pending_move_fn.assert_called_once_with(pending_move)
    handle_field_event_in_correction_mode_fn.assert_not_called()


def test_pending_capture_allows_lifting_capture_square_first_without_correction_mode() -> None:
    # Expected failure message if broken: "enter_correction_mode_fn called when lifting capture square for pending capture"
    # Why: For a pending capture, players often lift/remove the captured piece first. This must not trigger correction mode.
    chess_board = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/3PP3/8/PPP2PPP/RNBQKBNR b KQkq - 0 2")
    pending_move = chess.Move.from_uci("e5d4")  # capture on d4

    move_state = MoveState()
    correction_mode = CorrectionMode()

    board_module = Mock()
    board_module.getChessState.return_value = None
    board_module.beep = Mock()
    board_module.SOUND_WRONG_MOVE = 0

    enter_correction_mode_fn = Mock()
    provide_correction_guidance_fn = Mock()
    on_piece_event_fn = Mock()

    ctx = FieldEventContext(
        chess_board=chess_board,
        move_state=move_state,
        correction_mode=correction_mode,
        player_manager=_PlayerManagerStub(pending_move),
        board_module=board_module,
        led=Mock(),
        event_callback=None,
        enter_correction_mode_fn=enter_correction_mode_fn,
        provide_correction_guidance_fn=provide_correction_guidance_fn,
        handle_field_event_in_correction_mode_fn=Mock(),
        handle_piece_event_without_player_fn=Mock(),
        on_piece_event_fn=on_piece_event_fn,
        on_player_move_fn=Mock(return_value=False),
        handle_king_lift_resign_fn=Mock(),
        execute_pending_move_fn=Mock(),
        check_takeback_fn=Mock(return_value=False),
        get_kings_in_center_menu_active_fn=lambda: False,
        set_kings_in_center_menu_active_fn=lambda _v: None,
        on_kings_in_center_cancel_fn=None,
        get_king_lift_resign_menu_active_fn=lambda: False,
        set_king_lift_resign_menu_active_fn=lambda _v: None,
        on_king_lift_resign_cancel_fn=None,
        chess_board_to_state_fn=lambda _b: None,
    )

    # Lift the capture square first (d4) - must NOT enter correction mode.
    process_field_event(ctx, piece_event=0, field=chess.D4, time_in_seconds=0.0)

    assert move_state.has_seen_capture_square_event(chess.D4)
    enter_correction_mode_fn.assert_not_called()


def test_lifting_capturable_opponent_piece_does_not_trigger_correction_mode() -> None:
    # Expected failure message if broken: "enter_correction_mode_fn called when lifting capturable opponent piece"
    # Why: In normal play (no pending move), players lift the captured piece first. This must be allowed when capture is legal.
    chess_board = chess.Board("rnbqkbnr/pppp1ppp/8/8/3pP3/8/PPP2PPP/RNBQKBNR w KQkq - 0 3")

    move_state = MoveState()
    correction_mode = CorrectionMode()

    board_module = Mock()
    board_module.getChessState.return_value = None
    board_module.beep = Mock()
    board_module.SOUND_WRONG_MOVE = 0

    enter_correction_mode_fn = Mock()

    ctx = FieldEventContext(
        chess_board=chess_board,
        move_state=move_state,
        correction_mode=correction_mode,
        player_manager=_PlayerManagerStub(None),
        board_module=board_module,
        led=Mock(),
        event_callback=None,
        enter_correction_mode_fn=enter_correction_mode_fn,
        provide_correction_guidance_fn=Mock(),
        handle_field_event_in_correction_mode_fn=Mock(),
        handle_piece_event_without_player_fn=Mock(),
        on_piece_event_fn=Mock(),
        on_player_move_fn=Mock(return_value=False),
        handle_king_lift_resign_fn=Mock(),
        execute_pending_move_fn=Mock(),
        check_takeback_fn=Mock(return_value=False),
        get_kings_in_center_menu_active_fn=lambda: False,
        set_kings_in_center_menu_active_fn=lambda _v: None,
        on_kings_in_center_cancel_fn=None,
        get_king_lift_resign_menu_active_fn=lambda: False,
        set_king_lift_resign_menu_active_fn=lambda _v: None,
        on_king_lift_resign_cancel_fn=None,
        chess_board_to_state_fn=lambda _b: None,
    )

    # d4 contains a black pawn. White can legally capture it (e.g., Qxd4).
    process_field_event(ctx, piece_event=0, field=chess.D4, time_in_seconds=0.0)

    enter_correction_mode_fn.assert_not_called()


def test_lifting_non_capturable_opponent_piece_enters_correction_mode() -> None:
    # Expected failure message if broken: "enter_correction_mode_fn not called when lifting non-capturable opponent piece"
    # Why: If an opponent piece is lifted and no legal capture exists to that square, it is an illegal interaction and should trigger correction mode.
    chess_board = chess.Board()

    move_state = MoveState()
    correction_mode = CorrectionMode()

    board_module = Mock()
    board_module.getChessState.return_value = None
    board_module.beep = Mock()
    board_module.SOUND_WRONG_MOVE = 0

    enter_correction_mode_fn = Mock()

    ctx = FieldEventContext(
        chess_board=chess_board,
        move_state=move_state,
        correction_mode=correction_mode,
        player_manager=_PlayerManagerStub(None),
        board_module=board_module,
        led=Mock(),
        event_callback=None,
        enter_correction_mode_fn=enter_correction_mode_fn,
        provide_correction_guidance_fn=Mock(),
        handle_field_event_in_correction_mode_fn=Mock(),
        handle_piece_event_without_player_fn=Mock(),
        on_piece_event_fn=Mock(),
        on_player_move_fn=Mock(return_value=False),
        handle_king_lift_resign_fn=Mock(),
        execute_pending_move_fn=Mock(),
        check_takeback_fn=Mock(return_value=False),
        get_kings_in_center_menu_active_fn=lambda: False,
        set_kings_in_center_menu_active_fn=lambda _v: None,
        on_kings_in_center_cancel_fn=None,
        get_king_lift_resign_menu_active_fn=lambda: False,
        set_king_lift_resign_menu_active_fn=lambda _v: None,
        on_king_lift_resign_cancel_fn=None,
        chess_board_to_state_fn=lambda _b: None,
    )

    # Start position: white to move; a7 is a black pawn and is not capturable by any legal move.
    process_field_event(ctx, piece_event=0, field=chess.A7, time_in_seconds=0.0)

    enter_correction_mode_fn.assert_called_once()


def test_normal_move_lift_bump_then_place_valid_move_is_accepted() -> None:
    # Expected failure message if broken: "on_player_move_fn not called for legal move after bumps"
    # Why: After lifting a piece, bumping/replacing other pieces must not prevent accepting a valid final move.
    chess_board = chess.Board()  # start position, white to move

    move_state = MoveState()
    correction_mode = CorrectionMode()

    expected_after_board = chess_board.copy()
    expected_after_board.push(chess.Move.from_uci("e2e4"))
    expected_after_state = _piece_presence_state(expected_after_board)

    board_module = Mock()
    board_module.getChessState.side_effect = [
        _piece_presence_state(chess_board),  # after a7 place (still matches logical)
        expected_after_state,  # after e4 place (matches post-move)
    ]
    board_module.beep = Mock()
    board_module.SOUND_WRONG_MOVE = 0

    enter_correction_mode_fn = Mock()
    on_player_move_fn = Mock(return_value=True)

    ctx = FieldEventContext(
        chess_board=chess_board,
        move_state=move_state,
        correction_mode=correction_mode,
        player_manager=_PlayerManagerStub(None),
        board_module=board_module,
        led=Mock(),
        event_callback=None,
        enter_correction_mode_fn=enter_correction_mode_fn,
        provide_correction_guidance_fn=Mock(),
        handle_field_event_in_correction_mode_fn=Mock(),
        handle_piece_event_without_player_fn=Mock(),
        on_piece_event_fn=Mock(),
        on_player_move_fn=on_player_move_fn,
        handle_king_lift_resign_fn=Mock(),
        execute_pending_move_fn=Mock(),
        check_takeback_fn=Mock(return_value=False),
        get_kings_in_center_menu_active_fn=lambda: False,
        set_kings_in_center_menu_active_fn=lambda _v: None,
        on_kings_in_center_cancel_fn=None,
        get_king_lift_resign_menu_active_fn=lambda: False,
        set_king_lift_resign_menu_active_fn=lambda _v: None,
        on_king_lift_resign_cancel_fn=None,
        chess_board_to_state_fn=lambda b: _piece_presence_state(b),
    )

    # Lift source piece (e2 pawn), then bump another piece (a7 pawn), then place on e4.
    process_field_event(ctx, piece_event=0, field=chess.E2, time_in_seconds=0.0)
    process_field_event(ctx, piece_event=0, field=chess.A7, time_in_seconds=0.1)
    process_field_event(ctx, piece_event=1, field=chess.A7, time_in_seconds=0.2)
    process_field_event(ctx, piece_event=1, field=chess.E4, time_in_seconds=0.3)

    enter_correction_mode_fn.assert_not_called()
    on_player_move_fn.assert_called_once()
    assert on_player_move_fn.call_args[0][0].uci() == "e2e4"


def test_takeback_detected_on_place_when_board_matches_previous_position() -> None:
    # Expected failure message if broken: "check_takeback_fn not called or takeback not executed"
    # Why: When a PLACE event results in a board state matching the position before the last move,
    # the takeback should be detected and executed immediately, without forwarding to the player.
    
    # Start with a position after 1. e4
    chess_board = chess.Board()
    chess_board.push(chess.Move.from_uci("e2e4"))  # After 1. e4
    
    # Now the move_stack has one move, so takeback is possible
    assert len(chess_board.move_stack) == 1

    move_state = MoveState()
    correction_mode = CorrectionMode()

    board_module = Mock()
    board_module.beep = Mock()
    board_module.SOUND_WRONG_MOVE = 0

    # check_takeback_fn returns True to indicate takeback was executed
    check_takeback_fn = Mock(return_value=True)
    on_piece_event_fn = Mock()

    ctx = FieldEventContext(
        chess_board=chess_board,
        move_state=move_state,
        correction_mode=correction_mode,
        player_manager=_PlayerManagerStub(None),
        board_module=board_module,
        led=Mock(),
        event_callback=None,
        enter_correction_mode_fn=Mock(),
        provide_correction_guidance_fn=Mock(),
        handle_field_event_in_correction_mode_fn=Mock(),
        handle_piece_event_without_player_fn=Mock(),
        on_piece_event_fn=on_piece_event_fn,
        on_player_move_fn=Mock(return_value=False),
        handle_king_lift_resign_fn=Mock(),
        execute_pending_move_fn=Mock(),
        check_takeback_fn=check_takeback_fn,
        get_kings_in_center_menu_active_fn=lambda: False,
        set_kings_in_center_menu_active_fn=lambda _v: None,
        on_kings_in_center_cancel_fn=None,
        get_king_lift_resign_menu_active_fn=lambda: False,
        set_king_lift_resign_menu_active_fn=lambda _v: None,
        on_king_lift_resign_cancel_fn=None,
        chess_board_to_state_fn=lambda b: _piece_presence_state(b),
    )

    # PLACE event on e2 (pawn returned to original square)
    process_field_event(ctx, piece_event=1, field=chess.E2, time_in_seconds=0.0)

    # check_takeback_fn must be called and the event handling must return early
    check_takeback_fn.assert_called_once()
    # on_piece_event_fn must NOT be called because the takeback was handled
    on_piece_event_fn.assert_not_called()


def test_takeback_not_checked_on_lift_events() -> None:
    # Expected failure message if broken: "check_takeback_fn was called on LIFT event"
    # Why: Takeback detection should only happen after PLACE events, not LIFT events.
    
    chess_board = chess.Board()
    chess_board.push(chess.Move.from_uci("e2e4"))  # After 1. e4

    move_state = MoveState()
    correction_mode = CorrectionMode()

    board_module = Mock()
    board_module.beep = Mock()
    board_module.SOUND_WRONG_MOVE = 0

    check_takeback_fn = Mock(return_value=False)

    ctx = FieldEventContext(
        chess_board=chess_board,
        move_state=move_state,
        correction_mode=correction_mode,
        player_manager=_PlayerManagerStub(None),
        board_module=board_module,
        led=Mock(),
        event_callback=None,
        enter_correction_mode_fn=Mock(),
        provide_correction_guidance_fn=Mock(),
        handle_field_event_in_correction_mode_fn=Mock(),
        handle_piece_event_without_player_fn=Mock(),
        on_piece_event_fn=Mock(),
        on_player_move_fn=Mock(return_value=False),
        handle_king_lift_resign_fn=Mock(),
        execute_pending_move_fn=Mock(),
        check_takeback_fn=check_takeback_fn,
        get_kings_in_center_menu_active_fn=lambda: False,
        set_kings_in_center_menu_active_fn=lambda _v: None,
        on_kings_in_center_cancel_fn=None,
        get_king_lift_resign_menu_active_fn=lambda: False,
        set_king_lift_resign_menu_active_fn=lambda _v: None,
        on_king_lift_resign_cancel_fn=None,
        chess_board_to_state_fn=lambda b: _piece_presence_state(b),
    )

    # LIFT event (should NOT trigger takeback check)
    process_field_event(ctx, piece_event=0, field=chess.E4, time_in_seconds=0.0)

    check_takeback_fn.assert_not_called()


def test_takeback_not_checked_when_no_moves_in_stack() -> None:
    # Expected failure message if broken: "check_takeback_fn was called with empty move stack"
    # Why: Takeback detection should be skipped when there are no moves to take back.
    
    chess_board = chess.Board()  # Starting position, no moves made
    assert len(chess_board.move_stack) == 0

    move_state = MoveState()
    correction_mode = CorrectionMode()

    board_module = Mock()
    board_module.beep = Mock()
    board_module.SOUND_WRONG_MOVE = 0

    check_takeback_fn = Mock(return_value=False)

    ctx = FieldEventContext(
        chess_board=chess_board,
        move_state=move_state,
        correction_mode=correction_mode,
        player_manager=_PlayerManagerStub(None),
        board_module=board_module,
        led=Mock(),
        event_callback=None,
        enter_correction_mode_fn=Mock(),
        provide_correction_guidance_fn=Mock(),
        handle_field_event_in_correction_mode_fn=Mock(),
        handle_piece_event_without_player_fn=Mock(),
        on_piece_event_fn=Mock(),
        on_player_move_fn=Mock(return_value=False),
        handle_king_lift_resign_fn=Mock(),
        execute_pending_move_fn=Mock(),
        check_takeback_fn=check_takeback_fn,
        get_kings_in_center_menu_active_fn=lambda: False,
        set_kings_in_center_menu_active_fn=lambda _v: None,
        on_kings_in_center_cancel_fn=None,
        get_king_lift_resign_menu_active_fn=lambda: False,
        set_king_lift_resign_menu_active_fn=lambda _v: None,
        on_king_lift_resign_cancel_fn=None,
        chess_board_to_state_fn=lambda b: _piece_presence_state(b),
    )

    # PLACE event on e4 - should NOT call check_takeback_fn since move_stack is empty
    process_field_event(ctx, piece_event=1, field=chess.E4, time_in_seconds=0.0)

    check_takeback_fn.assert_not_called()


