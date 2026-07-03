"""Ground-truth tactical facts about a move, for grounding the AI coach.

The coach model is not a chess engine and will otherwise assert tactical features
that do not exist -- e.g. calling the Ruy Lopez ``3.Bb5`` a "pin of the knight to
the king" when the ``d7`` pawn blocks the diagonal, so it is not a pin at all.
This module derives a short list of *verified* facts about a move directly from
the position with python-chess, so the coach prompt can tell the model to base its
tactical claims only on features that are actually true of the position.

Design
------
Pure and deterministic: no engine, network, or file I/O, so it runs identically in
the board and web processes and is fully unit-testable. Only facts that are
unconditionally true of the resulting position are emitted:

- captures (including en passant), castling, promotion;
- check / checkmate (from the actual resulting position);
- the moved piece's real targets (enemy knights or better it attacks from its new
  square -- the basis for genuine threats/forks);
- absolute pins the move creates (an enemy piece pinned to *its king* by the moved
  slider, detected via ``Board.pin`` so a blocked "pin" like the Ruy is excluded).

Relative pins (to the queen) and engine evaluations are intentionally out of scope
here: the former are not detectable as absolute pins, and the latter arrive
separately as the eval swing. Emitting only verifiable facts is the whole point --
a fabricated fact would defeat the purpose of grounding the model.
"""

from __future__ import annotations

from typing import List

import chess

_PIECE_NAMES = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}

# Targets worth reporting: a threat against a knight or better is meaningful; pawns
# and the king are excluded (checks are reported separately, and pawn attacks are
# low-signal noise for a two-sentence coaching remark).
_TARGET_TYPES = {chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN}
_SLIDERS = {chess.BISHOP, chess.ROOK, chess.QUEEN}


def summarize_move_facts(fen_before: str, move_uci: str) -> List[str]:
    """Return verified factual statements about ``move_uci`` played in ``fen_before``.

    Each returned string is a plain-English fact that is unconditionally true of
    the position after the move (a capture, check, castling, promotion, a real
    target of the moved piece, or an absolute pin it creates).

    Returns an empty list -- never a fabricated fact -- when the FEN is invalid,
    the move is unparseable, or the move is illegal in the position. The caller
    then simply prompts without a facts section rather than asserting anything
    unverified.
    """
    try:
        board = chess.Board(fen_before)
    except ValueError:
        return []
    try:
        move = chess.Move.from_uci(move_uci)
    except ValueError:
        return []
    # Facts are derived by playing the move; an illegal move has no well-defined
    # resulting position, so emit nothing rather than guess.
    if move not in board.legal_moves:
        return []

    enemy = not board.turn
    enemy_adj = "black" if enemy == chess.BLACK else "white"
    mover_piece = board.piece_at(move.from_square)
    facts: List[str] = []

    if board.is_en_passant(move):
        facts.append("Captures a pawn en passant.")
    elif board.is_capture(move):
        captured = board.piece_at(move.to_square)
        if captured is not None:
            facts.append(
                f"Captures the {enemy_adj} {_PIECE_NAMES[captured.piece_type]} "
                f"on {chess.square_name(move.to_square)}."
            )

    if board.is_castling(move):
        kingside = chess.square_file(move.to_square) > chess.square_file(
            move.from_square
        )
        facts.append(f"Castles {'kingside' if kingside else 'queenside'}.")

    if move.promotion is not None:
        facts.append(f"Promotes to a {_PIECE_NAMES[move.promotion]}.")

    after = board.copy(stack=False)
    after.push(move)

    if after.is_checkmate():
        facts.append("Delivers checkmate.")
    elif after.is_check():
        facts.append("Gives check.")

    to_sq = move.to_square
    moved = after.piece_at(to_sq)
    mover_name = _PIECE_NAMES[mover_piece.piece_type] if mover_piece else "piece"

    # Real targets: enemy knights-or-better the moved piece attacks from its new
    # square. These are the basis for genuine threats and forks (two+ targets).
    for sq in after.attacks(to_sq) & after.occupied_co[enemy]:
        target = after.piece_at(sq)
        if target is not None and target.piece_type in _TARGET_TYPES:
            facts.append(
                f"The {mover_name} attacks the {enemy_adj} "
                f"{_PIECE_NAMES[target.piece_type]} on {chess.square_name(sq)}."
            )

    # Absolute pins created by the moved piece: an enemy piece pinned to its own
    # king along a line the moved slider controls. Board.pin returns the pin ray
    # (or BB_ALL when not pinned), which is why a blocked "pin" like the Ruy 3.Bb5
    # (the d7 pawn sits behind the knight) is correctly not reported.
    if moved is not None and moved.piece_type in _SLIDERS:
        for sq in chess.SquareSet(after.occupied_co[enemy]):
            target = after.piece_at(sq)
            if target is None or target.piece_type == chess.KING:
                continue
            ray = after.pin(enemy, sq)
            if ray != chess.BB_ALL and to_sq in ray and sq in after.attacks(to_sq):
                facts.append(
                    f"Pins the {enemy_adj} {_PIECE_NAMES[target.piece_type]} "
                    f"on {chess.square_name(sq)} to the king."
                )

    return facts


__all__ = ["summarize_move_facts"]
