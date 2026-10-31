"""Correction-mode flow helpers.

This module contains the correction-mode event processing rules. It is extracted from
`GameManager._handle_field_event_in_correction_mode` to reduce `GameManager` size and
make the correction subsystem more testable.
"""

from __future__ import annotations

import time
from typing import Callable, Optional, Sequence

from universalchess.board.logging import log
from universalchess.state.chess_game import ChessGameState
from .starting_position import interpret_physical_start

def handle_field_event_in_correction_mode(
    *,
    piece_event: int,
    board_module,
    board_size: int,
    expected_logical_state: Optional[Sequence[int]],
    chess_board,
    chess_board_to_state_fn: Callable,
    reset_game_fn: Callable[[], None],
    exit_correction_mode_fn: Callable[[], None],
    provide_correction_guidance_fn: Callable[[Sequence[int], Sequence[int]], None],
    game_is_over: bool = False,
    allow_matching_continue: bool = False,
) -> None:
    """Handle field events while correction mode is active.

    Behavior preserved from prior inline implementation:
    - On PLACE events, delays briefly to let sensors settle (sliding pieces).
    - Reads current physical state.
    - If starting position is detected and the live game has left start, the
      live game has already ended while still at start, or matching start is a
      local new-game gesture, abandons/resets. Physical start that already
      matches an in-progress remote game (``allow_matching_continue``, a Lichess
      seek that connected before the pieces were set) exits correction instead.
    - Recomputes expected logical state from the authoritative chess board.
    - If physical matches expected, exits correction mode.
    - Otherwise updates guidance LEDs.

    Args:
        piece_event: 0=LIFT, 1=PLACE
        board_module: DGTCentaurMods.board.board module (or compatible stub)
        board_size: Expected board size (64)
        expected_logical_state: Unused (kept for signature parity / future expansion)
        chess_board: Current authoritative chess.Board
        chess_board_to_state_fn: Function to compute expected state from chess board
        reset_game_fn: Callback to reset/abandon current game
        exit_correction_mode_fn: Callback to exit correction mode
        provide_correction_guidance_fn: Callback to update LEDs for correction guidance
        game_is_over: When True, matching start is a new-game reset (clear finished
            result), not board catch-up
        allow_matching_continue: When True (remote ply-0 catch-up), matching start
            exits correction instead of resetting
    """
    # Small delay to allow sensors to settle after piece placement (sliding pieces).
    is_place = piece_event == 1
    if is_place:
        time.sleep(0.05)

    current_physical_state = board_module.getChessState()

    expected_state = chess_board_to_state_fn(chess_board)
    if expected_state is None:
        log.error(
            "[GameManager._handle_field_event_in_correction_mode] Cannot validate: failed to get logical board state"
        )
        return

    # Start gesture before the match exit so a finished game still at home
    # ranks is reset instead of treated as a successful correction.
    start_action = interpret_physical_start(
        current_state=current_physical_state,
        board_size=board_size,
        expected_logical_state=expected_state,
        game_is_over=game_is_over,
        allow_matching_continue=allow_matching_continue,
    )
    if start_action == "abandon":
        log.warning(
            "[GameManager._handle_field_event_in_correction_mode] Starting position detected during "
            "correction mode - abandoning current game and starting new game"
        )
        reset_game_fn()
        return
    if start_action == "continue":
        log.info(
            "[GameManager._handle_field_event_in_correction_mode] Starting setup matches the live "
            "game, exiting correction mode"
        )
        board_module.beep(board_module.SOUND_GENERAL, event_type="game_event")
        exit_correction_mode_fn()
        return

    if (
        current_physical_state is not None
        and ChessGameState.states_match(current_physical_state, expected_state)
    ):
        log.info(
            "[GameManager._handle_field_event_in_correction_mode] Physical board now matches logical board, "
            "exiting correction mode"
        )
        board_module.beep(board_module.SOUND_GENERAL, event_type="game_event")
        exit_correction_mode_fn()
        return

    # Still incorrect → update guidance LEDs based on latest logical state.
    if current_physical_state is not None:
        current_expected_state = chess_board_to_state_fn(chess_board)
        if current_expected_state is not None:
            provide_correction_guidance_fn(current_physical_state, current_expected_state)


__all__ = ["handle_field_event_in_correction_mode"]


