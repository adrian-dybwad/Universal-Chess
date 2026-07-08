"""Detect fabricated or illegal moves named in an AI coach statement.

The coach writes free-form prose and nothing else verifies the moves it names, so
a hallucinated line -- e.g. "what if the opponent plays cxd4 in response to d3"
when d4 is empty -- is shown verbatim. This module extracts move-like tokens from
a statement and checks each against the actual position, so such a statement can
be caught and regenerated instead of shown.

Grounding
---------
A named move is considered *supported* when it is legal either in the position
before the played move (the mover's own move or an alternative it discusses) or in
the position after the played move (an opponent reply). Checking both positions
means a legitimate reference to either side's options passes; only a move that is
legal in neither is treated as fabricated.

Scope (why only some forms are validated)
-----------------------------------------
Only *unambiguous* move forms are validated: captures ("cxd4", "Bxb5"), piece
moves ("Nf3"), castling ("O-O"), promotions ("e8=Q"), and UCI ("e2e4"). A bare
pawn destination like "d4" is indistinguishable from a square reference ("the d4
square is weak"), so validating it would produce false positives; such tokens are
deliberately skipped. This still catches the damaging hallucinations (invented
captures, piece moves, and forks) that read as concrete but impossible tactics.

Pure and deterministic: no engine, network, or file I/O, so it runs identically in
the board and web processes and is fully unit-testable.
"""

from __future__ import annotations

import re
from typing import List, Optional

import chess

# Move-like tokens that are unambiguously moves rather than square references:
# castling, a piece move/capture (leading K/Q/R/B/N), a pawn capture (file 'x'
# file rank, optionally promoting), a pawn promotion, or a 4-5 char UCI move. A
# bare pawn destination ("d4") is intentionally excluded -- see the module doc.
_TOKEN_RE = re.compile(
    r"O-O-O|O-O|0-0-0|0-0"                       # castling (long before short)
    r"|[KQRBN][a-h1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?"  # piece move/capture
    r"|[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?"        # pawn capture (optional promo)
    r"|[a-h][18]=[QRBN][+#]?"                    # pawn promotion (non-capture)
    r"|[a-h][1-8][a-h][1-8][qrbn]?"             # UCI
)


_PIECE_WORDS = {
    "pawn": chess.PAWN,
    "knight": chess.KNIGHT,
    "bishop": chess.BISHOP,
    "rook": chess.ROOK,
    "queen": chess.QUEEN,
    "king": chess.KING,
}

# Explicit occupancy claims: "<piece> on <square>" ("pawn on d4") and the reversed
# "<square> <piece>" ("d4 pawn"). These assert a piece stands on a square right now,
# the class of hallucination the move check cannot see (it is not a move).
_PIECE_ON_SQUARE_RE = re.compile(
    r"\b(pawn|knight|bishop|rook|queen|king)s?\s+on\s+([a-h][1-8])\b",
    re.IGNORECASE,
)
_SQUARE_PIECE_RE = re.compile(
    r"\b([a-h][1-8])\s+(pawn|knight|bishop|rook|queen|king)s?\b",
    re.IGNORECASE,
)


def _square_has_piece_type(board: chess.Board, square_name: str, piece_type: int) -> bool:
    """True when a piece of ``piece_type`` (either color) stands on ``square_name``."""
    piece = board.piece_at(chess.parse_square(square_name))
    return piece is not None and piece.piece_type == piece_type


def _uci_move_is_legal(board: chess.Board, token: str) -> bool:
    """True when ``token`` parses as a UCI move that is legal on ``board``."""
    try:
        move = chess.Move.from_uci(token.lower())
    except ValueError:
        return False
    return move in board.legal_moves


def _is_legal(board: chess.Board, token: str) -> bool:
    """True when ``token`` is a legal move on ``board`` (SAN or UCI).

    An ambiguous SAN (e.g. "Nd7" when two knights can reach d7) still names a real
    legal move, only underspecified, so it is treated as supported rather than
    flagged. A token that is not a legal/parseable SAN is retried as UCI; only a
    token that is neither returns False.
    """
    normalized = token.replace("0", "O") if token[:1] in ("0", "O") else token
    try:
        board.parse_san(normalized)
        return True
    except chess.AmbiguousMoveError:
        return True
    except ValueError:
        # Not a legal/parseable SAN (IllegalMoveError/InvalidMoveError); the token
        # may instead be UCI, so fall back to that rather than declaring it illegal.
        return _uci_move_is_legal(board, token)


