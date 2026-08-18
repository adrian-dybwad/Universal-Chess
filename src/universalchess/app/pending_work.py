"""Work requested on a callback thread and performed on the main loop.

The board listens on several threads it does not control: the serial event
subscriber, the BLE and RFCOMM servers, and the web's settings socket. None of
them may touch the game or the display -- rebuilding widgets or restarting
players off the main loop corrupts the panel -- so each raises a flag that the
main loop notices on its next pass and acts on there.

Every flag is single-valued: two layout rebuilds are one layout rebuild, and the
newest reason for a next-game menu is the true one. What each flag must not do
is lose a request, which a bare module global did, because testing and clearing
it were two statements with a window between them::

    if _pending_player_rebuild:          # a request landing here
        _pending_player_rebuild = False  # is erased, never performed

:meth:`Slot.take` is that test and clear under one lock, so a request either
belongs to this pass or waits for the next one.
"""

import threading
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass(frozen=True)
class Request:
    """A piece of requested work, and whatever came with it.

    Exists so a request with no payload is still distinguishable from no request
    at all: a player rebuild usually carries no reason, and returning its
    payload directly would make it indistinguishable from an empty slot.
    """

    payload: Any = None


class Slot:
    """One kind of deferred work, requested off the main loop and taken on it.

    Requesting again before the main loop takes replaces the pending request:
    the work is idempotent and the newest payload is the accurate one.
    """

    def __init__(self, name: str, lock: Optional[Any] = None) -> None:
        """Create an empty slot.

        Args:
            name: What this work is, for logs and for reading the main loop.
            lock: The mutex guarding the slot. Injected only so a test can hold
                the handoff open and request into the middle of a take, which is
                the interleaving this class exists to survive and which cannot
                be produced by timing alone.
        """
        self.name = name
        self._lock = lock if lock is not None else threading.Lock()
        self._request: Optional[Request] = None

    def request(self, payload: Any = None) -> None:
        """Ask the main loop to do this work.

        Args:
            payload: The reason or command to carry, if the work needs one.
        """
        with self._lock:
            self._request = Request(payload)

    def take(self) -> Optional[Request]:
        """Claim the pending request, if there is one, and empty the slot.

        Atomic, so a request made on another thread during this call is either
        returned here or left for the next pass -- never dropped.

        Returns:
            The request, or None when nothing is pending.
        """
        with self._lock:
            request, self._request = self._request, None
            return request

    def requested(self) -> bool:
        """Whether work is pending, without claiming it.

        For flags that are consulted at several points before being acted on
        (a game start that may have been cancelled), where claiming at the first
        check would let the remaining checks pass.
        """
        with self._lock:
            return self._request is not None

    def clear(self) -> None:
        """Discard any pending request without performing it.

        For state that belongs to a finished attempt: a cancellation raised
        against the previous game start must not cancel the next one.
        """
        with self._lock:
            self._request = None


class PieceEventQueue:
    """Piece events seen while the board could not yet handle them.

    A lift that happens on a menu, or before the game's handler is wired, is
    kept here and forwarded once the game can take it -- it is usually the first
    half of the user's first move. Order is the order the board reported, since
    a lift and its place only make sense in sequence.

    Drained one event at a time rather than in bulk: forwarding an event can
    produce more of them, and those must be forwarded in the same pass.
    """

    def __init__(self) -> None:
        """Create an empty queue."""
        self._lock = threading.Lock()
        self._events: List[Any] = []

    def add(self, event: Any) -> None:
        """Queue an event for the game to receive later.

        Args:
            event: The ``(piece_event, field, timestamp)`` triple as reported.
        """
        with self._lock:
            self._events.append(event)

    def next(self) -> Optional[Any]:
        """Remove and return the oldest queued event.

        Returns:
            The event, or None when the queue is empty.
        """
        with self._lock:
            if not self._events:
                return None
            return self._events.pop(0)

    def clear(self) -> None:
        """Discard every queued event.

        Called when a game starts, because a lift recorded against the previous
        position would be forwarded as a move in the new one.
        """
        with self._lock:
            self._events.clear()

    def __len__(self) -> int:
        """Number of events waiting to be forwarded."""
        with self._lock:
            return len(self._events)


@dataclass
class PendingWork:
    """Every piece of work the board defers to its main loop.

    Named together because the main loop polls them together, and because each
    of these began as a separate module global copied from the one before it.

    Attributes:
        settings_reload: The web changed settings; rebuild the live game display.
        player_rebuild: Restart play with players rebuilt from current settings,
            because a board-reset new game would otherwise reuse stale ones.
            Carries the Lichess termination reason when a remote game ended.
        lichess_next: Why the next-game Lichess menu is showing (a remote abort
            or no-start), so its header names that instead of asking to seek.
        layout_rebuild: A layout-affecting setting changed under a reused
            display; rebuild the widgets before play continues.
        board_command: A board-control command pushed from the web (set up a
            position, abort the game).
        display_profile: A live waveform-profile change from the web, which
            re-initializes the panel.
        ble_client: A client connected while the board was between menus;
            carries which transport it was.
        positions_menu_return: Leave the game and return to the Positions menu.
        switch_to_normal_game: Leave a position (practice) game for a normal one.
        cancel_game_start: BACK arrived while a game was still being constructed,
            possibly before the managers it would tear down exist.
        piece_events: Piece events seen before the game could handle them.
    """

    settings_reload: Slot = field(default_factory=lambda: Slot("settings_reload"))
    player_rebuild: Slot = field(default_factory=lambda: Slot("player_rebuild"))
    lichess_next: Slot = field(default_factory=lambda: Slot("lichess_next"))
    layout_rebuild: Slot = field(default_factory=lambda: Slot("layout_rebuild"))
    board_command: Slot = field(default_factory=lambda: Slot("board_command"))
    display_profile: Slot = field(default_factory=lambda: Slot("display_profile"))
    ble_client: Slot = field(default_factory=lambda: Slot("ble_client"))
    positions_menu_return: Slot = field(
        default_factory=lambda: Slot("positions_menu_return")
    )
    switch_to_normal_game: Slot = field(
        default_factory=lambda: Slot("switch_to_normal_game")
    )
    cancel_game_start: Slot = field(default_factory=lambda: Slot("cancel_game_start"))
    piece_events: PieceEventQueue = field(default_factory=PieceEventQueue)
