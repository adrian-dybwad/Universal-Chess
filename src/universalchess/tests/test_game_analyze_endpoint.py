"""Tests for POST /api/games/<id>/analyze (on-demand gap-fill of a stored game).

Why these tests exist
---------------------
The browser no longer ships an engine, so the review page can only show
evaluations the board has stored. Games played with ``analysis_mode`` off, or
recorded before evaluations were persisted, have an empty eval chart with no way
to populate it. This endpoint asks the board to analyse the missing plies.

How a regression manifests
--------------------------
The board owns the engine and the analysis queue; the web process must not run a
search of its own (it would contend for the same pooled UCI process and the Pi's
limited RAM). So the endpoint must be a pure hand-off over the board-command
channel. A regression that answers success without reaching the board leaves the
user watching a chart that never fills, with no error shown.
"""

import importlib
import sys

import pytest

from universalchess.tests.webapp_fixture import configure_for_testing

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

import universalchess.services.game_broadcast as broadcast  # noqa: E402
from universalchess.db import models  # noqa: E402
from universalchess.managers.game.move_persistence import (  # noqa: E402
    persist_move_and_maybe_create_game,
)


@pytest.fixture
def seeded(monkeypatch):
    """A stored game plus a client, with board commands captured not sent.

    Yields ``(game_id, client, sent)`` where ``sent`` collects the
    ``(command, params)`` pairs the endpoint hands to the board.
    """
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    import universalchess.managers.game.move_persistence as mp
    monkeypatch.setattr(mp, "_get_models", lambda: models)
    monkeypatch.setattr(webapp, "get_db_session", lambda: Session())
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    configure_for_testing(webapp)

    # Every request also pings the board with reset_inactivity (a before_request
    # hook, unrelated to analysis), so only analyze_game commands are recorded.
    sent = []

    def fake_send(command, params=None):
        if command == "analyze_game":
            sent.append((command, params))
        return True

    monkeypatch.setattr(broadcast, "send_board_command", fake_send)

    session = Session()
    try:
        board = chess.Board()
        game_id = -1
        for index, uci in enumerate(["e2e4", "e7e5"]):
            fen_before = board.fen()
            board.push(chess.Move.from_uci(uci))
            game_id, _ = persist_move_and_maybe_create_game(
                session=session,
                is_first_move=(index == 0),
                current_game_db_id=game_id,
                source_file="test",
                game_info={},
                fen_before_move=fen_before,
                move_uci=uci,
                fen_after_move=board.fen(),
                white_clock=None,
                black_clock=None,
            )
    finally:
        session.close()

    return game_id, webapp.app.test_client(), sent


def test_analyze_hands_the_game_id_to_the_board(seeded):
    """The request becomes an analyze_game board command carrying the game id.

    Regression: analysing in the web process would contend with the board for
    the pooled engine process. Dropping the id (or sending the wrong one) fills
    a different game's chart.
    """
    game_id, client, sent = seeded

    resp = client.post(f"/api/games/{game_id}/analyze")

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert sent == [("analyze_game", {"game_id": game_id})]


def test_analyze_reports_503_when_the_board_is_not_running(seeded, monkeypatch):
    """An unreachable board is surfaced, not silently swallowed.

    Regression manifests as a success response and a chart that never fills,
    because nothing is listening on the board-command socket.
    """
    game_id, client, _sent = seeded
    monkeypatch.setattr(broadcast, "send_board_command", lambda *a, **k: False)

    resp = client.post(f"/api/games/{game_id}/analyze")

    assert resp.status_code == 503
    assert resp.get_json()["success"] is False


def test_analyze_of_an_unknown_game_is_404_and_sends_nothing(seeded):
    """A nonexistent game is rejected before the board is asked to do work.

    Regression: forwarding an unknown id makes the board open a session, read
    nothing and log a failure for every stray request.
    """
    _game_id, client, sent = seeded

    resp = client.post("/api/games/999999/analyze")

    assert resp.status_code == 404
    assert sent == []