def find_unsupported_moves(
    statement: str,
    fen_before: str,
    move_uci: str,
    *,
    chess960: bool = False,
) -> List[str]:
    """Return the move tokens in ``statement`` that are illegal in the position.

    A token is supported when legal in the position before the move or in the
    position after ``move_uci`` (an opponent reply). Returns the fabricated tokens
    in the order they appear (de-duplicated). Returns an empty list -- validating
    nothing -- when the FEN is invalid, so a data problem never blocks coaching.

    Args:
        statement: The coach's generated remark.
        fen_before: FEN of the position before the played move.
        move_uci: The played move in UCI, used to build the after-move position for
            validating opponent replies. When empty or illegal, only the before
            position is used.
        chess960: True for a Fischer Random game so castling (king-onto-rook) is
            legal for parsing.
    """
    board_before, board_after = _both_boards(fen_before, move_uci, chess960)
    if board_before is None:
        return []

    unsupported: List[str] = []
    seen = set()
    for match in _TOKEN_RE.finditer(statement):
        token = match.group()
        if token in seen:
            continue
        seen.add(token)
        if _is_legal(board_before, token):
            continue
        if board_after is not None and _is_legal(board_after, token):
            continue
        unsupported.append(token)
    return unsupported


def has_unsupported_move(
    statement: str,
    fen_before: str,
    move_uci: str,
    *,
    chess960: bool = False,
) -> bool:
    """True when ``statement`` names at least one move illegal in the position."""
    return bool(
        find_unsupported_moves(statement, fen_before, move_uci, chess960=chess960)
    )


def _both_boards(fen_before: str, move_uci: str, chess960: bool):
    """Return (board_before, board_after|None) or (None, None) on an invalid FEN."""
    try:
        board_before = chess.Board(fen_before, chess960=chess960)
    except ValueError:
        return None, None
    board_after: Optional[chess.Board] = None
    if move_uci:
        try:
            played = chess.Move.from_uci(move_uci)
        except ValueError:
            played = None
        if played is not None and played in board_before.legal_moves:
            board_after = board_before.copy(stack=False)
            board_after.push(played)
    return board_before, board_after


def find_unsupported_claims(
    statement: str,
    fen_before: str,
    move_uci: str,
    *,
    chess960: bool = False,
) -> List[str]:
    """Return phrases claiming a piece on a square where it is not actually present.

    Catches occupancy hallucinations the move check cannot (e.g. "the pawn on d4"
    when d4 is empty). A claim is flagged only when the named piece type is on that
    square in neither the position before the move nor the position after it, so a
    legitimate reference to a piece in either position passes. Color is not checked
    (type-only) to avoid brittle "your/their" parsing; the aim is to reject pieces
    that are simply not there. Returns [] on an invalid FEN so a data problem never
    blocks coaching.
    """
    board_before, board_after = _both_boards(fen_before, move_uci, chess960)
    if board_before is None:
        return []

    flagged: List[str] = []
    seen = set()
    matches = [
        (m.group(0), m.group(1).lower(), m.group(2).lower())
        for m in _PIECE_ON_SQUARE_RE.finditer(statement)
    ] + [
        (m.group(0), m.group(2).lower(), m.group(1).lower())
        for m in _SQUARE_PIECE_RE.finditer(statement)
    ]
    for phrase, piece_word, square in matches:
        if phrase in seen:
            continue
        seen.add(phrase)
        piece_type = _PIECE_WORDS[piece_word]
        if _square_has_piece_type(board_before, square, piece_type):
            continue
        if board_after is not None and _square_has_piece_type(
            board_after, square, piece_type
        ):
            continue
        flagged.append(phrase)
    return flagged


def find_grounding_problems(
    statement: str,
    fen_before: str,
    move_uci: str,
    *,
    chess960: bool = False,
) -> List[str]:
    """Return all fabricated references in a statement: illegal moves and false
    piece-on-square claims. The single check used before showing a coach statement.
    """
    return find_unsupported_moves(
        statement, fen_before, move_uci, chess960=chess960
    ) + find_unsupported_claims(statement, fen_before, move_uci, chess960=chess960)


__all__ = [
    "find_grounding_problems",
    "find_unsupported_claims",
    "find_unsupported_moves",
    "has_unsupported_move",
]
