"""Tests for the starting position of an exported PGN.

``build_chess_game_from_id`` used to build every game from a bare
``chess.pgn.Game()`` -- the standard opening -- and never read the game record's
``start_fen`` or ``chess960`` columns. Because ``add_variation`` does not
validate moves against the board, this failed silently: a Chess960 or
"play from here" game exported as a legal-looking movetext whose SAN describes
an entirely different game.

The positions endpoint (``/api/games/<id>/positions``) already replays from
``start_fen`` with the ``chess960`` flag, which is why the web move list was
correct while the PGN for the same game was not.
"""

import io

import importlib
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

import chess
import chess.pgn
from PIL import Image

import universalchess.db.uri as _uri  # noqa: E402

_uri.get_database_uri = lambda: "sqlite:///:memory:"
_orig_image_open = Image.open
Image.open = lambda *a, **k: Image.new("RGBA", (8, 8))
try:
    if "universalchess.web.app" in sys.modules:
        webapp = importlib.reload(sys.modules["universalchess.web.app"])
    else:
        import universalchess.web.app as webapp  # noqa: E402
finally:
    Image.open = _orig_image_open


# A Chess960-style position whose only legal castling is king c1 onto rook h1,
# encoded in UCI as "c1h1". On the *standard* starting position that string is
# not a legal move at all, so replaying it from the wrong root cannot succeed by
# accident -- which is what makes it a decisive fixture.
CHESS960_START = "2k4r/pppppppp/8/8/8/8/PPPPPPPP/2K4R w Kk - 0 1"
CHESS960_CASTLE_UCI = "c1h1"

# A mid-game position, as produced by "Play Game from here" after 1.e4 e5.
PLAY_FROM_HERE_START = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


@pytest.fixture
def session(private_db_session):
    """A database of this test's own (see the conftest fixture for why)."""
    return private_db_session


def _seed_game(session, *, moves_uci, start_fen=None, chess960=False):
    """Create a game whose move rows replay from ``start_fen``."""
    from universalchess.db import models

    game = models.Game(source="local.py", white="W", black="B", result="*",
                       start_fen=start_fen, chess960=chess960)
    session.add(game)
    session.flush()

    board = chess.Board(start_fen or chess.STARTING_FEN, chess960=chess960)
    session.add(models.GameMove(gameid=game.id, move="", fen=board.fen()))
    for uci in moves_uci:
        board.push(chess.Move.from_uci(uci))
        session.add(models.GameMove(gameid=game.id, move=uci, fen=board.fen()))
    session.commit()
    return game.id


def _mainline_uci(game):
    return [node.move.uci() for node in game.mainline()]


# ---------------------------------------------------------------------------
# Chess960
# ---------------------------------------------------------------------------


def test_chess960_game_declares_its_start_position_and_variant(session):
    """A Chess960 game emits [FEN], [SetUp "1"] and [Variant "Chess960"].

    Why: without them a reader starts from the standard opening and interprets
    the castling move under standard rules, so the game cannot be replayed. The
    Variant tag is what tells the reader to use king-onto-rook castling.

    How a regression manifests: the headers are absent, and the movetext is
    silently reinterpreted against the wrong position.
    """
    gid = _seed_game(session, moves_uci=[CHESS960_CASTLE_UCI],
                     start_fen=CHESS960_START, chess960=True)

    game = webapp.build_chess_game_from_id(session, gid)

    assert game.headers["FEN"] == CHESS960_START
    assert game.headers["SetUp"] == "1"
    assert game.headers["Variant"] == "Chess960"


