# Setup-mode policy
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""Policy for interpreting Chessnut LED commands as move indicators vs setup matrices.

A Chessnut client reuses the single LED command (0x0a) both to indicate a move
(lighting the destination, the from+to pair, or - for castling - the king and rook
squares) and, in puzzle mode, to show a SETUP MISMATCH matrix that lights every
square whose piece differs from the target.

:func:`classify_led_matrix` decides which case a given lit-square set represents,
using only the squares and the current game FEN. Keeping this pure (no hardware,
no emulator state) makes the emulator glue thin and the decision unit-testable.
"""

from typing import Iterable, Set

import chess

CLASS_OFF = "off"
CLASS_MOVE = "move"
CLASS_SETUP = "setup"

# Squares occupied in the standard start position (ranks 1-2 and 7-8).
_START_OCCUPIED: Set[int] = set(range(0, 16)) | set(range(48, 64))


def _occupied_squares(placement_fen: str) -> Set[int]:
    """Return the set of occupied squares for a placement (or full) FEN.

    Only occupancy is considered - piece identity is irrelevant, matching the
    Centaur hardware which senses presence only.
    """
    board = chess.Board()
    board.set_board_fen(placement_fen.split()[0])
    return set(chess.SquareSet(board.occupied))


def squares_to_restore_start(placement_fen: str) -> Set[int]:
    """Return the squares to light to return the board to the start occupancy.

    This is the symmetric difference between the current occupancy and the start
    occupancy: squares holding an extra piece that must be removed, plus empty
    home squares that must be refilled. Occupancy-only by design (identity is not
    sensed), so an identity-only difference yields an empty set.

    Args:
        placement_fen: Current position as a placement-only or full FEN.

    Returns:
        Square indices (0=a1 .. 63=h8) whose occupancy differs from start.
    """
    return _occupied_squares(placement_fen) ^ _START_OCCUPIED


def is_at_start_occupancy(placement_fen: str) -> bool:
    """Return True iff the occupancy matches the standard start position.

    Identity is not considered (e.g. a king/queen swap on their home squares is
    still "at start"), because the hardware cannot sense it.
    """
    return not squares_to_restore_start(placement_fen)


def infer_side_to_move(lit_squares: Iterable[int], placement_fen: str):
    """Infer the side to move from the app's first post-setup move indicator.

    After a setup handoff the app never sends a FEN, so the position is adopted
    provisionally as white-to-move. The app then lights the move it expects; the
    colour of the piece on that move's from-square is the real side to move.

    The two lit squares are the move's from and to. The side to move is the colour
    for which the position is legal (the side not to move is not left in check -
    so a king cannot be "captured") and one directed interpretation of the lit
    squares is a legal move. As a fallback, when exactly one square is occupied
    that square is the from-square and its piece's colour is the side to move.

    Args:
        lit_squares: Square indices the app lit (expected to be exactly two).
        placement_fen: Adopted position as a placement-only (or full) FEN.

    Returns:
        chess.WHITE or chess.BLACK, or None if the turn cannot be determined.
    """
    squares = list(lit_squares)
    if len(squares) != 2:
        return None
    placement = placement_fen.split()[0]
    square_set = set(squares)

    for turn in (chess.WHITE, chess.BLACK):
        board = chess.Board(f"{placement} {'w' if turn == chess.WHITE else 'b'} - - 0 1")
        # An invalid board (the side not to move is in check) means it cannot be
        # this colour's turn - e.g. the other interpretation would capture a king.
        if not board.is_valid():
            continue
        for move in board.legal_moves:
            if {move.from_square, move.to_square} == square_set:
                return turn

    # Fallback: the occupied square is the from-square (the to-square is empty).
    ref = chess.Board()
    ref.set_board_fen(placement)
    first, second = squares
    piece_first = ref.piece_at(first)
    piece_second = ref.piece_at(second)
    if piece_first is not None and piece_second is None:
        return piece_first.color
    if piece_second is not None and piece_first is None:
        return piece_second.color
    return None


def _move_squares(board: chess.Board, move: chess.Move) -> Set[int]:
    """Return all board squares a single move lights.

    For a normal move that is {from, to}. For castling it additionally includes
    the rook's origin and destination, because a client may light all four.
    """
    squares = {move.from_square, move.to_square}
    if board.is_castling(move):
        rank = chess.square_rank(move.from_square)
        if board.is_kingside_castling(move):
            squares.add(chess.square(7, rank))  # rook origin (h-file)
            squares.add(chess.square(5, rank))  # rook destination (f-file)
        else:
            squares.add(chess.square(0, rank))  # rook origin (a-file)
            squares.add(chess.square(3, rank))  # rook destination (d-file)
    return squares


def classify_led_matrix(lit_squares: Iterable[int], game_fen: str) -> str:
    """Classify an LED matrix as off / move / setup.

    Args:
        lit_squares: Square indices (0=a1 .. 63=h8) the client asked to light.
        game_fen: Current game FEN, used to enumerate legal moves.

    Returns:
        CLASS_OFF if nothing is lit; CLASS_MOVE if the lit squares are a non-empty
        subset of some single legal move's squares (destination-only, from+to, or
        castling king+rook); CLASS_SETUP otherwise.
    """
    lit: Set[int] = set(lit_squares)
    if not lit:
        return CLASS_OFF

    board = chess.Board(game_fen)
    for move in board.legal_moves:
        if lit <= _move_squares(board, move):
            return CLASS_MOVE

    return CLASS_SETUP


__all__ = [
    "classify_led_matrix",
    "squares_to_restore_start",
    "is_at_start_occupancy",
    "infer_side_to_move",
    "CLASS_OFF",
    "CLASS_MOVE",
    "CLASS_SETUP",
]
