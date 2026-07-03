"""Build a :class:`CoachRequest` from a FEN and a UCI move.

This is the one place that turns a raw ``(fen_before, move_uci)`` pair into the
move-text/side/move-number context the coach prompt needs, so the board's
live-game request builder, the web coach endpoints, and the tip generator all
derive that context identically (no drift between "why the played move" and "why
the hinted move" prompts).

The move is rendered in the user's chosen ``notation`` via the shared
:func:`universalchess.utils.chess_notation.format_move`, so the coach refers to a
move the same way it appears in the board/web move list.

Kept separate from :mod:`universalchess.services.coach` because it depends on
``python-chess`` (for notation and side-to-move), whereas the service layer is a
pure, network-only module with no chess dependency.
"""

from __future__ import annotations

from typing import Optional

from universalchess.services.coach import CoachRequest
from universalchess.utils.chess_notation import DEFAULT_NOTATION, format_move

from .move_facts import summarize_move_facts


def build_coach_request(
    fen_before: str,
    move_uci: str,
    *,
    notation: str = DEFAULT_NOTATION,
    eval_before_cp: Optional[int] = None,
    eval_after_cp: Optional[int] = None,
    is_potential_move: bool = False,
) -> Optional[CoachRequest]:
    """Return a :class:`CoachRequest` for a move, or None if it can't be built.

    Args:
        fen_before: FEN of the position before the move.
        move_uci: The move in UCI (e.g. ``"e2e4"``).
        notation: User's move notation ("figurine", "san", "lan", or "uci"); the
            move is formatted with it so the coach's remark matches the notation
            shown elsewhere. Unknown values fall back to the product default.
        eval_before_cp: Optional eval (centipawns, white's perspective) before.
        eval_after_cp: Optional eval (centipawns, white's perspective) after.
        is_potential_move: True when the move is a hint/tip the player is
            considering rather than a played move, so the prompt is framed as
            "why this would be a good move" instead of critiquing a played move.

    Returns None when the FEN or move is invalid/illegal for that position, so
    the caller produces no coach statement rather than prompting the AI with a
    fabricated position or move. Formatting needs a legal move for SAN/LAN; an
    unformattable move falls back to its UCI string as the ``move_text`` so a
    valid-FEN/odd-move case still yields a usable prompt.
    """
    import chess

    try:
        board = chess.Board(fen_before)
    except ValueError:
        return None

    try:
        move = chess.Move.from_uci(move_uci)
    except ValueError:
        return None

    # Notation rendering (SAN/LAN) needs a legal move; if the stored move isn't
    # legal in this position (corrupt data), fall back to the UCI text rather than
    # dropping the request.
    try:
        move_text = format_move(board, move, notation)
    except (ValueError, AssertionError):
        move_text = move_uci

    side_to_move = "white" if board.turn == chess.WHITE else "black"
    return CoachRequest(
        fen_before=fen_before,
        move_text=move_text,
        side_to_move=side_to_move,
        eval_before_cp=eval_before_cp,
        eval_after_cp=eval_after_cp,
        move_number=board.fullmove_number,
        facts=tuple(summarize_move_facts(fen_before, move_uci)),
        is_potential_move=is_potential_move,
    )


__all__ = ["build_coach_request"]
