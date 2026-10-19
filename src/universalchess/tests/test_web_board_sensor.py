"""The web board as a stand-in sensor: pure decoding and event synthesis.

Why these tests exist
---------------------
While a chess app (Millennium, Pegasus, Chessnut) is connected and the user plays
on the website's board, the web board takes the place of the physical board.
Two pure pieces make that possible:

- ``find_indicated_moves`` reads the app's LED indication (the squares it lit for
  its own reply) back into the legal moves those squares describe, so the reply
  can be played without anyone moving a physical piece.
- ``piece_events_for_move`` produces the lift/place sequence a physical board
  would have reported for a move, so an app that only follows piece events
  (Pegasus) sees a web move as if it were made on the board.

How a regression manifests
--------------------------
A lit pair decodes to no move (the reply stalls waiting for the physical board),
to the wrong move, or to a unique move when it is a promotion (the app's piece
choice would be fabricated). A synthesized sequence drops the captured piece's
lift or the castling rook, so the app's occupancy model drifts from the game.
"""

import chess
import pytest

from universalchess.board.move_indication import find_indicated_moves
from universalchess.board.piece_events import PIECE_LIFT, PIECE_PLACE, piece_events_for_move

AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
# White to move; white pawn on b7 can promote straight (b8) or by capturing a8/c8.
PROMOTION_FEN = "r1n1k3/1P6/8/8/8/8/8/4K3 w - - 0 1"
# White may castle both ways; nothing between king and rooks.
CASTLING_FEN = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"
# Black pawn on d4 can capture en passant on e3 after White's e2e4.
EN_PASSANT_FEN = "4k3/8/8/8/3pP3/8/8/4K3 b - e3 0 1"
# White knight on f3 can capture the black pawn on e5.
CAPTURE_FEN = "4k3/8/8/4p3/8/5N2/8/4K3 w - - 0 1"


def _ucis(moves):
    return sorted(move.uci() for move in moves)


@pytest.mark.parametrize(
    ("lit", "fen", "expected"),
    [
        pytest.param({chess.E7, chess.E5}, AFTER_E4, ["e7e5"], id="from-to-pair"),
        pytest.param({chess.E5, chess.E7}, AFTER_E4, ["e7e5"], id="pair-order-irrelevant"),
        pytest.param({chess.E1, chess.G1}, CASTLING_FEN, ["e1g1"], id="castle-king-squares"),
        pytest.param(
            {chess.E1, chess.G1, chess.H1, chess.F1},
            CASTLING_FEN,
            ["e1g1"],
            id="castle-king-and-rook",
        ),
        pytest.param(
            {chess.E1, chess.C1, chess.A1, chess.D1},
            CASTLING_FEN,
            ["e1c1"],
            id="long-castle-king-and-rook",
        ),
        pytest.param(
            {chess.B7, chess.B8},
            PROMOTION_FEN,
            ["b7b8b", "b7b8n", "b7b8q", "b7b8r"],
            id="promotion-all-pieces",
        ),
    ],
)
def test_find_indicated_moves_reads_the_lit_move(lit, fen, expected):
    """A lit move pattern decodes to exactly the legal moves it describes.

    The app lights only squares, never a piece, so a promotion pair must return
    all four promotion moves for the caller to treat as undecided. Regression:
    a pair returns [] (reply stalls), a castle lit as four squares is missed,
    or a promotion pair collapses to one move (the piece would be invented).
    """
    assert _ucis(find_indicated_moves(lit, fen)) == expected


@pytest.mark.parametrize(
    ("lit", "fen"),
    [
        pytest.param(set(), AFTER_E4, id="nothing-lit"),
        pytest.param({chess.E5}, AFTER_E4, id="destination-only"),
        # The user's own last move, lit by an app that echoes it: no black move uses these squares.
        pytest.param({chess.E2, chess.E4}, AFTER_E4, id="side-not-to-move"),
        pytest.param({chess.A2, chess.H7, chess.D4}, AFTER_E4, id="setup-matrix"),
        pytest.param({chess.E7, chess.E4}, AFTER_E4, id="illegal-pair"),
    ],
)
def test_find_indicated_moves_rejects_anything_but_an_exact_move(lit, fen):
    """Only an exact move pattern decodes; anything else returns no moves.

    Destination-only lights match several moves, an echoed last move is the other
    side's, and a setup matrix is not a move. Each must decode to [] so nothing is
    played on the app's behalf. Regression: a non-empty list here would auto-play
    a move the app never asked for.
    """
    assert find_indicated_moves(lit, fen) == []


def _events(fen, uci):
    return piece_events_for_move(chess.Board(fen), chess.Move.from_uci(uci))


@pytest.mark.parametrize(
    ("fen", "uci", "expected"),
    [
        pytest.param(
            chess.STARTING_FEN,
            "e2e4",
            [(PIECE_LIFT, chess.E2), (PIECE_PLACE, chess.E4)],
            id="quiet",
        ),
        pytest.param(
            CAPTURE_FEN,
            "f3e5",
            [(PIECE_LIFT, chess.E5), (PIECE_LIFT, chess.F3), (PIECE_PLACE, chess.E5)],
            id="capture-lifts-victim-first",
        ),
        pytest.param(
            EN_PASSANT_FEN,
            "d4e3",
            [(PIECE_LIFT, chess.E4), (PIECE_LIFT, chess.D4), (PIECE_PLACE, chess.E3)],
            id="en-passant-lifts-passed-pawn",
        ),
        pytest.param(
            CASTLING_FEN,
            "e1g1",
            [
                (PIECE_LIFT, chess.E1),
                (PIECE_PLACE, chess.G1),
                (PIECE_LIFT, chess.H1),
                (PIECE_PLACE, chess.F1),
            ],
            id="castle-king-then-rook",
        ),
        pytest.param(
            PROMOTION_FEN,
            "b7c8q",
            [(PIECE_LIFT, chess.C8), (PIECE_LIFT, chess.B7), (PIECE_PLACE, chess.C8)],
            id="capturing-promotion",
        ),
    ],
)
def test_piece_events_for_move_matches_a_physical_board(fen, uci, expected):
    """The synthesized events are the sequence a physical board reports.

    The order is fixed so the app sees one deterministic sequence: a capture
    removes the captured piece, then moves the capturing piece; en passant lifts
    the passed pawn from its own square, not the destination; castling moves the
    king, then the rook. Every lifted square ends empty and every placed square
    ends occupied, as in the game after the move. Regression: a missing lift
    leaves the app thinking the captured square still holds its own piece; a
    missing rook pair leaves the rook on its home square in the app's model.
    """
    assert _events(fen, uci) == expected


def test_piece_events_for_move_does_not_mutate_the_board():
    """The caller's board must be untouched; it is the live game board.

    Regression: the board has the move pushed (or popped) after the call, so the
    game would advance twice or not at all.
    """
    board = chess.Board(CAPTURE_FEN)
    piece_events_for_move(board, chess.Move.from_uci("f3e5"))
    assert board.fen() == CAPTURE_FEN
