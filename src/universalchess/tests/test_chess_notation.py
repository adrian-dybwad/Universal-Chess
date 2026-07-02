"""Tests for board-side move-history notation formatting.

Why these tests exist
---------------------
The e-paper analysis widget renders the played move list in the notation chosen
by the shared ``game.notation`` setting. format_move_history is the single
formatter behind that display, so a regression here (wrong glyph, dropped
capture marker, missing disambiguation, or replay from the wrong root) shows up
only as wrong text on the board with no other signal. Each test pins a distinct
edge across the four notations.
"""

import chess
import pytest

from universalchess.utils.chess_notation import (
    DEFAULT_NOTATION,
    format_move,
    format_move_history,
    normalize_notation,
)

# Figurine glyphs, referenced by name so the expected strings read clearly.
KNIGHT = "\u2658"
BISHOP = "\u2657"
QUEEN = "\u2655"


def _board_after(ucis):
    """Build a board by playing a list of UCI moves from the standard start."""
    board = chess.Board()
    for uci in ucis:
        board.push(chess.Move.from_uci(uci))
    return board


def test_format_move_all_notations_for_quiet_piece_move():
    # Nf3 from the start: the canonical piece move. Verifies figurine swaps the
    # knight letter for its glyph, LAN inserts the origin square, and UCI is the
    # raw coordinates -- one move exercised through every branch.
    board = chess.Board()
    move = chess.Move.from_uci("g1f3")
    assert format_move(board, move, "san") == "Nf3"
    assert format_move(board, move, "figurine") == f"{KNIGHT}f3"
    assert format_move(board, move, "lan") == "Ng1-f3"
    assert format_move(board, move, "uci") == "g1f3"


def test_format_move_capture_marks_capture_in_lan():
    # exd5 (a pawn capture): the SAN 'x' must survive, and LAN must use the 'x'
    # separator. A regression dropping the capture would render 'e4-d5'.
    board = _board_after(["e2e4", "d7d5"])
    move = chess.Move.from_uci("e4d5")
    assert format_move(board, move, "san") == "exd5"
    assert format_move(board, move, "lan") == "e4xd5"
    assert format_move(board, move, "uci") == "e4d5"


def test_format_move_castling_is_preserved():
    # O-O: castling has no piece letter to convert and LAN keeps the SAN form.
    # Guards against figurine altering 'O' or LAN expanding to a king move.
    board = _board_after(["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6"])
    move = chess.Move.from_uci("e1g1")
    assert format_move(board, move, "san") == "O-O"
    assert format_move(board, move, "figurine") == "O-O"
    assert format_move(board, move, "lan") == "O-O"
    assert format_move(board, move, "uci") == "e1g1"


def test_format_move_promotion_with_check_applies_glyph_to_promoted_piece():
    # d8=Q+: the promoted piece must become a glyph in figurine and LAN must
    # carry '=Q' plus the '+' check suffix; UCI lowercases the promotion.
    board = chess.Board("4k3/3P4/8/8/8/8/8/4K3 w - - 0 1")
    move = chess.Move.from_uci("d7d8q")
    assert format_move(board, move, "san") == "d8=Q+"
    assert format_move(board, move, "figurine") == f"d8={QUEEN}+"
    assert format_move(board, move, "lan") == "d7-d8=Q+"
    assert format_move(board, move, "uci") == "d7d8q"


def test_format_move_history_returns_all_moves_in_order():
    # A short opening: the history must contain exactly one entry per played move,
    # in order, formatted in the requested notation. A wrong count means moves are
    # dropped/duplicated; wrong order means the replay diverged.
    board = _board_after(["e2e4", "e7e5", "g1f3", "b8c6"])
    assert format_move_history(board, "san") == ["e4", "e5", "Nf3", "Nc6"]
    assert format_move_history(board, "figurine") == [
        "e4",
        "e5",
        f"{KNIGHT}f3",
        f"{KNIGHT}c6",
    ]
    assert format_move_history(board, "uci") == ["e2e4", "e7e5", "g1f3", "b8c6"]


def test_format_move_history_replays_from_custom_root():
    # A game set up from a custom FEN must be formatted relative to that FEN, not
    # the standard start. If the formatter replayed from the standard opening, the
    # first move here (a rook move only legal in this position) would raise or
    # disambiguate wrongly. The empty stack case returns [].
    start_fen = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"
    board = chess.Board(start_fen)
    assert format_move_history(board, "san") == []
    board.push(chess.Move.from_uci("a1d1"))  # Rd1
    board.push(chess.Move.from_uci("a8d8"))  # Rd8
    assert format_move_history(board, "san") == ["Rd1", "Rd8"]
    assert format_move_history(board, "lan") == ["Ra1-d1", "Ra8-d8"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("san", "san"),
        ("lan", "lan"),
        ("uci", "uci"),
        ("figurine", "figurine"),
        ("", DEFAULT_NOTATION),
        ("bogus", DEFAULT_NOTATION),
    ],
)
def test_normalize_notation_falls_back_to_default(value, expected):
    # Unknown/empty settings values must resolve to figurine so a stale config
    # renders a valid notation instead of crashing or blanking.
    assert normalize_notation(value) == expected
