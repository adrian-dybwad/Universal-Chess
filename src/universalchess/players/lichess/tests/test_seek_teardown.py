"""BACK on "Waiting for game" must remove the seek from the Lichess lobby.

Why these tests exist
---------------------
Lichess keeps a public seek listed until the streamed ``POST /api/board/seek``
connection closes. ``LichessPlayer.stop()`` used to set a flag, join the seek
thread, and call ``requests.Session.close()``. None of that closes a connection
that is checked out and blocked in a read, so the seek survived BACK and the
board's seek stayed in the lobby -- twice reported from the board.

The earlier attempt was checked with a ``MagicMock`` session, which cannot tell
"socket closed" from "method called". These tests drive the real berserk
``board.seek`` against a loopback server that reports when its peer closes, so
the assertion is the thing the user observes: the seek connection is gone.
"""

import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("requests")
berserk = pytest.importorskip("berserk")

from universalchess.players.lichess.http_session import (  # noqa: E402
    LichessConnection,
    abortable_token_session_class,
)
from universalchess.players.lichess.player import (  # noqa: E402
    LichessPlayer,
    LichessPlayerConfig,
)
from universalchess.players.base import PlayerState  # noqa: E402

# Measured teardown is ~3 ms; generous enough for a loaded CI box.
TEARDOWN_DEADLINE_SECONDS = 3.0
SEEK_MINUTES = 10
SEEK_INCREMENT = 5


class _LobbyHandler(BaseHTTPRequestHandler):
    """A ``POST /api/board/seek`` that sends headers and then waits, like Lichess."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's required name
        body_length = int(self.headers.get("Content-Length") or 0)
        if body_length:
            self.server.seek_body = self.rfile.read(body_length).decode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()
        self.server.seek_open.set()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self.connection], [], [], 0.02)
            if not readable:
                continue
            try:
                received = self.connection.recv(1)
            except OSError:
                received = b""
            if received == b"":
                self.server.seek_closed.set()
                return

    def log_message(self, *args):
        """Silence the default stderr request log."""


@pytest.fixture
def lobby_server():
    """Loopback stand-in for the Lichess lobby endpoint."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LobbyHandler)
    server.seek_open = threading.Event()
    server.seek_closed = threading.Event()
    server.seek_body = ""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=TEARDOWN_DEADLINE_SECONDS)


def _player_seeking_against(
    lobby_server, color_preference: str = "random"
) -> tuple[LichessPlayer, threading.Thread]:
    """A player whose real seek thread is blocked on a live seek connection.

    Uses the production client factory's session class and the player's own
    ``_seek_game_thread``, so the connection under test is the one a game
    actually opens.
    """
    player = LichessPlayer(
        LichessPlayerConfig(
            name="Lichess",
            time_minutes=SEEK_MINUTES,
            increment_seconds=SEEK_INCREMENT,
            color_preference=color_preference,
        )
    )
    session = abortable_token_session_class()("token")
    player._connection = LichessConnection(
        client=berserk.Client(session=session, base_url=lobby_server.url),
        session=session,
    )
    player._client = player._connection.client

    seek_thread = threading.Thread(target=player._seek_game_thread, daemon=True)
    player._seek_thread = seek_thread
    seek_thread.start()
    assert lobby_server.seek_open.wait(TEARDOWN_DEADLINE_SECONDS), (
        "the seek was never posted"
    )
    # Let the seek thread settle into its blocking read, so stop() is exercised
    # against the state BACK actually finds.
    time.sleep(0.1)
    return player, seek_thread


def test_stop_closes_the_seek_connection_so_the_lobby_drops_the_seek(lobby_server):
    """The reported bug: after BACK the seek must not remain on Lichess.

    A regression manifests as ``seek_closed`` never being set: the POST stays
    open, so Lichess keeps advertising the seek and an opponent can still take
    a game the board has already left.
    """
    player, seek_thread = _player_seeking_against(lobby_server)
    assert not lobby_server.seek_closed.is_set()

    player.stop()

    assert lobby_server.seek_closed.wait(TEARDOWN_DEADLINE_SECONDS), (
        "stop() left the seek connection open, so the seek stays in the lobby"
    )
    assert not seek_thread.is_alive(), "stop() returned with the seek thread blocked"


def test_stop_does_not_report_a_seek_failure_when_it_aborts_the_seek(lobby_server):
    """A deliberate teardown must not surface as an error on the board.

    Aborting the socket makes ``board.seek`` raise inside the seek thread. That
    is expected, so it must not set ERROR (which would paint a seek failure over
    the menu the user is returning to). A regression manifests as ERROR state.
    """
    player, _ = _player_seeking_against(lobby_server)

    player.stop()

    assert player.state is PlayerState.STOPPED
    assert player.error_message in (None, "")


def test_stop_is_safe_when_the_seek_already_ended(lobby_server):
    """stop() runs on BACK and again during game teardown; the second is a no-op.

    A regression manifests as an exception escaping the second stop(), which
    would abandon the rest of the player teardown.
    """
    player, seek_thread = _player_seeking_against(lobby_server)
    player.stop()
    assert lobby_server.seek_closed.wait(TEARDOWN_DEADLINE_SECONDS)
    seek_thread.join(timeout=TEARDOWN_DEADLINE_SECONDS)

    player.stop()

    assert player.state is PlayerState.STOPPED


def test_the_seek_posted_matches_the_configured_clock(lobby_server):
    """Guards the payload the abort machinery is wrapped around.

    If the seek stopped carrying the configured clock, the teardown tests would
    still pass while the board posted the wrong game. A regression manifests as
    a body without this clock.
    """
    player, _ = _player_seeking_against(lobby_server)
    try:
        assert f"time={SEEK_MINUTES}" in lobby_server.seek_body
        assert f"increment={SEEK_INCREMENT}" in lobby_server.seek_body
    finally:
        player.stop()


@pytest.mark.parametrize("color_preference", ["random", "white", "black"])
def test_the_seek_states_its_color_rather_than_omitting_it(
    lobby_server, color_preference
):
    """Every seek must name its color on the wire, including a random one.

    A random seek passed ``color=None``, and ``requests`` drops None form
    fields, so the request left the board with no color at all and depended on
    Lichess choosing random for an absent parameter. The colour the user picked
    should be what is sent. A regression manifests as a body with no ``color``
    field, which no longer states what the board asked for.
    """
    player, _ = _player_seeking_against(lobby_server, color_preference)
    try:
        assert f"color={color_preference}" in lobby_server.seek_body
    finally:
        player.stop()
