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

from typing import Iterable, Optional, Tuple

from universalchess.services.coach import DEFAULT_LANGUAGE, CoachRequest
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
    is_opponent_move: bool = False,
    persona: Optional[str] = None,
    language: str = DEFAULT_LANGUAGE,
    chess960: bool = False,
) -> Optional[CoachRequest]:
    """Return a :class:`CoachRequest` for a move, or None if it can't be built.

    Args:
        fen_before: FEN of the position before the move.
        move_uci: The move in UCI (e.g. ``"e2e4"``).
        chess960: True for a Fischer Random game. 960 castling is encoded as a
            king-onto-rook move (e.g. ``f1h1``) that is only legal/notatable on a
            board built with ``chess960=True``; without this flag such a move is
            illegal on the standard board, so its SAN/LAN formatting and its
            "Castles" fact are lost and the coach mis-describes every 960 castle.
            The flag is also carried onto the returned request so later enrichment
            (MultiPV candidate lines) can rebuild the board 960-aware.
        notation: User's move notation ("figurine", "san", "lan", or "uci"); the
            move is formatted with it so the coach's remark matches the notation
            shown elsewhere. Unknown values fall back to the product default.
        eval_before_cp: Optional eval (centipawns, white's perspective) before.
        eval_after_cp: Optional eval (centipawns, white's perspective) after.
        is_potential_move: True when the move is a hint/tip the player is
            considering rather than a played move, so the prompt is framed as
            "why this would be a good move" instead of critiquing a played move.
        is_opponent_move: True when the played move was the opponent's, so the
            prompt tells the coach to explain what the opponent is doing/threatening
            and address the player about it, rather than critiquing it as the
            player's own move. Should match the move context used to pick the
            persona so framing and persona agree.
        persona: Optional coach persona (from the selected coach) carried onto the
            request so the service composes it into the system prompt.
        language: Natural language the coach must respond in (defaults to English,
            which adds no prompt instruction). Carried onto the request so the
            service appends the language directive to the system prompt.

    Returns None when the FEN or move is invalid/illegal for that position, so
    the caller produces no coach statement rather than prompting the AI with a
    fabricated position or move. Formatting needs a legal move for SAN/LAN; an
    unformattable move falls back to its UCI string as the ``move_text`` so a
    valid-FEN/odd-move case still yields a usable prompt.
    """
    import chess

    try:
        board = chess.Board(fen_before, chess960=chess960)
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
        facts=tuple(summarize_move_facts(fen_before, move_uci, chess960=chess960)),
        is_potential_move=is_potential_move,
        is_opponent_move=is_opponent_move,
        persona=persona,
        language=language,
        chess960=chess960,
    )


def _format_white_eval(score) -> str:
    """Format a python-chess PovScore as a white's-perspective pawn/mate string.

    Matches the coach's white's-perspective convention (see coach._format_eval):
    a numeric eval reads like ``+0.30``/``-1.20`` (pawns), a forced mate like
    ``#3``/``#-2``. Returns "" when the score is missing or unscored so the
    candidate line falls back to just the move text rather than a fabricated eval.
    """
    if score is None:
        return ""
    white = score.white()
    if white.is_mate():
        mate = white.mate()
        if mate is None:
            return ""
        return f"#{mate}" if mate > 0 else f"#-{abs(mate)}"
    centipawns = white.score()
    if centipawns is None:
        return ""
    return f"{centipawns / 100.0:+.2f}"


def format_candidate_lines(
    fen_before: str,
    infos: Iterable[dict],
    notation: str = DEFAULT_NOTATION,
    *,
    chess960: bool = False,
) -> Tuple[str, ...]:
    """Format MultiPV analysis results into coach candidate-line strings.

    Turns the ``analyse(multipv=N)`` output for the position before a move into
    strings like ``"e4 (+0.30)"`` -- the first move of each principal variation
    in the user's notation, with its white's-perspective eval -- ordered best
    first (the order python-chess returns). Used to give the AI coach the
    engine's preferred/alternative moves without leaking python-chess types into
    the service layer.

    ``chess960`` must be True for a Fischer Random game so a candidate that is a
    960 castle (king-onto-rook, e.g. ``f1h1``) is legal for SAN formatting; without
    it the engine's top move would fall back to raw UCI and read as a non-castle.

    An info with no ``pv`` (no move) is skipped. An unformattable move (illegal
    for the FEN -- corrupt data) falls back to its UCI string rather than dropping
    the line. Returns an empty tuple when the FEN is invalid or nothing usable is
    present, so the caller simply omits the alternatives block.
    """
    import chess

    try:
        board = chess.Board(fen_before, chess960=chess960)
    except ValueError:
        return ()

    lines = []
    for info in infos:
        pv = info.get("pv")
        if not pv:
            continue
        move = pv[0]
        try:
            move_text = format_move(board, move, notation)
        except (ValueError, AssertionError):
            move_text = move.uci()
        eval_text = _format_white_eval(info.get("score"))
        lines.append(f"{move_text} ({eval_text})" if eval_text else move_text)
    return tuple(lines)


__all__ = ["build_coach_request", "format_candidate_lines"]
