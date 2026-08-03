"""Tests for GET /api/games/<id>/positions (authoritative per-ply positions).

Why these tests exist
---------------------
The web analysis view navigates a saved game's history by these server-computed
FENs instead of replaying the PGN in the browser, because chess.js mis-computes
Chess960 castling. The endpoint must therefore:

1. For a Chess960 game, return the true per-ply FENs (king-onto-rook castling
   lands the king on the correct square) with SAN rendered as O-O/O-O-O and the
   variant flag set -- the regression being that a standard-board reconstruction
   would reject or mis-place the 960 castle.
2. For a standard game, return chess960=False and ordinary SAN, so the common
   case is unaffected and the browser keeps its PGN-replay path.
3. Return 404 for an unknown game rather than a fabricated position.
"""

import importlib
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")
pytest.importorskip("chess")

import chess  # noqa: E402
from PIL import Image  # noqa: E402

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

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from universalchess.db import models  # noqa: E402
from universalchess.managers.game.move_persistence import (  # noqa: E402
    persist_move_and_maybe_create_game,
)
from universalchess.state.chess960 import chess960_fen  # noqa: E402

# 960 start with non-corner rooks (BBQNNRKR) so castling is genuinely 960.
FRC_START = chess960_fen(0)


@pytest.fixture
def seeded(monkeypatch):
    """A fresh in-memory DB the endpoint reads via a patched get_db_session.

    Returns a helper to persist a game's moves and a factory that yields a new
    session bound to the same engine each call (the endpoint closes the session
    it gets, so each request needs its own).
    """
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # persist_move_and_maybe_create_game resolves models via its module global.
    import universalchess.managers.game.move_persistence as mp

    monkeypatch.setattr(mp, "_get_models", lambda: models)
    # The endpoint calls get_db_session(); hand it a session on our engine.
    monkeypatch.setattr(webapp, "get_db_session", lambda: Session())
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    webapp.app.config.update(TESTING=True)

    def persist_game(*, chess960: bool, start_fen: str, move_ucis):
        session = Session()
        try:
            board = chess.Board(start_fen, chess960=chess960)
            game_db_id = -1
            first = True
            fen_before = board.fen()
            for uci in move_ucis:
                move = chess.Move.from_uci(uci)
                fen_before = board.fen()
                board.push(move)
                game_db_id, _ = persist_move_and_maybe_create_game(
                    session=session,
                    is_first_move=first,
                    current_game_db_id=game_db_id,
                    source_file="test",
                    game_info={},
                    fen_before_move=fen_before,
                    move_uci=uci,
                    fen_after_move=board.fen(),
                    white_clock=None,
                    black_clock=None,
                    chess960=chess960,
                )
                first = False
            return game_db_id
        finally:
            session.close()

    return persist_game, webapp.app.test_client()


def test_positions_960_castle_has_true_fen_and_san(seeded):
    # Build a 960 game that includes a king-onto-rook castle, then assert the
    # endpoint returns the authoritative post-castle FEN and O-O SAN. Regression:
    # a standard-board reconstruction would reject f1h1 (king-onto-rook) and the
    # castle row would be missing or mis-placed.
    persist_game, client = seeded
    # A cleared-rank 960 position where the king (f1) castles with the h1 rook.
    start = "4k3/8/8/8/8/8/8/5K1R w K - 0 1"
    game_id = persist_game(chess960=True, start_fen=start, move_ucis=["f1h1"])

    resp = client.get(f"/api/games/{game_id}/positions")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["chess960"] is True
    positions = body["positions"]
    assert len(positions) == 2
    assert positions[0]["uci"] is None
    assert positions[1]["uci"] == "f1h1"
    assert positions[1]["san"] == "O-O"
    # King on g1, rook on f1 -- the true 960 kingside castle result.
    assert positions[1]["fen"].startswith("4k3/8/8/8/8/8/8/5RK1")


def test_positions_standard_game_reports_flag_off(seeded):
    # A standard game must report chess960=False with ordinary SAN so the browser
    # keeps its unchanged PGN-replay path. Regression: defaulting the flag on would
    # push standard games onto the authoritative path needlessly.
    persist_game, client = seeded
    game_id = persist_game(
        chess960=False, start_fen=chess.STARTING_FEN, move_ucis=["e2e4", "e7e5"]
    )

    resp = client.get(f"/api/games/{game_id}/positions")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["chess960"] is False
    assert [p["san"] for p in body["positions"][1:]] == ["e4", "e5"]


def test_positions_unknown_game_is_404(seeded):
    # An unknown game id must be a clean 404, not a fabricated empty position list.
    _persist_game, client = seeded
    resp = client.get("/api/games/999999/positions")
    assert resp.status_code == 404
