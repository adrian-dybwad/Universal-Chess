"""Tests for the web Lichess lobby endpoints.

GET /api/lichess/ongoing and /challenges list the same rows as the board lobby.
POST /api/lichess/start forwards lichess_start to the board. Auth is required.
The Lichess client is faked so these tests never hit the network.
"""

import importlib
import json
import sys
from types import SimpleNamespace

import pytest

from universalchess.tests.webapp_fixture import configure_for_testing

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

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


@pytest.fixture
def client(monkeypatch):
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


class _FakeGames:
    def get_ongoing(self, count=10):
        return [
            {
                "gameId": "g1",
                "opponent": {"username": "Bob", "rating": 1500},
                "color": "white",
            }
        ]


class _FakeChallenges:
    def get_mine(self):
        return {
            "in": [{"id": "c1", "challenger": {"name": "Ann", "rating": 1400}}],
            "out": [{"id": "c2", "destUser": {"name": "Bo", "rating": 1600}}],
        }


class _FakeConnection:
    """Stands in for LichessConnection, counting the close the endpoint owes."""

    def __init__(self):
        self.client = SimpleNamespace(games=_FakeGames(), challenges=_FakeChallenges())
        self.closes = 0

    def close(self) -> int:
        self.closes += 1
        return 0


_opened_connections = []


def _fake_ok_client(_settings, _log):
    connection = _FakeConnection()
    _opened_connections.append(connection)
    return connection, "alice", None


@pytest.fixture
def lichess_connections(monkeypatch):
    """Serve the lobby endpoints a fake connection and expose the ones opened."""
    _opened_connections.clear()
    monkeypatch.setattr(
        "universalchess.players.lichess.lobby.lichess_connection_from_settings",
        _fake_ok_client,
    )
    return _opened_connections


def test_ongoing_returns_summaries(client, lichess_connections):
    """GET /api/lichess/ongoing must return the board lobby's game rows.

    Why: the web list posts each id as a join. How a regression manifests: the
    payload shape changes, or Lichess is contacted (this fake would not be used).
    """
    resp = client.get("/api/lichess/ongoing")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {
        "games": [
            {"id": "g1", "opponent": "Bob", "rating": 1500, "color": "white"},
        ]
    }


def test_challenges_returns_incoming_then_outgoing(client, lichess_connections):
    """GET /api/lichess/challenges must return IN then OUT summaries.

    Why: selecting a row posts direction+id. Regression: order flips or a field
    is renamed so the web card cannot start the join.
    """
    resp = client.get("/api/lichess/challenges")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {
        "challenges": [
            {"id": "c1", "direction": "in", "name": "Ann", "rating": 1400},
            {"id": "c2", "direction": "out", "name": "Bo", "rating": 1600},
        ]
    }


@pytest.mark.parametrize("endpoint", ["/api/lichess/ongoing", "/api/lichess/challenges"])
def test_lobby_endpoints_close_the_connection_they_opened(
    client, lichess_connections, endpoint
):
    """Each request must release its Lichess connection before responding.

    The lobby cards poll these endpoints, so a connection left to the garbage
    collector means an idle socket to lichess.org per poll. A regression
    manifests as closes staying at 0 while the response still looks correct.
    """
    assert client.get(endpoint).status_code == 200

    assert [connection.closes for connection in lichess_connections] == [1]


def test_a_lichess_failure_still_closes_the_connection(
    client, lichess_connections, monkeypatch
):
    """A 502 must not keep the connection the failed request opened.

    Lichess being unreachable is exactly when polling retries most, so the
    error path is where leaked connections would accumulate fastest. A
    regression manifests as closes staying at 0 on the failure path.
    """

    def _unreachable(_self, count=10):
        raise RuntimeError("lichess is down")

    monkeypatch.setattr(_FakeGames, "get_ongoing", _unreachable)

    resp = client.get("/api/lichess/ongoing")

    assert resp.status_code == 502
    assert json.loads(resp.data)["error"] == "fetch_failed"
    assert [connection.closes for connection in lichess_connections] == [1]


def test_ongoing_no_token_is_409(client, monkeypatch):
    """No credential must be 409 with error no_token, not an empty success.

    Why: an empty games list looks like "this account has none" and hides that
    Accounts still needs a token. How a regression manifests: 200 with games [].
    """
    monkeypatch.setattr(
        "universalchess.players.lichess.lobby.lichess_connection_from_settings",
        lambda _s, _l: (None, None, "no_token"),
    )
    resp = client.get("/api/lichess/ongoing")
    assert resp.status_code == 409
    body = json.loads(resp.data)
    assert body["error"] == "no_token"
    assert body["games"] == []


def test_start_new_forwards_lichess_start(client, monkeypatch):
    """POST /api/lichess/start mode=new must send lichess_start to the board.

    Why: New Game on the web card must take the lobby path, not /api/board/new-game
    (that ignores ongoing/challenge ids). Regression: wrong command name or mode.
    """
    sent = []
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: (
            sent.append((command, params)) if command != "reset_inactivity" else None
        )
        or True,
    )
    resp = client.post(
        "/api/lichess/start",
        data=json.dumps({"mode": "new"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    assert sent == [
        (
            "lichess_start",
            {
                "mode": "new",
                "game_id": "",
                "challenge_id": "",
                "challenge_direction": "in",
            },
        )
    ]


def test_start_ongoing_forwards_game_id(client, monkeypatch):
    """Ongoing start must include the selected game id in the board command.

    How a regression manifests: game_id dropped, so the board seeks instead.
    """
    sent = []
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: (
            sent.append((command, params)) if command != "reset_inactivity" else None
        )
        or True,
    )
    resp = client.post(
        "/api/lichess/start",
        data=json.dumps({"mode": "ongoing", "game_id": "g1"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert sent[0][1]["mode"] == "ongoing"
    assert sent[0][1]["game_id"] == "g1"


def test_start_ongoing_without_game_id_is_400(client, monkeypatch):
    """Ongoing without a game id must not reach the board.

    Why: an empty game_id would seek. How a regression manifests: 200 and a
    lichess_start command with game_id "".
    """
    sent = []
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: (
            sent.append((command, params)) if command != "reset_inactivity" else None
        )
        or True,
    )
    resp = client.post(
        "/api/lichess/start",
        data=json.dumps({"mode": "ongoing"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert sent == []


def test_start_reports_board_not_running(client, monkeypatch):
    """When the board is not listening, start must report 503.

    send_board_command returns False if the main process isn't running; the UI
    needs a distinct failure so it does not claim the seek started.
    """
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: False,
    )
    resp = client.post(
        "/api/lichess/start",
        data=json.dumps({"mode": "new"}),
        content_type="application/json",
    )
    assert resp.status_code == 503
    assert json.loads(resp.data)["success"] is False


def test_lichess_lobby_endpoints_require_auth(monkeypatch):
    """Unauthenticated lobby GETs and start must be 401.

    These talk to Lichess with the stored token and start games on the board.
    """
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()
    assert unauth.get("/api/lichess/ongoing").status_code == 401
    assert unauth.get("/api/lichess/challenges").status_code == 401
    assert unauth.post(
        "/api/lichess/start",
        data=json.dumps({"mode": "new"}),
        content_type="application/json",
    ).status_code == 401
