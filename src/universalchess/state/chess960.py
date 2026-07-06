"""Chess960 (Fischer Random) start-position helpers.

Pure functions with no UI or hardware dependencies so they can be reused and
tested in isolation. python-chess enumerates the 960 legal starting arrays via
the Scharnagl numbering (0-959); ``chess.Board.from_chess960_pos`` builds a board
for a given number with ``chess960`` already set to True.
"""

from __future__ import annotations

import random
from typing import Optional, Tuple

import chess

CHESS960_POSITION_COUNT = 960


def chess960_fen(position_number: int) -> str:
    """Return the starting FEN for a Chess960 position number.

    Args:
        position_number: Scharnagl position number in ``range(960)``.

    Returns:
        The starting FEN (with Chess960 castling rights) for that position.

    Raises:
        ValueError: If ``position_number`` is outside ``0..959``.
    """
    if not 0 <= position_number < CHESS960_POSITION_COUNT:
        raise ValueError(
            f"Chess960 position number must be in 0..{CHESS960_POSITION_COUNT - 1}, "
            f"got {position_number}"
        )
    board = chess.Board.from_chess960_pos(position_number)
    return board.fen()


def random_chess960_fen(rng: Optional[random.Random] = None) -> Tuple[str, int]:
    """Pick a random Chess960 starting position.

    Args:
        rng: Optional random source (injected for deterministic tests). Uses the
            module-global generator when omitted.

    Returns:
        Tuple of ``(fen, position_number)`` for the chosen position.
    """
    source = rng if rng is not None else random
    position_number = source.randint(0, CHESS960_POSITION_COUNT - 1)
    return chess960_fen(position_number), position_number


__all__ = ["CHESS960_POSITION_COUNT", "chess960_fen", "random_chess960_fen"]
