"""Starting position detection helpers.

Multiple GameManager flows treat the physical board being in the standard starting
position as a signal to abandon/reset the current game.

A remote game can already be at that start (a Lichess seek that connected before
the pieces were set). Completing the starting setup then is the board catching
up, not a new game -- abandoning it cancelled the remote game and started a
local one. That catch-up path is opt-in via ``allow_matching_continue`` (wired
from ``requires_rebuild_on_new_game``). A local game that is already at start
(takeback to ply 0, or never moved) must still treat home-rank as a new game.

When the live game has already ended (flag, resign, agreement) while occupancy
is still start -- for example takeback to ply 0 then a clock flag -- matching
start is never catch-up: home-rank setup must run the new-game reset so
``is_game_over`` clears. ``abandon_current_game`` only writes ``*`` when the
database row has no result yet, so a finished forfeit/resign stays recorded.
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
    game_is_over: bool = False,
    allow_matching_continue: bool = False,
) -> Optional[str]:
    """How a physical starting setup relates to the live game.

    Returns ``continue`` only when the live occupancy is already start, the game
    is still in progress, and ``allow_matching_continue`` is True (remote catch-up).
    Returns ``abandon`` when start disagrees with the live position, the live game
    has ended, or matching start is a local new-game gesture. Returns ``None``
    when the physical board is not at start.
    """
    if not is_starting_position_state(current_state=current_state, board_size=board_size):
        return None
    if game_is_over:
        return "abandon"
    if (
        allow_matching_continue
        and expected_logical_state is not None
        and ChessGameState.states_match(current_state, expected_logical_state)
    ):
        return "continue"
    return "abandon"


def reset_game_if_starting_position(
    *,
    current_state: Optional[Sequence[int]],
    board_size: int,
    reset_game_fn: Callable[[], None],
    expected_logical_state: Optional[Sequence[int]] = None,
    game_is_over: bool = False,
    allow_matching_continue: bool = False,
) -> bool:
    """Reset the game if the physical board is in the starting position.

    Does not reset when that start already matches a still-in-progress remote
    game (``allow_matching_continue``). Resets for local matching start and when
    the live game has ended.

    Returns:
        True if reset was triggered, False otherwise.
    """
    if (
        interpret_physical_start(
            current_state=current_state,
            board_size=board_size,
            expected_logical_state=expected_logical_state,
            game_is_over=game_is_over,
            allow_matching_continue=allow_matching_continue,
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
