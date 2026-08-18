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
) -> None:
    """Handle field events while correction mode is active.

    Behavior preserved from prior inline implementation:
    - On PLACE events, delays briefly to let sensors settle (sliding pieces).
    - Reads current physical state.
    - If starting position is detected and the live game has left start,
      abandons the current game and resets. Physical start that already matches
      the live game (a Lichess seek that connected before the pieces were set)
      exits correction instead.
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

    # Physical start that does not match the live game is a new-game gesture.
    # Matching start is handled above so a Lichess ply-0 setup is not abandoned.
    if (
        interpret_physical_start(
            current_state=current_physical_state,
            board_size=board_size,
            expected_logical_state=expected_state,
        )
        == "abandon"
    ):
        log.warning(
            "[GameManager._handle_field_event_in_correction_mode] Starting position detected during "
            "correction mode - abandoning current game and starting new game"
        )
        reset_game_fn()
        return

    # Still incorrect → update guidance LEDs based on latest logical state.
    if current_physical_state is not None:
        current_expected_state = chess_board_to_state_fn(chess_board)
        if current_expected_state is not None:
            provide_correction_guidance_fn(current_physical_state, current_expected_state)


__all__ = ["handle_field_event_in_correction_mode"]


