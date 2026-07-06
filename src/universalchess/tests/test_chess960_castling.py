#!/usr/bin/env python3
"""Tests for variant-aware physical-castling helpers in player_moves.

Why this exists
---------------
Physical castling on the DGT Centaur is a two-part gesture: the king move is
accepted, then a rook-follow (lift rook_from, place rook_to) completes the
castle. Chess960 breaks the standard assumptions the helpers baked in:

- The rook is no longer always in the corner (h/a file), so the rook-follow
  origin must come from the actual position, not a fixed file.
- python-chess encodes 960 castling as king-onto-rook (``to_square`` is the
  rook), and the two-square/king-onto-rook rejection used for standard chess
  must NOT reject the legal 960 form.
- A player may slide the king to its final square (g/c) OR onto the rook; both
  must map to python-chess's canonical castling move.

These tests pin those three behaviors for standard chess (unchanged) and for a
non-corner Chess960 position, which is exactly where a standard-only
implementation produces the wrong rook square or rejects a legal castle.
"""

import chess

from universalchess.managers.game.player_moves import (
    _castling_rook_move,
    _is_king_onto_rook_castle,
    _resolve_castling_gesture,
)

# Standard chess: king e1, rooks a1/h1.
STANDARD_FEN = "4k3/8/8/8/8/8/8/R3K2R w KQ - 0 1"
# Chess960, non-corner king: king on b1, rooks a1 (queenside) and h1 (kingside),
# back rank otherwise clear so both castlings are legal. Kingside king lands g1
# with rook h1->f1; queenside king lands c1 with rook a1->d1.
FRC_FEN = "1k6/8/8/8/8/8/8/RK5R w KQ - 0 1"


def _standard_board():
    return chess.Board(STANDARD_FEN)


def _frc_board():
    return chess.Board(FRC_FEN, chess960=True)


def test_standard_rook_follow_unchanged():
    """Standard castling still yields corner->crossed-square rook follows.

    Guards against the 960 changes regressing normal chess: kingside must remain
    h1->f1 and queenside a1->d1 for the king's two-square move.
    """
    board = _standard_board()
    kingside = chess.Move(chess.E1, chess.G1)
    queenside = chess.Move(chess.E1, chess.C1)
    assert _castling_rook_move(board, kingside) == (chess.H1, chess.F1)
    assert _castling_rook_move(board, queenside) == (chess.A1, chess.D1)


def test_standard_rejects_king_onto_rook_form():
    """In standard chess the king-onto-rook gesture (e1h1) stays rejected.

    Only the two-square gesture is supported for standard chess; accepting e1h1
    would let a stray king-onto-rook interaction be read as a castle. The
    two-square form must not be rejected.
    """
    board = _standard_board()
    assert _is_king_onto_rook_castle(board, chess.Move(chess.E1, chess.H1)) is True
    assert _is_king_onto_rook_castle(board, chess.Move(chess.E1, chess.A1)) is True
    assert _is_king_onto_rook_castle(board, chess.Move(chess.E1, chess.G1)) is False


def test_standard_resolve_is_noop():
    """Gesture resolution never rewrites a standard-chess move.

    The 960 gesture mapping must be inert for standard games; otherwise a normal
    king move could be silently swapped for a castling move.
    """
    board = _standard_board()
    move = chess.Move(chess.E1, chess.G1)
    assert _resolve_castling_gesture(board, move) == move


def test_frc_rook_follow_uses_actual_rook_squares():
    """960 rook-follow origin is the rook's real square; destination is f/d file.

    This is the core 960 fix: with king on b1 and rooks on a1/h1, kingside must
    give h1->f1 and queenside a1->d1 derived from the king-onto-rook encoding,
    not from fixed corner assumptions relative to the king. A regression here
    would light the wrong rook-follow squares and drop the board into correction
    mode on every 960 castle.
    """
    board = _frc_board()
    kingside = chess.Move(chess.B1, chess.H1)  # canonical king-onto-rook
    queenside = chess.Move(chess.B1, chess.A1)
    assert board.is_kingside_castling(kingside) is True
    assert _castling_rook_move(board, kingside) == (chess.H1, chess.F1)
    assert _castling_rook_move(board, queenside) == (chess.A1, chess.D1)


def test_frc_never_rejects_king_onto_rook():
    """The king-onto-rook form is legal in 960 and must never be rejected.

    python-chess only emits the king-onto-rook encoding for 960 castling; if
    _is_king_onto_rook_castle rejected it (as it does for standard chess), no 960
    castle could ever be played.
    """
    board = _frc_board()
    assert _is_king_onto_rook_castle(board, chess.Move(chess.B1, chess.H1)) is False
    assert _is_king_onto_rook_castle(board, chess.Move(chess.B1, chess.A1)) is False


def test_frc_resolves_king_to_final_square_gesture():
    """Sliding the king to g1/c1 maps to the canonical king-onto-rook move.

    A player naturally moves the king to its final square. python-chess would
    reject Move(b1, g1) as illegal, so it must be resolved to Move(b1, h1)
    (kingside) / Move(b1, a1) (queenside). Without this, the common physical
    gesture never castles.
    """
    board = _frc_board()
    assert _resolve_castling_gesture(board, chess.Move(chess.B1, chess.G1)) == chess.Move(
        chess.B1, chess.H1
    )
    assert _resolve_castling_gesture(board, chess.Move(chess.B1, chess.C1)) == chess.Move(
        chess.B1, chess.A1
    )


def test_frc_resolves_king_onto_rook_gesture_identity():
    """The king-onto-rook gesture resolves to itself (already canonical).

    Ensures the resolver accepts the direct king-onto-rook placement too, not
    only the king-to-final-square gesture.
    """
    board = _frc_board()
    assert _resolve_castling_gesture(board, chess.Move(chess.B1, chess.H1)) == chess.Move(
        chess.B1, chess.H1
    )
    assert _resolve_castling_gesture(board, chess.Move(chess.B1, chess.A1)) == chess.Move(
        chess.B1, chess.A1
    )


def test_frc_resolve_leaves_non_castling_king_move_untouched():
    """A one-square king step is not a castle and must be returned unchanged.

    The resolver keys off legal castling targets; a normal king move to an
    adjacent empty square must not be rewritten into a castle.
    """
    board = _frc_board()
    normal = chess.Move(chess.B1, chess.B2)
    assert _resolve_castling_gesture(board, normal) == normal
