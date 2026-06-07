"""Move execution helper for GameManager.

This module extracts the critical-path move execution logic from `GameManager._execute_move`.
The implementation is kept behavior-identical and uses dependency injection to avoid
hard coupling to GameManager internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import chess

from universalchess.board.logging import log
from universalchess.utils.led import LedCallbacks

from .move_state import BOARD_WIDTH, INVALID_SQUARE, MIN_UCI_MOVE_LENGTH, PROMOTION_ROW_BLACK, PROMOTION_ROW_WHITE


@dataclass(frozen=True)
class MoveExecutionContext:
    chess_board: chess.Board
    game_state: object
    move_state: object
    board_module: object
    led: LedCallbacks  # LED control callbacks

    # GameManager callbacks / helpers
    handle_promotion_fn: Callable[[int, str, bool], str]
    switch_turn_with_event_fn: Callable[[], None]
    enqueue_post_move_tasks_fn: Callable[..., None]

    # Additional state
    get_game_db_id_fn: Callable[[], int]


def execute_move(ctx: MoveExecutionContext, target_square: int) -> None:
    """Execute a move from source to target square (critical path)."""
    outcome = ctx.chess_board.outcome(claim_draw=True)
    if outcome is not None:
        log.warning(
            "[GameManager._execute_move] Attempted to execute move after game ended. "
            f"Result: {ctx.chess_board.result()}, Termination: {outcome.termination}"
        )
        ctx.board_module.beep(ctx.board_module.SOUND_WRONG_MOVE, event_type="error")
        ctx.led.off()
        ctx.move_state.reset()
        return

    from_name = chess.square_name(ctx.move_state.source_square)
    to_name = chess.square_name(target_square)
    piece_name = str(ctx.chess_board.piece_at(ctx.move_state.source_square))

    if ctx.move_state.is_forced_move:
        move_uci = ctx.move_state.computer_move_uci

        is_white_promotion = (target_square // BOARD_WIDTH) == PROMOTION_ROW_WHITE and piece_name == "P"
        is_black_promotion = (target_square // BOARD_WIDTH) == PROMOTION_ROW_BLACK and piece_name == "p"
        if is_white_promotion or is_black_promotion:
            if len(move_uci) < 5:
                log.warning(
                    f"[GameManager._execute_move] Forced move UCI '{move_uci}' missing promotion piece "
                    "for promotion move, defaulting to queen"
                )
                move_uci = move_uci + "q"
    else:
        promotion_suffix = ctx.handle_promotion_fn(target_square, piece_name, ctx.move_state.is_forced_move)
        move_uci = from_name + to_name + promotion_suffix

    try:
        move = chess.Move.from_uci(move_uci)
    except ValueError as e:
        log.error(f"[GameManager._execute_move] Invalid move UCI format: {move_uci}. Error: {e}")
        ctx.board_module.beep(ctx.board_module.SOUND_WRONG_MOVE, event_type="error")
        ctx.led.off()
        ctx.move_state.reset()
        return

    fen_before_move = str(ctx.chess_board.fen())
    is_first_move = ctx.get_game_db_id_fn() < 0

    try:
        ctx.game_state.push_move(move)
    except (ValueError, AssertionError) as e:
        log.error(f"[GameManager._execute_move] Illegal move or chess engine push failed: {move_uci}. Error: {e}")
        ctx.board_module.beep(ctx.board_module.SOUND_WRONG_MOVE, event_type="error")
        ctx.led.off()
        ctx.move_state.reset()
        return

    ctx.led.off()
    # Move/place confirmation beep (fires with the place-confirmation flash) ->
    # piece_event, consistent with execute_complete_move.
    ctx.board_module.beep(ctx.board_module.SOUND_GENERAL, event_type="piece_event")
    ctx.led.single_fast(target_square, repeat=1)

    fen_after_move = str(ctx.chess_board.fen())
    late_castling_in_progress = ctx.move_state.late_castling_in_progress

    game_ended = False
    result_string = None
    termination = None
    outcome = ctx.chess_board.outcome(claim_draw=True)
    if outcome is not None:
        game_ended = True
        result_string = str(ctx.chess_board.result())
        termination = str(outcome.termination)

    preserve_castling_rook_source = ctx.move_state.castling_rook_source
    preserve_castling_rook_placed = ctx.move_state.castling_rook_placed

    ctx.move_state.reset()

    if preserve_castling_rook_placed:
        ctx.move_state.castling_rook_source = preserve_castling_rook_source
        ctx.move_state.castling_rook_placed = preserve_castling_rook_placed

    if not game_ended:
        ctx.switch_turn_with_event_fn()

    ctx.enqueue_post_move_tasks_fn(
        target_square=target_square,
        move_uci=move_uci,
        fen_before_move=fen_before_move,
        fen_after_move=fen_after_move,
        is_first_move=is_first_move,
        late_castling_in_progress=late_castling_in_progress,
        game_ended=game_ended,
        result_string=result_string,
        termination=termination,
    )


__all__ = ["MoveExecutionContext", "execute_move"]


