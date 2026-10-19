# Move indication
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""Read the squares a chess app lit back into the legal moves they describe.

Millennium, Pegasus and Chessnut apps announce their own move by lighting squares:
the from/to pair or, for castling, the king's and the rook's squares. They never
say which piece a pawn promotes to. Pure functions only; callers decide what to do
with the result.
"""

from typing import Callable, Hashable, Iterable, List, Set

import chess

from universalchess.board.piece_events import castling_rook_squares


def move_squares(board: chess.Board, move: chess.Move) -> Set[int]:
    """Return all squares a single move lights.

    For a normal move that is {from, to}. For castling it additionally includes
    the rook's origin and destination, because a client may light all four.
    """
    squares = {move.from_square, move.to_square}
    if board.is_castling(move):
        squares.update(castling_rook_squares(board, move))
    return squares


def find_moves_lighting(
    lit_marks: Iterable[Hashable],
    game_fen: str,
    marks_for_square: Callable[[int], Iterable[Hashable]],
) -> List[chess.Move]:
    """Return the legal moves whose squares light exactly ``lit_marks``.

    A "mark" is whatever the app's protocol lights for a square: the square
    itself, or (Millennium) the four corner LEDs around it, which neighbouring
    squares share. Comparing marks rather than squares keeps a two-square
    straight move (e7e5, e1g1) from reading as three squares when the corners of
    the square between are all lit.

    A match is either the move's {from, to} or, for castling, its full king+rook
    set. Subsets do not match: a destination alone fits several moves and is a
    hint, not a move. A promotion pair returns one move per promotion piece, so
    more than one result means the move is not decided by the lights.

    Args:
        lit_marks: The marks the app lit.
        game_fen: Current game FEN; only the side to move's moves are considered.
        marks_for_square: The marks lighting a square (0=a1 .. 63=h8) produces.

    Returns:
        The matching legal moves, empty when the lights describe no move.
    """
    lit: Set[Hashable] = set(lit_marks)
    if not lit:
        return []

    def marks(squares: Set[int]) -> Set[Hashable]:
        return {mark for square in squares for mark in marks_for_square(square)}

    board = chess.Board(game_fen)
    return [
        move
        for move in board.legal_moves
        if lit in (marks({move.from_square, move.to_square}), marks(move_squares(board, move)))
    ]


def find_indicated_moves(lit_squares: Iterable[int], game_fen: str) -> List[chess.Move]:
    """Return the legal moves whose squares are exactly the lit squares.

    The square-per-light case of find_moves_lighting (Pegasus, Chessnut).

    Args:
        lit_squares: Square indices (0=a1 .. 63=h8) the app lit.
        game_fen: Current game FEN; only the side to move's moves are considered.
    """
    return find_moves_lighting(lit_squares, game_fen, lambda square: (square,))


__all__ = ["find_indicated_moves", "find_moves_lighting", "move_squares"]
