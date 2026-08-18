"""Tests for work requested on one thread and performed on the main loop.

The board answers its serial port, its BLE and RFCOMM clients, the web's
settings socket and its own event subscribers on threads that must not touch the
game or the display: rebuilding widgets or restarting players off the main loop
corrupts the panel. Each of those threads therefore raises a flag that the main
loop notices on its next pass.

There were eleven such flags, each a module global in the application, each
tested and cleared as two separate statements::

    if _pending_player_rebuild:          # a request landing here
        _pending_player_rebuild = False  # is erased, never performed

That window is small and the consequence is silent -- a rebuild that never
happens looks like a board that ignored you -- so the test and the clear are one
locked operation here, and every flag goes through it.
"""

import threading

import pytest

from universalchess.app.pending_work import PendingWork, PieceEventQueue, Slot


def test_a_slot_starts_empty():
    """Nothing is pending until something is requested.

    Why: the main loop polls every slot on every pass, so a slot that reads as
    requested before anyone asked would rebuild the game on the first
    iteration. How a regression manifests: the board rebuilds its players or
    relayouts the display immediately after starting.
    """
    slot = Slot("player_rebuild")

    assert slot.requested() is False
    assert slot.take() is None


def test_taking_a_request_clears_it():
    """A request is performed once, then the slot is empty again.

    Why: the main loop polls continuously, so a request that survived being
    taken would be performed on every pass. How a regression manifests: the
    board rebuilds the game in a loop and never returns to the menu.
    """
    slot = Slot("settings_reload")
    slot.request()

    assert slot.requested() is True
    assert slot.take() is not None
    assert slot.requested() is False
    assert slot.take() is None


def test_a_request_carries_its_payload():
    """The reason or command that came with the request survives the handoff.

    Why: several of these are not bare flags -- the next-game menu needs to know
    the game ended because Lichess aborted it, and the board-command slot
    carries the parsed command itself. How a regression manifests: the next-game
    menu asks "Seek a new game?" after an abort, or a web command arrives empty.
    """
    slot = Slot("lichess_next")
    slot.request("ABORTED")

    request = slot.take()

    assert request is not None
    assert request.payload == "ABORTED"


def test_a_request_without_a_payload_is_still_a_request():
    """A payload-less request is distinguishable from no request at all.

    Why: a player rebuild is usually requested with no reason (the user reset
    the pieces), and if "no payload" read as "nothing pending", that rebuild
    would never run. This is the exact confusion a bare ``Optional`` return
    would create, which is why taking returns a request object rather than the
    payload. How a regression manifests: board-reset new games silently reuse
    the old, stale players.
    """
    slot = Slot("player_rebuild")
    slot.request()

    request = slot.take()

    assert request is not None
    assert request.payload is None


def test_the_last_request_wins():
    """Re-requesting before the main loop takes replaces the payload.

    Why: these slots are single-valued by nature -- two layout rebuilds are one
    layout rebuild -- and the newest reason is the true one. How a regression
    manifests: a stale reason is shown, or requests pile up and the same rebuild
    runs repeatedly.
    """
    slot = Slot("lichess_next")
    slot.request("ABORTED")
    slot.request("NOSTART")

    assert slot.take().payload == "NOSTART"
    assert slot.take() is None


def test_peeking_does_not_consume():
    """A slot can be read without performing it.

    Why: the game-start cancellation flag is checked at several points during a
    start that has not finished yet, and clearing it at the first check would
    let the rest of the start proceed on a cancelled game. How a regression
    manifests: BACK on the Lichess waiting splash is honoured at one checkpoint
    and ignored at the next, so a game starts anyway.
    """
    slot = Slot("cancel_game_start")
    slot.request()

    assert slot.requested() is True
    assert slot.requested() is True
    assert slot.take() is not None


def test_clearing_discards_a_request_without_performing_it():
    """A slot can be reset at the start of a fresh game.

    Why: a cancellation raised against the previous start must not cancel the
    next one, so starting a game clears the flag rather than taking it. How a
    regression manifests: the game after a cancelled one aborts immediately.
    """
    slot = Slot("cancel_game_start")
    slot.request()

    slot.clear()

    assert slot.requested() is False


class _HoldingLock:
    """A real lock that runs a hook once, while the holder is inside it.

    Lets a test act in the middle of a chosen critical section, which is the
    only way to produce this interleaving on purpose: timing alone cannot place
    a request reliably between another thread's read and clear. The hook is
    armed explicitly so the setup calls that also take the lock do not trigger
    it.
    """

    def __init__(self, hook):
        self._lock = threading.Lock()
        self._hook = hook
        self._armed = False

    def arm(self):
        """Run the hook inside the next critical section, and only that one."""
        self._armed = True

    def __enter__(self):
        self._lock.acquire()
        if self._armed:
            self._armed = False
            self._hook()
        return self

    def __exit__(self, *exc_info):
        self._lock.release()
        return False


