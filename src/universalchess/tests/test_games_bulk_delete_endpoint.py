"""Tests for POST /api/games/delete (auth-gated bulk delete of stored games).

The Games page multi-select toolbar deletes many rows in one request after a
single login. These tests guard that contract: unauthenticated callers get 401,
empty/invalid bodies get 400, matching GameMove then Game rows are removed,
and unknown ids are ignored so a mixed batch still succeeds for the rest.
"""

import importlib
import sys

import pytest

from universalchess.tests.webapp_fixture import configure_for_testing

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

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

import datetime  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from universalchess.db import models  # noqa: E402


@pytest.fixture
def env(monkeypatch):
    """Fresh in-memory DB, authenticated client, and helpers to seed/query rows."""
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(webapp, "get_db_session", lambda: Session())
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    configure_for_testing(webapp)

    def add_game_with_move():
        session = Session()
        try:
            game = models.Game(
                white="W",
                black="B",
                result="1-0",
                source="test",
                created_at=datetime.datetime(2026, 7, 10, 1, 0, 0),
            )
            session.add(game)
            session.flush()
            session.add(
                models.GameMove(gameid=game.id, move="e2e4", fen="4k3/8/8/8/4P3/8/8/4K3 w - - 0 1")
            )
            session.commit()
            return game.id
        finally:
            session.close()

    def count_games():
        session = Session()
        try:
            return session.query(models.Game).count()
        finally:
            session.close()

    def count_moves_for(game_id: int):
        session = Session()
        try:
            return session.query(models.GameMove).filter_by(gameid=game_id).count()
        finally:
            session.close()

    return webapp.app.test_client(), add_game_with_move, count_games, count_moves_for, Session


def test_requires_auth(env, monkeypatch):
    # Bulk delete must not run without credentials. How a regression manifests:
    # status 200 and rows gone while verify_webdav_authentication returned False.
    client, add_game_with_move, count_games, _count_moves, _Session = env
    game_id = add_game_with_move()
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))

    resp = client.post("/api/games/delete", json={"ids": [game_id]})

    assert resp.status_code == 401
    assert count_games() == 1


def test_empty_ids_returns_400(env):
    # An empty batch is a client error, not a no-op success. How a regression
    # manifests: 200 with deleted:0, so the UI treats a no-op as success.
    client, _add, _cg, _cm, _Session = env

    resp = client.post("/api/games/delete", json={"ids": []})

    assert resp.status_code == 400


def test_invalid_ids_returns_400(env):
    # Non-positive or non-int ids must be rejected before any delete. How a
    # regression manifests: 500 from SQL, or silent coercion that deletes wrong rows.
    client, add_game_with_move, count_games, _cm, _Session = env
    add_game_with_move()

    for body in (
        {"ids": [0]},
        {"ids": [-1]},
        {"ids": ["abc"]},
        {"ids": [1.5]},
        {},
        {"ids": "1"},
    ):
        resp = client.post("/api/games/delete", json=body)
        assert resp.status_code == 400, body

    assert count_games() == 1


def test_deletes_moves_then_games(env):
    # Matching GameMove rows must go with their games so orphans do not remain.
    # How a regression manifests: games gone but move rows left, or 500 on FK.
    client, add_game_with_move, count_games, count_moves_for, _Session = env
    id_a = add_game_with_move()
    id_b = add_game_with_move()
    kept = add_game_with_move()

    resp = client.post("/api/games/delete", json={"ids": [id_a, id_b]})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"success": True, "deleted": 2}
    assert count_games() == 1
    assert count_moves_for(id_a) == 0
    assert count_moves_for(id_b) == 0
    assert count_moves_for(kept) == 1


def test_unknown_ids_do_not_fail_the_batch(env):
    # A stale id in the selection must not abort deletes of real games. How a
    # regression manifests: 404/500 and zero deletes when one id is missing.
    client, add_game_with_move, count_games, count_moves_for, _Session = env
    real_id = add_game_with_move()

    resp = client.post("/api/games/delete", json={"ids": [real_id, 999999]})

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True, "deleted": 1}
    assert count_games() == 0
    assert count_moves_for(real_id) == 0
