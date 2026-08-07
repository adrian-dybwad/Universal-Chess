"""Tests for GET /api/games (full game summary list for the Games side-nav).

The Games page groups games by month in a side-nav, so it needs every game's
summary in one response, newest first. These tests guard that contract: all
games are returned (not a 10-row page), ordered newest-first, with the summary
fields the client buckets and renders; and an empty database yields an empty
list rather than an error.
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
def client(monkeypatch):
    """Test client backed by a fresh in-memory DB and a factory to seed games."""
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(webapp, "get_db_session", lambda: Session())
    configure_for_testing(webapp)

    def add_game(*, white, black, result, source="test", created_at=None):
        session = Session()
        try:
            game = models.Game(
                white=white,
                black=black,
                result=result,
                source=source,
                created_at=created_at or datetime.datetime(2026, 7, 10, 1, 0, 0),
            )
            session.add(game)
            session.commit()
            return game.id
        finally:
            session.close()

    return webapp.app.test_client(), add_game


def test_returns_all_games_newest_first(client):
    # Seed more than one page (10) so a regression to paged behaviour is visible,
    # and assert descending id order. How a regression manifests: only 10 rows
    # come back, or they arrive oldest-first, breaking the sidebar's newest-first
    # month ordering.
    test_client, add_game = client
    ids = [add_game(white="W", black="B", result="1-0") for _ in range(12)]

    resp = test_client.get("/api/games")

    assert resp.status_code == 200
    body = resp.get_json()
    returned_ids = [int(g["id"]) for g in body["games"]]
    assert len(returned_ids) == 12
    assert returned_ids == sorted(ids, reverse=True)


def test_includes_summary_fields(client):
    # The client buckets by created_at and renders players/result, so those
    # fields must be present with the stored values. Regression: a dropped field
    # would blank the card or make the row unbucketable.
    test_client, add_game = client
    add_game(
        white="Alice",
        black="Bob",
        result="1-0",
        created_at=datetime.datetime(2026, 6, 15, 12, 0, 0),
    )

    body = test_client.get("/api/games").get_json()
    game = body["games"][0]
    assert game["white"] == "Alice"
    assert game["black"] == "Bob"
    assert game["result"] == "1-0"
    # created_at is serialized as explicit-UTC ISO so the browser renders local time.
    assert game["created_at"].startswith("2026-06-15T12:00:00")


def test_empty_database_returns_empty_list(client):
    # A board with no games must yield an empty array, not a 404/500. Regression:
    # an unguarded access on empty results would error instead of returning [].
    test_client, _add_game = client
    body = test_client.get("/api/games").get_json()
    assert body == {"games": []}


def test_status_derived_from_result(client):
    # The Games screen gates its Resume action on `status`, so each game's result
    # must map to the right lifecycle: NULL -> in_progress, "*" -> abandoned, and
    # a PGN code -> finished. Regression: a mis-mapped status would either hide
    # Resume on a resumable game or offer it on a finished one. Seeded newest-last
    # so the newest-first response orders them finished, abandoned, in_progress.
    test_client, add_game = client
    add_game(white="W", black="B", result=None)   # in progress
    add_game(white="W", black="B", result="*")     # abandoned
    add_game(white="W", black="B", result="1-0")   # finished

    games = test_client.get("/api/games").get_json()["games"]
    statuses = [g["status"] for g in games]
    assert statuses == ["finished", "abandoned", "in_progress"]


def test_null_result_serialized_as_json_null(client):
    # An in-progress game has a NULL result. It must serialize as JSON null, not
    # the string "None": the client treats result as `string | null` and only
    # renders a result badge for finished games. Regression: stringifying NULL to
    # "None" (the prior bug) made every in-progress game show a bogus "None"
    # badge and be indistinguishable from a real result client-side.
    test_client, add_game = client
    add_game(white="W", black="B", result=None)

    game = test_client.get("/api/games").get_json()["games"][0]
    assert game["result"] is None
    assert game["status"] == "in_progress"
