# Piece events
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""The lift/place events a physical board reports for a move.

A move played somewhere other than the physical board (the website's board) is
reported to a connected chess app as the events the board would have produced,
so an app that follows piece events sees the same move.
"""

from typing import List, Tuple

import chess

PIECE_LIFT = 0
PIECE_PLACE = 1


def castling_rook_squares(board: chess.Board, move: chess.Move) -> Tuple[int, int]:
    """Return the castling rook's (origin, destination) for a standard castle."""
    rank = chess.square_rank(move.from_square)
    if board.is_kingside_castling(move):
        return chess.square(7, rank), chess.square(5, rank)
    return chess.square(0, rank), chess.square(3, rank)


def piece_events_for_move(board: chess.Board, move: chess.Move) -> List[Tuple[int, int]]:
    """Return (piece_event, square) pairs for ``move`` played on ``board``.

    Fixed order: a capture lifts the captured piece, then lifts and places the
    capturing piece; en passant lifts the passed pawn from its own square;
    castling moves the king, then the rook. ``board`` is not modified.

    Args:
        board: Position before the move.
        move: A legal move in that position.
    """
    if board.is_castling(move):
        king_file = 6 if board.is_kingside_castling(move) else 2
        king_to = chess.square(king_file, chess.square_rank(move.from_square))
        rook_from, rook_to = castling_rook_squares(board, move)
        return [
            (PIECE_LIFT, move.from_square),
            (PIECE_PLACE, king_to),
            (PIECE_LIFT, rook_from),
            (PIECE_PLACE, rook_to),
        ]

    events: List[Tuple[int, int]] = []
    if board.is_en_passant(move):
        passed_pawn = chess.square(
            chess.square_file(move.to_square), chess.square_rank(move.from_square)
        )
        events.append((PIECE_LIFT, passed_pawn))
    elif board.is_capture(move):
        events.append((PIECE_LIFT, move.to_square))
    events.append((PIECE_LIFT, move.from_square))
    events.append((PIECE_PLACE, move.to_square))
    return events


__all__ = ["PIECE_LIFT", "PIECE_PLACE", "castling_rook_squares", "piece_events_for_move"]
