"""Chess move notation formatting for move-history display.

The board (e-paper analysis widget) and the web share a single ``game.notation``
setting. This module is the board-side formatter: it turns a played game's move
stack into display strings in the selected notation. Kept as pure functions over
``chess.Board`` / ``chess.Move`` so it is directly unit-testable and free of any
display or settings dependency.

Supported notations (matching the ``notation`` option set in the menu catalog):
    - ``figurine``: SAN with piece letters replaced by figurine glyphs (default).
    - ``san``: standard algebraic notation (``Nf3``).
    - ``lan``: long algebraic notation (``Ng1-f3``).
    - ``uci``: pure coordinate notation (``g1f3``).
"""

from typing import List

import chess

DEFAULT_NOTATION = "figurine"

# Supported notation identifiers, in menu order.
NOTATIONS = ("figurine", "san", "lan", "uci")

# White-outline figurine glyphs, used for both colors per common FAN usage.
_FIGURINE_GLYPHS = {
    "K": "\u2654",
    "Q": "\u2655",
    "R": "\u2656",
    "B": "\u2657",
    "N": "\u2658",
}


def normalize_notation(value: str) -> str:
    """Coerce an arbitrary setting value to a supported notation.

    Falls back to the product default (figurine) for unknown/empty values so a
    stale or malformed config never breaks rendering.
    """
    return value if value in NOTATIONS else DEFAULT_NOTATION


def _to_figurine(algebraic: str) -> str:
    """Replace piece letters (K, Q, R, B, N) with figurine glyphs.

    Files are lowercase and ranks are digits, and castling uses 'O', so only true
    piece letters are affected -- including the promotion piece in ``e8=Q``.
    """
    return "".join(_FIGURINE_GLYPHS.get(ch, ch) for ch in algebraic)


def format_move(board_before: chess.Board, move: chess.Move, notation: str) -> str:
    """Format a single move made from ``board_before`` in ``notation``.

    Args:
        board_before: Position immediately before ``move`` is played. Required so
            SAN/LAN disambiguation and check/mate suffixes are computed correctly.
        move: The move to format (must be legal in ``board_before``).
        notation: Target notation; unknown values fall back to figurine.
    """
    notation = normalize_notation(notation)
    if notation == "uci":
        return move.uci()
    if notation == "lan":
        return board_before.lan(move)
    san = board_before.san(move)
    if notation == "san":
        return san
    return _to_figurine(san)


def format_move_history(board: chess.Board, notation: str) -> List[str]:
    """Format every move in ``board``'s move stack in ``notation``.

    Replays the moves from the board's root position (``board.root()``) so games
    that started from a custom FEN (e.g. a set-up position) disambiguate and
    number correctly rather than being replayed from the standard start.

    Returns:
        One formatted string per played move, in play order.
    """
    notation = normalize_notation(notation)
    work = board.root()
    formatted: List[str] = []
    for move in board.move_stack:
        formatted.append(format_move(work, move, notation))
        work.push(move)
    return formatted