def test_chess960_castling_survives_a_full_export_and_reparse(session):
    """A 960 castling move exports as O-O and reads back as the same UCI.

    Why: this is the end-to-end proof that the game is rooted correctly. The
    king-onto-rook encoding "c1h1" is only meaningful on the stored start
    position with the chess960 flag set.

    How a regression manifests: replaying from the standard opening builds a
    tree whose SAN is nonsense, and the reparse either raises "illegal san" or
    yields a shorter mainline than was stored.
    """
    gid = _seed_game(session, moves_uci=[CHESS960_CASTLE_UCI],
                     start_fen=CHESS960_START, chess960=True)

    text = webapp.generate_pgn_string(gid)
    reparsed = chess.pgn.read_game(io.StringIO(text))

    assert "O-O" in text
    assert reparsed.board().chess960 is True
    assert _mainline_uci(reparsed) == [CHESS960_CASTLE_UCI]
    assert reparsed.end().board().fen() == (
        "2k4r/pppppppp/8/8/8/8/PPPPPPPP/5RK1 b k - 1 1"
    )


# ---------------------------------------------------------------------------
# Play from here
# ---------------------------------------------------------------------------


def test_play_from_here_game_declares_its_start_without_a_variant_tag(session):
    """A non-standard start in standard chess emits [FEN]/[SetUp] but no [Variant].

    Why: "Play Game from here" stores a mid-game start_fen with chess960 false.
    Tagging it as a variant would make readers apply Chess960 castling rules to
    an ordinary game.

    How a regression manifests: either the position is missing (the game replays
    from the standard opening, so the move numbers and SAN are wrong) or the
    game is falsely labelled Chess960.
    """
    gid = _seed_game(session, moves_uci=["g1f3"], start_fen=PLAY_FROM_HERE_START)

    game = webapp.build_chess_game_from_id(session, gid)

    assert game.headers["FEN"] == PLAY_FROM_HERE_START
    assert game.headers["SetUp"] == "1"
    assert "Variant" not in game.headers


def test_standard_game_emits_no_start_position_headers(session):
    """A game from the standard opening carries neither [FEN] nor [SetUp].

    Why: start_fen is NULL for the overwhelming majority of games, and the PGN
    standard says the tags are only present for a non-standard start. This
    guards the common case against the fix for the uncommon one.

    How a regression manifests: every ordinary game gains a redundant [FEN] of
    the standard opening, which some importers treat as a variant game.
    """
    gid = _seed_game(session, moves_uci=["e2e4", "e7e5"])

    game = webapp.build_chess_game_from_id(session, gid)

    assert "FEN" not in game.headers
    assert "SetUp" not in game.headers
    assert _mainline_uci(game) == ["e2e4", "e7e5"]


# ---------------------------------------------------------------------------
# Unreplayable data
# ---------------------------------------------------------------------------


def test_a_move_illegal_in_its_position_truncates_rather_than_corrupts(session):
    """Replay stops at the first move that is illegal in the position reached.

    Why: add_variation does not validate, so appending an illegal move produces
    a tree whose every later SAN is computed from a position that never
    occurred. A short but truthful game is recoverable; a long wrong one is not.

    How a regression manifests: the mainline contains all three moves and the
    exported SAN describes a game that was never played.
    """
    from universalchess.db import models

    gid = _seed_game(session, moves_uci=["e2e4", "e7e5"])
    # e4e5 is not legal after 1.e4 e5 -- the e5 square is occupied by a pawn
    # that is defended, and the e4 pawn cannot advance onto it.
    session.add(models.GameMove(gameid=gid, move="e4e5", fen=""))
    session.commit()

    game = webapp.build_chess_game_from_id(session, gid)

    assert _mainline_uci(game) == ["e2e4", "e7e5"]


def test_an_unparseable_move_string_does_not_abort_the_export(session):
    """A malformed UCI string is skipped, leaving the rest of the game intact.

    Why: the existing exporter already tolerates this, and a single bad row must
    not turn the whole game into a 404. How a regression manifests: replacing
    the tolerant parse with a strict one makes generate_pgn_string return None,
    so the game disappears from the web UI entirely.
    """
    from universalchess.db import models

    gid = _seed_game(session, moves_uci=["e2e4"])
    session.add(models.GameMove(gameid=gid, move="zzzz", fen=""))
    session.commit()

    assert webapp.build_chess_game_from_id(session, gid) is not None
    assert _mainline_uci(webapp.build_chess_game_from_id(session, gid)) == ["e2e4"]
