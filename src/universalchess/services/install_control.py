"""Board-side client for the engine-install service the web process owns.

Engine installs run in one place. The web process holds the persisted install
state, writes the resume points, and runs every install flow: the catalog build,
the repair, and the custom-engine-from-URL upload. The board asks it to act and
reads the outcome from that shared state.

That split exists because the alternative was tried. When each process installed
on its own ``EngineManager``, a build stopped from the board left a preserved tree
the web could neither see, resume nor reclaim, and nothing prevented the two from
starting installs simultaneously.

Transport
---------
No new socket. A request goes out as an ``engine_install_request`` event on the
game socket, the channel the board already uses for battery, clock and Bluetooth
status. The answer comes back as an ``engine_install_reply`` board command on the
settings socket, the channel that already carries shutdown and reboot. The reply
is matched to its request by id, because the two cross different sockets in
opposite directions and nothing else relates them.

What the reply means
--------------------
Only whether the request was *accepted*. A refusal is information the board cannot
work out for itself -- another install already running, an unknown engine, a
discard of a tree still being written to -- and without it the board would open a
progress screen over a build that was never dispatched. What happens next is
observed in the persisted install state, which is the record that survives a
restart of either process.
"""

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, Optional

log = logging.getLogger(__name__)

__all__ = [
    "REPLY_COMMAND",
    "REQUEST_EVENT",
    "InstallActionResult",
    "InstallControlClient",
    "get_install_control",
]

# Event name for board -> web requests (game socket).
REQUEST_EVENT = "engine_install_request"
# Board command name for web -> board replies (settings socket).
REPLY_COMMAND = "engine_install_reply"

# The web validates and dispatches before replying, which is a thread spawn or a
# flag set. Generous enough to absorb a loaded board, short enough that a dead web
# process does not look like a hung menu.
REPLY_TIMEOUT_SECONDS = 5.0

# Discard deletes the build tree inline before replying. A Rust build tree is tens
# of thousands of files on an SD card, so it is measured in tens of seconds; held
# to the ordinary deadline it would report a timeout for a removal that is working.
DISCARD_TIMEOUT_SECONDS = 120.0

_ACTION_TIMEOUTS = {"discard": DISCARD_TIMEOUT_SECONDS}


@dataclass(frozen=True)
class InstallActionResult:
    """Whether the web process took the request, and what it said about it.

    ``message`` is display text either way: the far end's description of what it
    started, or its reason for refusing. It is shown to the user verbatim, because
    only that process knows what is actually going on.
    """

    accepted: bool
    message: str


class InstallControlClient:
    """Sends install requests to the web process and waits for its answer.

    The transport is injected so this is testable without sockets, and so the
    board's wiring stays in one place.
    """

    def __init__(
        self,
        send_request: Optional[Callable[[str, dict], bool]] = None,
        reply_timeout_seconds: float = REPLY_TIMEOUT_SECONDS,
    ):
        self._send_request = send_request if send_request is not None else _broadcast_request
        self._reply_timeout_seconds = reply_timeout_seconds
        self._lock = threading.Lock()
        self._pending: Dict[str, "_PendingRequest"] = {}

    # -- actions ------------------------------------------------------------
    def install(self, engine: str, ref: Optional[str] = None) -> InstallActionResult:
        """Ask for a fresh install of ``engine``, optionally at a chosen git ref."""
        return self._request("install", engine=engine, ref=ref)

    def resume(self, engine: str) -> InstallActionResult:
        """Ask for a paused install to continue from its preserved tree.

        Deliberately not an install carrying a ref: only a resume reuses the tree,
        and the ref comes from the engine's own resume point at the far end rather
        than from the board, so the rebuild targets what the tree actually holds.
        """
        return self._request("resume", engine=engine)

    def stop(self) -> InstallActionResult:
        """Ask for the running install to stop, preserving its build tree.

        Names no engine: one install runs at a time and the far end stops the
        manager holding it. A name from the board would be a stale guess about
        which install that is.
        """
        return self._request("stop")

    def discard(self, engine: str) -> InstallActionResult:
        """Ask for a paused install's resume point and build tree to be removed."""
        return self._request("discard", engine=engine)

    # -- reply plumbing -----------------------------------------------------
    def deliver_reply(self, payload: dict) -> None:
        """Hand a reply to whichever request is waiting for it.

        Called from the settings-socket listener thread. A reply whose id is not
        waiting is dropped: it belongs to a request that already timed out, and
        resolving some other caller with it would report the wrong outcome.
        """
        request_id = payload.get("request_id")
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            log.debug("Ignoring install reply for unknown request %r", request_id)
            return
        pending.resolve(InstallActionResult(
            accepted=bool(payload.get("accepted")),
            message=str(payload.get("message") or ""),
        ))

    def timeout_for(self, action: str) -> float:
        """Seconds to wait for ``action``'s reply."""
        return _ACTION_TIMEOUTS.get(action, self._reply_timeout_seconds)

    @property
    def pending_count(self) -> int:
        """Requests still awaiting a reply. Exposed so leaks are visible to tests."""
        with self._lock:
            return len(self._pending)

    def _request(self, action: str, **params) -> InstallActionResult:
        request_id = uuid.uuid4().hex
        pending = _PendingRequest()
        with self._lock:
            self._pending[request_id] = pending
        try:
            payload = {"action": action, "request_id": request_id, **params}
            if not self._send_request(REQUEST_EVENT, payload):
                # The datagram had nowhere to go, so no reply is coming. Waiting
                # out the deadline would stall the menu on a question never asked.
                log.warning("Engine install %s not delivered: web process not listening", action)
                return InstallActionResult(False, "Web service is not running")
            result = pending.wait(self.timeout_for(action))
            if result is None:
                log.warning("No reply to engine install %s within its deadline", action)
                return InstallActionResult(False, "Web service is not responding")
            return result
        finally:
            with self._lock:
                self._pending.pop(request_id, None)


class _PendingRequest:
    """One in-flight request, resolved by the listener thread."""

    def __init__(self):
        self._event = threading.Event()
        self._result: Optional[InstallActionResult] = None

    def resolve(self, result: InstallActionResult) -> None:
        self._result = result
        self._event.set()

    def wait(self, timeout_seconds: float) -> Optional[InstallActionResult]:
        if not self._event.wait(timeout_seconds):
            return None
        return self._result


def _broadcast_request(event_type: str, data: dict) -> bool:
    """Publish a request on the game socket (board -> web).

    Imported lazily so this module can be used in tests, and on hardware without
    the socket, without pulling in the broadcast stack.
    """
    from universalchess.services.game_broadcast import get_broadcaster

    return get_broadcaster().broadcast_event(event_type, data)


_CLIENT: Optional[InstallControlClient] = None


def get_install_control() -> InstallControlClient:
    """Return the board's shared install-control client."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = InstallControlClient()
    return _CLIENT
