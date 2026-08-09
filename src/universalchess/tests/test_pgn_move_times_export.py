"""Tests for per-move time data in the exported PGN.

``/getpgn/<id>`` and the WebDAV PGN view both render through
``build_chess_game_from_id``. These pin the time-related output: the standard
``[TimeControl]`` tag from the game record, ``[%emt]`` from the measured move
duration, and ``[%clk]`` from the stored clock remainders.

The clock columns hold 0 for an untimed game (the clock is seeded to zero and
never runs), so ``[%clk]`` must be gated on the game actually having a time
control -- otherwise a casual game exports as though both players had flagged
on every move.
"""

import datetime
import importlib
import io
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


# (uci, white_clock, black_clock, duration_ms) for a short Scholar's-mate opening.
_TIMED_MOVES = [
    ("e2e4", 296, 300, 4000),
    ("e7e5", 296, 291, 9000),
    ("g1f3", 288, 291, 8000),
]


@pytest.fixture
def session(private_db_session):
    """A database of this test's own (see the conftest fixture for why)."""
    return private_db_session


def _seed_game(session, *, moves, time_control):
    """Create a game with an initial-position row plus the given moves."""
    from universalchess.db import models

    game = models.Game(
        source="local.py", white="W", black="B", result="*",
        created_at=datetime.datetime(2026, 8, 8, 12, 0, 0),
        time_control=time_control,
    )
    session.add(game)
    session.flush()

    session.add(models.GameMove(gameid=game.id, move="", fen=chess.STARTING_FEN))
    board = chess.Board()
    for uci, white_clock, black_clock, duration_ms in moves:
        board.push(chess.Move.from_uci(uci))
        session.add(models.GameMove(
            gameid=game.id, move=uci, fen=board.fen(),
            white_clock=white_clock, black_clock=black_clock,
            move_duration_ms=duration_ms,
        ))
    session.commit()
    return game.id


def _nodes(game):
    """The mainline nodes of a chess.pgn.Game, in order."""
    nodes, node = [], game
    while node.variations:
        node = node.variations[0]
        nodes.append(node)
    return nodes


# ---------------------------------------------------------------------------
# [TimeControl] header
# ---------------------------------------------------------------------------


def test_time_control_header_comes_from_the_game_record(session):
    """The stored PGN-format control is emitted as the [TimeControl] tag.

    Why: the per-move [%clk] values are uninterpretable without knowing the
    control they count down from -- 296 seconds remaining means something
    different in a 5-minute game than in a 90-minute one.

    How a regression manifests: the tag is absent, so a reader cannot tell
    whether a player was in time trouble.
    """
    gid = _seed_game(session, moves=_TIMED_MOVES, time_control="300+5")

    game = webapp.build_chess_game_from_id(session, gid)

    assert game.headers["TimeControl"] == "300+5"


def test_untimed_game_omits_the_time_control_header(session):
    """A game with no stored control emits no [TimeControl] tag.

    Why: every game recorded before this column existed has NULL there, and the
    PGN standard's placeholder for "unknown" is a distinct thing from claiming
    no control was in use.

    How a regression manifests: historical games export as [TimeControl "None"]
    or [TimeControl ""], neither of which is a valid tag value.
    """
    gid = _seed_game(session, moves=[("e2e4", 0, 0, 4000)], time_control=None)

    game = webapp.build_chess_game_from_id(session, gid)

    assert "TimeControl" not in game.headers


# ---------------------------------------------------------------------------
# [%emt] elapsed move time
# ---------------------------------------------------------------------------


def test_every_measured_move_carries_its_elapsed_time(session):
    """Each move's stored duration is emitted as [%emt] on that move.

    Why: this is the feature. Asserting the full sequence rather than a single
    move catches an off-by-one that shifts durations onto the neighbouring ply.

    How a regression manifests: reading the duration column with the wrong row
    offset attributes White's long think to Black's reply.
    """
    gid = _seed_game(session, moves=_TIMED_MOVES, time_control="300+5")

    game = webapp.build_chess_game_from_id(session, gid)

    assert [node.emt() for node in _nodes(game)] == [4, 9, 8]


