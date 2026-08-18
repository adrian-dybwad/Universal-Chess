"""Starting position detection helpers.

Multiple GameManager flows treat the physical board being in the standard starting
position as a signal to abandon/reset the current game.

A remote game can already be at that start (a Lichess seek that connected before
the pieces were set). Completing the starting setup then is the board catching
up, not a new game -- abandoning it cancelled the remote game and started a
local one.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from universalchess.state.chess_game import ChessGameState


def is_starting_position_state(
    *,
    current_state: Optional[Sequence[int]],
    board_size: int,
) -> bool:
    """Return True if the physical board state represents the standard starting position."""
    if current_state is None or len(current_state) != board_size:
        return False
    return ChessGameState.is_starting_position(current_state)


def interpret_physical_start(
    *,
    current_state: Optional[Sequence[int]],
    board_size: int,
    expected_logical_state: Optional[Sequence[int]],
) -> Optional[str]:
    """How a physical starting setup relates to the live game.

    Returns ``continue`` when the live occupancy is already start (board catching
    up), ``abandon`` when start disagrees with the live position (new-game
    gesture), and ``None`` when the physical board is not at start.
    """
    if not is_starting_position_state(current_state=current_state, board_size=board_size):
        return None
    if expected_logical_state is not None and ChessGameState.states_match(
        current_state, expected_logical_state
    ):
        return "continue"
    return "abandon"


def reset_game_if_starting_position(
    *,
    current_state: Optional[Sequence[int]],
    board_size: int,
    reset_game_fn: Callable[[], None],
    expected_logical_state: Optional[Sequence[int]] = None,
) -> bool:
    """Reset the game if the physical board is in the starting position.

    Does not reset when that start already matches the live game.

    Returns:
        True if reset was triggered, False otherwise.
    """
    if (
        interpret_physical_start(
            current_state=current_state,
            board_size=board_size,
            expected_logical_state=expected_logical_state,
        )
        != "abandon"
    ):
        return False

    reset_game_fn()
    return True


__all__ = [
    "is_starting_position_state",
    "interpret_physical_start",
    "reset_game_if_starting_position",
]
