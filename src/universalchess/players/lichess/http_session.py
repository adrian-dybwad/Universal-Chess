"""Abortable HTTP streaming for the Lichess Board API.

Lichess keeps a lobby seek listed until the streamed ``POST /api/board/seek``
connection closes, and berserk consumes that response inside
``Requestor.request``, so the application never holds the ``requests.Response``
and cannot close it.

Two teardowns that look correct do not work, both measured against a local
server that reports when its peer closes:

* ``requests.Session.close()`` closes the adapters, which only clears *idle*
  pooled connections. The seek connection is checked out and blocked in a read
  on the seek thread, so it is untouched: the server never sees a close and the
  seek stays in the lobby. This is what made BACK look like it did nothing.
* ``Response.close()`` called from a thread other than the reader deadlocks
  while that read is blocked, which would hang the key thread on BACK.

``socket.shutdown(SHUT_RDWR)`` is the primitive that works: it returns
immediately, wakes the blocked reader with a connection error, and sends FIN so
Lichess drops the seek. This module records every streamed response a session
hands out so those sockets can be shut down when the player stops.
"""

from __future__ import annotations

import socket
import threading
import weakref
from typing import Any, NamedTuple


def _response_socket(response):
    """The socket a streamed ``requests`` response still holds, or None.

    urllib3 exposes no public accessor, so this reads ``raw._connection``, which
    it populates while the body is unconsumed and sets back to None in
    ``release_conn`` once the connection returns to the pool. None therefore
    means this response holds no connection and there is nothing to abort --
    importantly, it also means a socket reachable by any other route belongs to
    whichever request has since borrowed it from the pool, so no second lookup
    path is attempted. If a future urllib3 stops setting ``_connection``,
    test_http_session.py fails rather than this quietly aborting nothing.
    """
    raw = getattr(response, "raw", None)
    if raw is None:
        return None
    return getattr(getattr(raw, "_connection", None), "sock", None)


def abort_stream(response) -> bool:
    """Shut down ``response``'s socket so its reader wakes and the peer sees FIN.

    Returns True when a socket was shut down. Deliberately does not also call
    ``response.close()``: from any thread but the reader that deadlocks against
    the in-progress read. The reader's own iteration ends with a connection
    error and releases the connection.
    """
    sock = _response_socket(response)
    if sock is None:
        return False
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        # Already shut down, or closed by the peer or by the reader finishing.
        # The goal -- no live connection holding a seek open -- already holds.
        return False
    return True


class StreamAbortingSessionMixin:
    """Session mixin that can abort the response streams it opened.

    Mixed in front of a ``requests.Session`` subclass (berserk's
    ``TokenSession`` in production) so every Board API stream -- the seek, the
    incoming-event stream, and the game stream -- is reachable from one place.

    Only streamed responses are tracked: a fully-read response holds no
    connection open. Tracking is weak so a finished stream is forgotten when
    its consumer releases it, rather than accumulating for the process's life.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._open_streams = weakref.WeakSet()
        self._open_streams_lock = threading.Lock()

    def request(self, *args, **kwargs):
        """Record streamed responses; behave exactly as the wrapped session."""
        response = super().request(*args, **kwargs)
        if kwargs.get("stream"):
            with self._open_streams_lock:
                self._open_streams.add(response)
        return response

    def abort_streams(self) -> int:
        """Shut down every stream still open. Returns how many were aborted."""
        with self._open_streams_lock:
            responses = list(self._open_streams)
            self._open_streams.clear()
        return sum(1 for response in responses if abort_stream(response))


class LichessConnection(NamedTuple):
    """A berserk client paired with the session whose streams it opens.

    berserk exposes no way to reach a client's session -- ``BaseClient`` keeps it
    on a private ``Requestor`` -- so teardown code cannot find it from the client
    alone. Pairing the two at construction keeps ``close()`` honest without
    reading berserk's internals: an earlier ``getattr(client, "session", None)``
    silently found nothing and closed nothing.
    """

    client: Any
    session: Any

    def close(self) -> int:
        """Abort in-flight streams, then release pooled connections.

        Order matters: aborting wakes readers blocked on the seek and event
        streams and makes Lichess drop the seek; closing afterwards only clears
        connections sitting idle in the pool. Returns the number of streams
        aborted, which is how many connections Lichess was still holding open.
        """
        aborted = self.session.abort_streams()
        self.session.close()
        return aborted


def abortable_token_session_class():
    """berserk ``TokenSession`` that can abort its own streams.

    Built per call, on the berserk module resolved at that moment, so importing
    this module does not require berserk and no cached class outlives the import
    it was derived from.
    """
    import berserk

    class AbortableTokenSession(StreamAbortingSessionMixin, berserk.TokenSession):
        """A berserk token session whose in-flight streams can be shut down."""

    return AbortableTokenSession