def test_a_request_cannot_interleave_with_a_take():
    """A thread requesting while a take is in progress waits for it to finish.

    Why: this mutual exclusion is what stops a request being erased. A bare flag
    has none: the main loop reads it as set, a callback thread sets it again for
    a second piece of work, and the main loop then clears it -- performing the
    first request and destroying the second, with nothing logged and no way to
    tell from the board that it happened. The request that arrives during the
    take is held until the take is over, and is then waiting on the next pass.

    How a regression manifests: the request completes inside the take's critical
    section (``arrived_during_take`` is True), which means the read and the
    clear are once again exposed to a write landing between them, and a settings
    change made during a rebuild can be lost.

    Every wait is bounded so an implementation that drops the lock entirely
    fails an assertion instead of hanging the suite.
    """
    observed = {}
    entered_take = threading.Event()
    request_completed = threading.Event()
    writers = []

    def request_from_another_thread():
        """Runs inside the take's critical section, so the request races it."""
        writer = threading.Thread(
            target=lambda: (slot.request("second"), request_completed.set())
        )
        writer.start()
        writers.append(writer)
        entered_take.set()
        observed["arrived_during_take"] = request_completed.wait(timeout=0.2)

    lock = _HoldingLock(request_from_another_thread)
    slot = Slot("settings_reload", lock=lock)
    slot.request("first")

    lock.arm()
    first = slot.take()

    assert entered_take.wait(timeout=1.0), "the take never entered its critical section"
    writers[0].join(timeout=1.0)
    assert observed["arrived_during_take"] is False
    assert request_completed.wait(timeout=1.0)
    assert first is not None and first.payload == "first"
    second = slot.take()
    assert second is not None and second.payload == "second"


def test_the_piece_event_queue_preserves_order():
    """Queued piece events are forwarded in the order the board saw them.

    Why: the events are a lift and a place that make up a move; reordering them
    turns a legal move into a board mismatch the user has to correct by hand.
    How a regression manifests: entering a game from a piece lift leaves the
    board in correction mode.
    """
    queue = PieceEventQueue()
    queue.add(("lift", 12, 1.0))
    queue.add(("place", 28, 2.0))

    assert len(queue) == 2
    assert queue.next() == ("lift", 12, 1.0)
    assert queue.next() == ("place", 28, 2.0)
    assert queue.next() is None
    assert len(queue) == 0


def test_the_piece_event_queue_accepts_events_while_it_is_draining():
    """Events arriving during forwarding are forwarded too, not dropped.

    Why: forwarding an event can produce more of them (the board keeps
    reporting while the game starts), and the drain loop relies on being able to
    see those. How a regression manifests: the first move after entering a game
    is lost.
    """
    queue = PieceEventQueue()
    queue.add(("lift", 12, 1.0))

    drained = []
    while (event := queue.next()) is not None:
        drained.append(event)
        if len(drained) == 1:
            queue.add(("place", 28, 2.0))

    assert drained == [("lift", 12, 1.0), ("place", 28, 2.0)]


def test_clearing_the_queue_discards_events_from_the_previous_game():
    """A new game starts with no events left over from the last one.

    Why: a lift recorded against the finished game's position is meaningless in
    the new one and would be forwarded as a move. How a regression manifests:
    the new game opens already in correction mode.
    """
    queue = PieceEventQueue()
    queue.add(("lift", 12, 1.0))

    queue.clear()

    assert len(queue) == 0
    assert queue.next() is None


@pytest.mark.parametrize(
    "name",
    [
        "settings_reload",
        "player_rebuild",
        "lichess_next",
        "layout_rebuild",
        "board_command",
        "display_profile",
        "ble_client",
        "positions_menu_return",
        "switch_to_normal_game",
        "cancel_game_start",
    ],
)
def test_every_flag_the_application_defers_has_a_slot(name):
    """The set of deferred work is stated in one place.

    Why: each of these was a separate module global with its own test-and-clear,
    and the eleventh was added by copying the tenth. Naming them together is
    what makes the shared, locked handoff possible. How a regression manifests:
    a new deferred flag is added as a global again and races the main loop.
    """
    pending = PendingWork()

    assert isinstance(getattr(pending, name), Slot)


def test_the_application_keeps_no_deferred_flag_of_its_own():
    """No module-level flag in the application defers work any more.

    Why: the eleven flags this class replaced were module globals, and the way
    an unsynchronised twelfth appears is by copying one of them. Reading the
    application's own module-level assignments is the only way to see that,
    since a new global compiles and passes every behavioural test while still
    racing the main loop. How a regression manifests: a name like
    ``_pending_something = False`` reappears here.
    """
    import ast

    from universalchess.tests import app_source

    module_level = []
    for node in ast.parse(app_source.BOARD_APP_PY.read_text()).body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        module_level += [t.id for t in targets if isinstance(t, ast.Name)]

    deferred = [name for name in module_level
                if name.startswith("_pending_") or name == "_cancel_game_start"]

    assert deferred == []


def test_the_piece_event_queue_is_part_of_the_pending_work():
    """Queued piece events are deferred work like the rest.

    Why: they are set on the events thread and forwarded by the main loop, the
    same handoff as the flags, and holding them together keeps the main loop's
    poll in one place. How a regression manifests: the queue drifts back to a
    bare module-level list.
    """
    pending = PendingWork()

    assert isinstance(pending.piece_events, PieceEventQueue)
