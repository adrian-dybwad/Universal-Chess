"""Aborting an in-flight Lichess stream must really close the socket.

Why these tests exist
---------------------
Lichess keeps a lobby seek listed until the streamed ``POST /api/board/seek``
connection closes. A previous fix called ``requests.Session.close()`` and was
"verified" with a ``MagicMock`` session, which can only prove that a method was
called -- not that a socket closed. It had not closed: the seek stayed in the
lobby after BACK.

These tests therefore use a real loopback HTTP server that reports when its
peer closes the connection, so the assertions are about the socket the fix has
to move, and mock nothing.
"""

import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

requests = pytest.importorskip("requests")

from universalchess.players.lichess.http_session import (  # noqa: E402
    StreamAbortingSessionMixin,
    _response_socket,
    abort_stream,
)

# Bounds for waits on a loopback socket. The measured times are ~3 ms; these are
# generous enough for a loaded CI box while still failing fast.
CLOSE_DEADLINE_SECONDS = 3.0
# How long to insist a connection is still open when nothing should have closed
# it. Long enough that a real teardown would have landed (measured at 3 ms).
STILL_OPEN_SECONDS = 0.5


class _SeekHandler(BaseHTTPRequestHandler):
    """Stands in for ``POST /api/board/seek``: headers, then silence.

    Lichess sends response headers immediately and then nothing at all while the
    seek waits for an opponent, which is what leaves the client blocked in a
    read. Records the moment the peer closes so a test can assert the seek was
    actually dropped.
    """

    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's required name
        body_length = int(self.headers.get("Content-Length") or 0)
        if body_length:
            self.rfile.read(body_length)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()
        self.server.ledger.record_open()
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
                self.server.ledger.record_close()
                return

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's required name
        """Answer the non-stream polling requests with an empty JSON list."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"[]")

    def log_message(self, *args):
        """Silence the default stderr request log."""


class _ConnectionLedger:
    """Counts streamed connections the server opened and later saw closed.

    Counting rather than a pair of flags because the player holds several
    streams at once (seek, incoming events, game state) and a teardown that
    dropped only the first would look correct to a flag.
    """

    def __init__(self):
        self._changed = threading.Condition()
        self.opened = 0
        self.closed = 0

    def record_open(self) -> None:
        with self._changed:
            self.opened += 1
            self._changed.notify_all()

    def record_close(self) -> None:
        with self._changed:
            self.closed += 1
            self._changed.notify_all()

    def wait_opened(self, count: int, timeout: float) -> bool:
        with self._changed:
            return self._changed.wait_for(lambda: self.opened >= count, timeout)

    def wait_closed(self, count: int, timeout: float) -> bool:
        with self._changed:
            return self._changed.wait_for(lambda: self.closed >= count, timeout)


@pytest.fixture
def seek_server():
    """A loopback server that holds streamed POSTs open and reports closes."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SeekHandler)
    server.ledger = _ConnectionLedger()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=CLOSE_DEADLINE_SECONDS)


class _AbortableSession(StreamAbortingSessionMixin, requests.Session):
    """The production composition (mixin in front of a requests session)."""


class _StreamReader:
    """Consumes a streamed response on a background thread, as berserk's seek does.

    Keeps whatever ended the read so a test can assert the abort broke the
    connection, rather than the server having ended the response cleanly.
    """

    def __init__(self, response):
        self.error = None
        self.finished = threading.Event()
        self._response = response
        self._thread = threading.Thread(target=self._read_until_closed, daemon=True)
        self._thread.start()

    def _read_until_closed(self):
        try:
            for _ in self._response.iter_content(chunk_size=1):
                pass
        except requests.RequestException as error:
            self.error = error
        finally:
            self.finished.set()

    def join(self, timeout: float) -> None:
        self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()


def _open_seek_stream(session, seek_server, expected_open: int = 1):
    """Post a streaming request and wait until the server holds them all open."""
    response = session.post(
        f"{seek_server.url}/api/board/seek",
        data={"time": 10, "increment": 5},
        stream=True,
    )
    assert seek_server.ledger.wait_opened(expected_open, CLOSE_DEADLINE_SECONDS), (
        "server never saw the streamed POST"
    )
    return response