def test_unmeasured_moves_carry_no_elapsed_time(session):
    """A NULL duration produces no [%emt] rather than a zero.

    Why: every move recorded before this feature has NULL. How a regression
    manifests: an entire back catalogue of games exports claiming every move was
    played instantly, which is indistinguishable from a real bullet game.
    """
    moves = [("e2e4", 296, 300, None), ("e7e5", 296, 291, 9000)]
    gid = _seed_game(session, moves=moves, time_control="300+5")

    game = webapp.build_chess_game_from_id(session, gid)

    assert [node.emt() for node in _nodes(game)] == [None, 9]


# ---------------------------------------------------------------------------
# [%clk] remaining clock
# ---------------------------------------------------------------------------


def test_clock_annotation_uses_the_moving_side_remainder(session):
    """[%clk] after a move is that mover's remaining time, not the opponent's.

    Why: each row stores both sides' clocks, so the exporter has to pick by the
    colour that played the move. The supplement defines [%clk] as "the time
    displayed on the player's clock" for the move it comments.

    How a regression manifests: reading white_clock for every ply produces
    296, 296, 288 -- a clock that only ever belongs to White, so Black's time
    usage vanishes and White's appears to tick during Black's turn.
    """
    gid = _seed_game(session, moves=_TIMED_MOVES, time_control="300+5")

    game = webapp.build_chess_game_from_id(session, gid)

    # e2e4 (White), e7e5 (Black), g1f3 (White).
    assert [node.clock() for node in _nodes(game)] == [296, 291, 288]


def test_untimed_game_emits_no_clock_annotation(session):
    """An untimed game gets no [%clk], despite the columns holding 0.

    Why: the clock service seeds an untimed control to zero seconds and never
    runs, so white_clock/black_clock are a literal 0 rather than NULL. Only the
    game's time control distinguishes "untimed" from "flagged".

    How a regression manifests: every move of a casual over-the-board game
    exports as [%clk 0:00:00], which reads as both players out of time from
    move one.
    """
    moves = [("e2e4", 0, 0, 4000), ("e7e5", 0, 0, 9000)]
    gid = _seed_game(session, moves=moves, time_control=None)

    game = webapp.build_chess_game_from_id(session, gid)

    nodes = _nodes(game)
    assert [node.clock() for node in nodes] == [None, None]
    # The duration must still be exported -- it does not depend on a clock.
    assert [node.emt() for node in nodes] == [4, 9]


# ---------------------------------------------------------------------------
# Rendered output
# ---------------------------------------------------------------------------


def test_generated_pgn_text_is_reparseable_with_its_time_data(session):
    """The served PGN string carries the annotations through to a reader.

    Why: generate_pgn_string builds its own StringExporter, so it can silently
    drop comments even when the game tree holds them. This is what the HTTP
    endpoint actually returns.

    How a regression manifests: an exporter constructed with comments=False
    yields text whose reparsed clock and emt are both None, while the
    node-level tests above still pass.
    """
    gid = _seed_game(session, moves=_TIMED_MOVES, time_control="300+5")

    text = webapp.generate_pgn_string(gid)

    assert "[%clk 0:04:56]" in text
    assert "[%emt 0:00:04]" in text

    reparsed = chess.pgn.read_game(io.StringIO(text))
    assert reparsed.headers["TimeControl"] == "300+5"
    assert [node.emt() for node in _nodes(reparsed)] == [4, 9, 8]
    assert [node.clock() for node in _nodes(reparsed)] == [296, 291, 288]


def test_move_text_remains_valid_san_alongside_the_comments(session):
    """Adding comments must not disturb the movetext itself.

    Why: comments are interleaved into the same movetext stream, and a malformed
    comment swallows the moves after it. How a regression manifests: the
    reparsed game has fewer moves than were stored, or the SAN is corrupted.
    """
    gid = _seed_game(session, moves=_TIMED_MOVES, time_control="300+5")

    reparsed = chess.pgn.read_game(io.StringIO(webapp.generate_pgn_string(gid)))

    assert [node.move.uci() for node in _nodes(reparsed)] == ["e2e4", "e7e5", "g1f3"]
    assert reparsed.end().board().fen() == (
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
    )
