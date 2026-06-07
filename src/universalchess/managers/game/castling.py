"""Castling helpers for GameManager.

This module encapsulates the complex castling flows that were previously embedded in
`GameManager`:
- rook-first castling execution (`_execute_castling_move`)
- late castling correction when rook moved as a regular move (`_execute_late_castling`)
- "late castling from submitted king move" detection (`_detect_late_castling`)

The implementation keeps behavior identical by using dependency injection for
GameManager-owned state and callbacks.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

import chess

from universalchess.board.logging import log
from universalchess.utils.led import LedCallbacks

from .deferred_imports import _get_models
from .move_persistence import persist_move_and_maybe_create_game


STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def detect_late_castling(
    *,
    king_move: chess.Move,
    chess_board: chess.Board,
    pop_move_fn: Callable[[], Optional[chess.Move]],
    push_move_fn: Callable[[chess.Move], None],
) -> Optional[chess.Move]:
    """Detect if a king move is part of a late castling sequence.

    Late castling occurs when the player moves the rook first (e.g., h1f1),
    and then moves the king to the castling destination (e.g., e1g1).
    After the rook move, castling is no longer legal because the rook moved,
    but we recognize this pattern and allow it.

    Behavior mirrors the previous inline implementation:
    - checks last move for rook move pattern
    - temporarily undoes the rook move via pop_move_fn
    - if castling would have been legal, returns castling move
    - otherwise restores rook move via push_move_fn
    """
    if len(chess_board.move_stack) == 0:
        return None

    patterns = [
        (chess.E1, chess.G1, chess.H1, chess.F1, "e1g1"),  # White kingside
        (chess.E1, chess.C1, chess.A1, chess.D1, "e1c1"),  # White queenside
        (chess.E8, chess.G8, chess.H8, chess.F8, "e8g8"),  # Black kingside
        (chess.E8, chess.C8, chess.A8, chess.D8, "e8c8"),  # Black queenside
    ]

    for king_from, king_to, rook_from, rook_to, castling_uci in patterns:
        if king_move.from_square == king_from and king_move.to_square == king_to:
            last_move = chess_board.peek()
            if last_move.from_square == rook_from and last_move.to_square == rook_to:
                undone = pop_move_fn()  # Notifies observers in GameManager state wrapper
                if undone is None:
                    return None

                castling_move = chess.Move.from_uci(castling_uci)
                if castling_move in chess_board.legal_moves:
                    return castling_move

                push_move_fn(last_move)
                return None

    return None


def execute_rook_first_castling(
    *,
    rook_source: int,
    move_state,
    chess_board: chess.Board,
    push_move_fn: Callable[[chess.Move], None],
    board_module,
    led: LedCallbacks,
    enter_correction_mode_fn: Callable[[], None],
    chess_board_to_state_fn: Callable[[chess.Board], Optional[bytearray]],
    provide_correction_guidance_fn: Callable[[Optional[bytearray], Optional[bytearray]], None],
    database_session,
    game_db_id: int,
    source_file: str,
    game_info: Dict[str, str],
    get_clock_times_for_db_fn: Callable[[], tuple],
    get_eval_score_for_db_fn: Callable[[], Optional[int]],
    move_callback_fn: Optional[Callable[[str], None]],
    switch_turn_with_event_fn: Callable[[], None],
    update_game_result_fn: Callable[[str, str, str], None],
) -> int:
    """Execute a castling move when the rook was moved first.

    Returns:
        Updated game_db_id (may change if castling is the first DB move and game is created).
    """
    outcome = chess_board.outcome(claim_draw=True)
    if outcome is not None:
        log.warning(
            "[GameManager._execute_castling_move] Attempted to execute castling after game ended. "
            f"Result: {chess_board.result()}"
        )
        board_module.beep(board_module.SOUND_WRONG_MOVE, event_type="error")
        led.off()
        move_state.reset()
        return game_db_id

    castling_uci = move_state.get_castling_king_move(rook_source)
    if not castling_uci:
        log.error(f"[GameManager._execute_castling_move] Invalid rook source for castling: {rook_source}")
        board_module.beep(board_module.SOUND_WRONG_MOVE, event_type="error")
        move_state.reset()
        return game_db_id

    log.info(f"[GameManager._execute_castling_move] Executing rook-first castling: {castling_uci}")

    try:
        move = chess.Move.from_uci(castling_uci)
        if move not in chess_board.legal_moves:
            log.error(
                f"[GameManager._execute_castling_move] Castling move {castling_uci} is not legal at current position"
            )
            board_module.beep(board_module.SOUND_WRONG_MOVE, event_type="error")
            enter_correction_mode_fn()
            current_state = board_module.getChessState()
            expected_state = chess_board_to_state_fn(chess_board)
            if expected_state is not None:
                provide_correction_guidance_fn(current_state, expected_state)
            move_state.reset()
            return game_db_id
    except ValueError as e:
        log.error(f"[GameManager._execute_castling_move] Invalid castling UCI: {castling_uci}. Error: {e}")
        board_module.beep(board_module.SOUND_WRONG_MOVE, event_type="error")
        move_state.reset()
        return game_db_id

    king_dest = chess.parse_square(castling_uci[2:4])

    try:
        push_move_fn(move)
    except (ValueError, AssertionError) as e:
        log.error(f"[GameManager._execute_castling_move] Failed to push castling move: {e}")
        board_module.beep(board_module.SOUND_WRONG_MOVE, event_type="error")
        move_state.reset()
        return game_db_id

    # Save to database (first-move case creates game+initial move row)
    if database_session is not None:
        try:
            white_clock, black_clock = get_clock_times_for_db_fn()
            eval_score = get_eval_score_for_db_fn()
            is_first_move = game_db_id < 0
            new_game_db_id, _ = persist_move_and_maybe_create_game(
                session=database_session,
                is_first_move=is_first_move,
                current_game_db_id=game_db_id,
                source_file=source_file,
                game_info=game_info,
                fen_before_move=STARTING_FEN,
                move_uci=castling_uci,
                fen_after_move=str(chess_board.fen()),
                white_clock=white_clock,
                black_clock=black_clock,
                eval_score=eval_score,
            )
            game_db_id = new_game_db_id
        except Exception as db_error:
            log.error(f"[GameManager._execute_castling_move] Database error: {db_error}")
            try:
                database_session.rollback()
            except Exception:
                pass

    if move_callback_fn is not None:
        try:
            move_callback_fn(castling_uci)
        except Exception as e:
            log.error(f"[GameManager._execute_castling_move] Error in move callback: {e}")

    move_state.reset()
    led.off()
    # King place confirmation (with the place-confirmation flash) -> piece_event,
    # consistent with execute_late_castling.
    board_module.beep(board_module.SOUND_GENERAL, event_type="piece_event")
    led.single_fast(king_dest, repeat=1)

    outcome = chess_board.outcome(claim_draw=True)
    if outcome is None:
        switch_turn_with_event_fn()
    else:
        board_module.beep(board_module.SOUND_GENERAL, event_type="game_event")
        result_string = str(chess_board.result())
        termination = str(outcome.termination)
        update_game_result_fn(result_string, termination, "_execute_castling_move")

    return game_db_id


def _delete_last_move_for_game(database_session, game_db_id: int) -> None:
    if database_session is None or game_db_id < 0:
        return
    models = _get_models()
    if models is None:
        return
    db_last_move = (
        database_session.query(models.GameMove)
        .filter(models.GameMove.gameid == game_db_id)
        .order_by(models.GameMove.id.desc())
        .first()
    )
    if db_last_move is None:
        return
    database_session.delete(db_last_move)
    database_session.commit()


def execute_late_castling(
    *,
    rook_source: int,
    move_state,
    chess_board: chess.Board,
    pop_move_fn: Callable[[], Optional[chess.Move]],
    push_move_fn: Callable[[chess.Move], None],
    board_module,
    led: LedCallbacks,
    database_session,
    game_db_id: int,
    get_clock_times_for_db_fn: Callable[[], tuple],
    get_eval_score_for_db_fn: Callable[[], Optional[int]],
    move_callback_fn: Optional[Callable[[str], None]],
    takeback_callback_fn: Optional[Callable[[], None]],
    switch_turn_with_event_fn: Callable[[], None],
    enter_correction_mode_fn: Callable[[], None],
    chess_board_to_state_fn: Callable[[chess.Board], Optional[bytearray]],
    provide_correction_guidance_fn: Callable[[Optional[bytearray], Optional[bytearray]], None],
    update_game_result_fn: Callable[[str, str, str], None],
) -> int:
    """Execute castling when rook move was already made as a regular move.

    Returns updated game_db_id.
    """
    log.info(
        f"[GameManager._execute_late_castling] Processing late castling for rook from {chess.square_name(rook_source)}"
    )

    castling_uci = move_state.get_castling_king_move(rook_source)
    if not castling_uci:
        log.error(f"[GameManager._execute_late_castling] Invalid rook source: {rook_source}")
        board_module.beep(board_module.SOUND_WRONG_MOVE, event_type="error")
        move_state.reset()
        return game_db_id

    if len(chess_board.move_stack) < 1:
        log.error("[GameManager._execute_late_castling] No moves in stack to undo")
        board_module.beep(board_module.SOUND_WRONG_MOVE, event_type="error")
        move_state.reset()
        return game_db_id

    rook_move_uci = None
    from .move_state import MoveState  # local import to avoid cycles

    if rook_source == MoveState.WHITE_KINGSIDE_ROOK:
        rook_move_uci = "h1f1"
    elif rook_source == MoveState.WHITE_QUEENSIDE_ROOK:
        rook_move_uci = "a1d1"
    elif rook_source == MoveState.BLACK_KINGSIDE_ROOK:
        rook_move_uci = "h8f8"
    elif rook_source == MoveState.BLACK_QUEENSIDE_ROOK:
        rook_move_uci = "a8d8"

    moves_to_undo = 0
    undone_moves = []

    for i in range(min(2, len(chess_board.move_stack))):
        check_move = chess_board.move_stack[-(i + 1)]
        if check_move.uci() == rook_move_uci:
            moves_to_undo = i + 1
            break

    if moves_to_undo == 0:
        log.error(f"[GameManager._execute_late_castling] Rook move {rook_move_uci} not found in recent moves")
        board_module.beep(board_module.SOUND_WRONG_MOVE, event_type="error")
        move_state.reset()
        return game_db_id

    log.info(f"[GameManager._execute_late_castling] Undoing {moves_to_undo} move(s) to correct castling")

    for _ in range(moves_to_undo):
        undone_move = pop_move_fn()
        if undone_move:
            undone_moves.append(undone_move)
            log.info(f"[GameManager._execute_late_castling] Undone move: {undone_move.uci()}")

        if database_session is not None:
            try:
                _delete_last_move_for_game(database_session, game_db_id)
            except Exception as e:
                log.error(f"[GameManager._execute_late_castling] Error removing move from database: {e}")

    try:
        castling_move = chess.Move.from_uci(castling_uci)
        if castling_move not in chess_board.legal_moves:
            log.error(f"[GameManager._execute_late_castling] Castling {castling_uci} not legal after undo")
            for mv in reversed(undone_moves):
                push_move_fn(mv)
            board_module.beep(board_module.SOUND_WRONG_MOVE, event_type="error")
            enter_correction_mode_fn()
            current_state = board_module.getChessState()
            expected_state = chess_board_to_state_fn(chess_board)
            if expected_state is not None:
                provide_correction_guidance_fn(current_state, expected_state)
            move_state.reset()
            return game_db_id
    except ValueError as e:
        log.error(f"[GameManager._execute_late_castling] Invalid castling UCI: {e}")
        board_module.beep(board_module.SOUND_WRONG_MOVE, event_type="error")
        move_state.reset()
        return game_db_id

    try:
        push_move_fn(castling_move)
    except (ValueError, AssertionError) as e:
        log.error(f"[GameManager._execute_late_castling] Failed to push castling: {e}")
        board_module.beep(board_module.SOUND_WRONG_MOVE, event_type="error")
        move_state.reset()
        return game_db_id

    log.info(f"[GameManager._execute_late_castling] Castling {castling_uci} executed successfully")

    if database_session is not None and game_db_id >= 0:
        try:
            white_clock, black_clock = get_clock_times_for_db_fn()
            eval_score = get_eval_score_for_db_fn()
            new_game_db_id, _ = persist_move_and_maybe_create_game(
                session=database_session,
                is_first_move=False,
                current_game_db_id=game_db_id,
                source_file="",  # unused when is_first_move=False
                game_info={},
                fen_before_move="",
                move_uci=castling_uci,
                fen_after_move=str(chess_board.fen()),
                white_clock=white_clock,
                black_clock=black_clock,
                eval_score=eval_score,
            )
            game_db_id = new_game_db_id
        except Exception as db_error:
            log.error(f"[GameManager._execute_late_castling] Database error: {db_error}")

    if move_callback_fn is not None:
        try:
            move_callback_fn(castling_uci)
        except Exception as e:
            log.error(f"[GameManager._execute_late_castling] Error in move callback: {e}")

    move_state.reset()

    king_dest = chess.parse_square(castling_uci[2:4])
    led.off()
    # King place confirmation (with the place-confirmation flash) -> piece_event.
    board_module.beep(board_module.SOUND_GENERAL, event_type="piece_event")
    led.single_fast(king_dest, repeat=1)

    if moves_to_undo > 1 and takeback_callback_fn is not None:
        log.info("[GameManager._execute_late_castling] Calling takeback callback to re-trigger engine")
        try:
            takeback_callback_fn()
        except Exception as e:
            log.error(f"[GameManager._execute_late_castling] Error in takeback callback: {e}")

    outcome = chess_board.outcome(claim_draw=True)
    if outcome is None:
        switch_turn_with_event_fn()
    else:
        board_module.beep(board_module.SOUND_GENERAL, event_type="game_event")
        result_string = str(chess_board.result())
        termination = str(outcome.termination)
        update_game_result_fn(result_string, termination, "_execute_late_castling")

    return game_db_id


__all__ = [
    "detect_late_castling",
    "execute_rook_first_castling",
    "execute_late_castling",
]


