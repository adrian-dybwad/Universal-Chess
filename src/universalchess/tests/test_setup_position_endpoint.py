"""Tests for POST /api/board/setup-position, focused on the ``record`` flag.

The endpoint forwards a validated FEN to the board via ``send_board_command``.
"Play Game from here" needs the resulting game recorded, while predefined-position
setups stay unrecorded practice games. These tests pin that the ``record`` flag is
forwarded (true when asked, falsey by default) so the two behaviors do not merge.
"""

import importlib
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("chess")

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

import universalchess.services.game_broadcast as game_broadcast  # noqa: E402

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


@pytest.fixture
def client(monkeypatch):
    """Test client with auth stubbed to pass and the board command captured.

    send_board_command is patched to record the (command, params) it receives and
    return True, so the test reads back exactly what the endpoint forwarded
    without needing a running board.
    """
    captured = {}

    def fake_send(command, params):
        captured["command"] = command
        captured["params"] = params
        return True

    monkeypatch.setattr(game_broadcast, "send_board_command", fake_send)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    webapp.app.config.update(TESTING=True)
    yield webapp.app.test_client(), captured


def test_record_flag_forwarded_when_requested(client):
    # "Play Game from here" sends record=true; the board must receive it so the
    # game is saved to history. Regression: dropping the flag would silently make
    # the game an unrecorded practice game.
    test_client, captured = client
    resp = test_client.post(
        "/api/board/setup-position",
        json={"fen": START_FEN, "name": "Analysis", "record": True},
    )

    assert resp.status_code == 200
    assert captured["command"] == "setup_position"
    assert captured["params"]["record"] is True
    assert captured["params"]["fen"] == START_FEN


def test_record_defaults_false_for_predefined_setup(client):
    # A predefined-position setup omits record; the board must receive a falsey
    # record so it stays an unrecorded practice game. Regression: defaulting to
    # True would start recording every position setup.
    test_client, captured = client
    resp = test_client.post(
        "/api/board/setup-position",
        json={"fen": START_FEN, "name": "Ruy Lopez"},
    )

    assert resp.status_code == 200
    assert captured["params"]["record"] is False


def test_history_forwarded_for_play_from_here(client):
    # "Play Game from here" past the opening transfers the reviewed game's moves so
    # the live game keeps the full PGN. The endpoint must forward the history plus
    # the board it replays on (start_fen/chess960) and the transferred players.
    # Regression: dropping any of these would make the board start cold from `fen`
    # with no history, or replay the moves on the wrong board.
    test_client, captured = client
    resp = test_client.post(
        "/api/board/setup-position",
        json={
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "name": "Analysis",
            "record": True,
            "moves": ["e2e4"],
            "start_fen": START_FEN,
            "white": "Alice",
            "black": "Bob",
        },
    )

    assert resp.status_code == 200
    params = captured["params"]
    assert params["moves"] == ["e2e4"]
    assert params["start_fen"] == START_FEN
    assert params["chess960"] is False
    assert params["white"] == "Alice"
    assert params["black"] == "Bob"
    assert params["record"] is True


def test_empty_moves_stays_plain_setup(client):
    # At the opening ply there is no history to transfer; an empty moves list must
    # not add history params so the board does the plain `fen` setup. Regression:
    # forwarding an empty history would push the board down the play-from-history
    # path with nothing to seed the game.
    test_client, captured = client
    resp = test_client.post(
        "/api/board/setup-position",
        json={"fen": START_FEN, "record": True, "moves": []},
    )

    assert resp.status_code == 200
    assert "moves" not in captured["params"]


def test_non_list_moves_rejected(client):
    # `moves` must be a list of UCI-shaped strings; a malformed payload is rejected
    # before touching the board. Regression: forwarding arbitrary data would push
    # unvalidated input toward the board's game builder.
    test_client, captured = client
    resp = test_client.post(
        "/api/board/setup-position",
        json={"fen": START_FEN, "record": True, "moves": "e2e4"},
    )

    assert resp.status_code == 400
    assert "command" not in captured


def test_requires_authentication(client, monkeypatch):
    # The setup command mutates board state, so it must stay auth-gated.
    test_client, captured = client
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))

    resp = test_client.post(
        "/api/board/setup-position",
        json={"fen": START_FEN, "record": True},
    )

    assert resp.status_code == 401
    assert "command" not in captured