def test_abort_streams_closes_a_stream_the_reader_is_blocked_on(seek_server):
    """The whole point: abort must close the socket while a reader is blocked.

    A regression manifests as the server never seeing the close (Lichess would
    keep the seek listed) or the reader thread never waking (the seek thread
    would outlive the game and stop() would time out joining it).
    """
    session = _AbortableSession()
    response = _open_seek_stream(session, seek_server)
    reader = _StreamReader(response)
    # Let the reader reach its blocking read before aborting, so this exercises
    # the cross-thread case that deadlocks with Response.close().
    time.sleep(0.1)

    started = time.monotonic()
    assert session.abort_streams() == 1
    aborted_in = time.monotonic() - started

    assert seek_server.ledger.wait_closed(1, CLOSE_DEADLINE_SECONDS), (
        "server never saw the connection close, so Lichess would keep the seek"
    )
    assert reader.finished.wait(CLOSE_DEADLINE_SECONDS), "blocked reader never woke"
    reader.join(timeout=CLOSE_DEADLINE_SECONDS)
    assert not reader.is_alive()
    # The read must have been broken by the abort. A cleanly finished read would
    # mean the server ended the response and this test proved nothing.
    assert isinstance(reader.error, requests.RequestException)
    # The caller is the key-press thread; aborting must not block it. Response
    # .close() deadlocks here, which is why abort_stream does not call it.
    assert aborted_in < CLOSE_DEADLINE_SECONDS


def test_abort_streams_closes_every_open_stream_not_just_the_first(seek_server):
    """A stopping player holds three streams at once; all must be dropped.

    NEW mode runs the seek and the incoming-event stream together, and a game
    adds the game-state stream. Aborting only one would leave Lichess holding a
    live connection -- the seek stays listed, or the event stream keeps the
    thread alive past the join in stop().

    A regression manifests as a count below the number opened, or fewer closes
    observed by the server than connections it accepted.
    """
    session = _AbortableSession()
    first = _open_seek_stream(session, seek_server, expected_open=1)
    second = _open_seek_stream(session, seek_server, expected_open=2)
    readers = [_StreamReader(first), _StreamReader(second)]
    time.sleep(0.1)
    assert seek_server.ledger.closed == 0

    assert session.abort_streams() == 2

    assert seek_server.ledger.wait_closed(2, CLOSE_DEADLINE_SECONDS), (
        f"only {seek_server.ledger.closed} of 2 connections closed"
    )
    for reader in readers:
        assert reader.finished.wait(CLOSE_DEADLINE_SECONDS)
        assert isinstance(reader.error, requests.RequestException)


def test_session_close_alone_leaves_the_stream_open(seek_server):
    """Root-cause guard: Session.close() cannot drop a checked-out stream.

    This is the exact mechanism the earlier fix relied on. It is pinned so that
    "simplifying" the teardown back to ``session.close()`` fails here instead of
    silently leaving seeks in the Lichess lobby again. A regression manifests as
    the server seeing a close -- at which point the abort machinery would be
    unnecessary and this test should be deleted deliberately.
    """
    session = requests.Session()
    response = _open_seek_stream(session, seek_server)
    reader = _StreamReader(response)
    time.sleep(0.1)

    session.close()

    assert not seek_server.ledger.wait_closed(1, STILL_OPEN_SECONDS)
    assert not reader.finished.is_set()
    assert reader.error is None


def test_abort_streams_ignores_responses_that_were_not_streamed(seek_server):
    """A fully-read response holds no connection, so it must not be tracked.

    Tracking every response would make abort_streams() shut down sockets that
    urllib3 has already returned to the pool for reuse. A regression manifests
    as a non-zero count here.
    """
    session = _AbortableSession()
    response = session.get(f"{seek_server.url}/api/account/playing")
    assert response.json() == []

    assert session.abort_streams() == 0


def test_abort_streams_is_idempotent_and_forgets_aborted_streams(seek_server):
    """stop() can run twice (BACK then game teardown); the second must be a no-op.

    A regression manifests as the second call reporting an abort again, meaning
    the registry kept a dead response and would shut down a socket that
    urllib3 may by then have reused for another request.
    """
    session = _AbortableSession()
    response = _open_seek_stream(session, seek_server)
    reader = _StreamReader(response)
    time.sleep(0.1)

    assert session.abort_streams() == 1
    assert reader.finished.wait(CLOSE_DEADLINE_SECONDS)
    assert session.abort_streams() == 0


def test_a_completed_response_holds_no_socket_to_abort(seek_server):
    """Pins the urllib3 attribute the abort depends on, in both directions.

    An unconsumed stream must expose its socket, and a response whose body has
    been read must expose none -- by then urllib3 has returned that connection to
    the pool, and shutting it down would break whichever request borrowed it
    next. A regression manifests as a socket on the completed response (a live
    hazard) or none on the open stream (the abort silently stops working).
    """
    session = _AbortableSession()
    streamed = _open_seek_stream(session, seek_server)
    assert _response_socket(streamed) is not None

    completed = session.get(f"{seek_server.url}/api/account/playing")
    assert completed.content == b"[]"

    assert _response_socket(completed) is None
    session.abort_streams()


def test_abort_stream_reports_false_when_there_is_no_socket():
    """A response with no connection must report nothing to abort, not raise.

    ``_response_socket`` returns None once a body is fully read or released.
    A regression manifests as an AttributeError escaping into stop(), which
    would abandon the rest of the teardown.
    """
    assert abort_stream(requests.Response()) is False
