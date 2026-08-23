"""A long-press OK must not eat the next short OK.

Why these tests exist
---------------------
Holding OK for 1s emits LONG_TICK at that 1s boundary (beep, overlay) while
the button is still down, so the user hears that the gesture registered and
does not have to guess when to let go. The matching release is then consumed
so it cannot also count as a short OK. A latch armed for that release was
never cleared when the wait loop ate it, so the next real OK -- the one that
selects Take back on the overlay -- was discarded as a stale key-up.

How a regression manifests
--------------------------
After a held OK and a clean release, the next completed tap is missing (only
LONG_TICK was delivered), so confirming the overlay requires pressing OK
twice. An unpaired bounce TICK after the hold must not appear as a tap either.
"""

from universalchess.board.sync_centaur import (
    Key,
    WAIT_ABORTED,
    dispatchable_after_poll,
    is_key_down_event,
    wait_for_matching_release,
)


class _Clock:
    """Monotonic clock that advances on sleep."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class _QueuedKeys:
    """Releases the matching up once the clock has passed ``release_at``."""

    def __init__(self, clock, up, *, release_at):
        self.clock = clock
        self.up = up
        self.release_at = release_at
        self._sent = False

    def get_next_key(self, timeout=0.0):
        if self._sent or self.clock.t < self.release_at:
            return None
        self._sent = True
        return self.up


def _wait(clock, key_down, up, *, release_at, on_threshold):
    return wait_for_matching_release(
        key_down,
        _QueuedKeys(clock, up, release_at=release_at).get_next_key,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        on_threshold=on_threshold,
    )


def test_key_down_is_only_the_pressed_event():
    """Long-press waits start on a down; a key-up is not a new press.

    Why: eventsThread must ignore an unpaired TICK (bounce after a hold) and
    only start a wait on TICK_DOWN. How a regression manifests: TICK or
    LONG_TICK is treated as a down, so a leftover release begins a wait that
    never sees a matching up.
    """
    assert is_key_down_event(Key.TICK_DOWN) is True
    assert is_key_down_event(Key.PLAY_DOWN) is True
    assert is_key_down_event(Key.TICK) is False
    assert is_key_down_event(Key.LONG_TICK) is False
    assert is_key_down_event(None) is False


def test_held_ok_fires_at_the_one_second_boundary_and_ignores_the_release():
    """LONG_TICK must fire at 1s, not when the user lets go.

    Why: the beep and overlay have to appear at the threshold so the hold is
    visible; the later release is leftover and must not also be a short OK.
    How a regression manifests: on_threshold is never called, or the wait
    returns TICK (the release is dispatched as a tap).
    """
    clock = _Clock()
    fired = []

    def on_threshold():
        fired.append(clock.t)
        return None

    result = _wait(
        clock, Key.TICK_DOWN, Key.TICK, release_at=1.3, on_threshold=on_threshold
    )
    assert len(fired) == 1
    assert fired[0] >= 1.0
    assert fired[0] < 1.0 + 0.05
    assert result is None


def test_short_ok_is_the_release():
    """A tap that never reaches 1s must still be a TICK.

    Why: short OK pages coach text / refreshes and confirms the overlay.
    How a regression manifests: result is None (treated as a consumed hold)
    or LONG_TICK (on_threshold ran for a sub-second press).
    """
    clock = _Clock()
    fired = []
    result = _wait(
        clock,
        Key.TICK_DOWN,
        Key.TICK,
        release_at=0.2,
        on_threshold=lambda: fired.append(True),
    )
    assert fired == []
    assert result is Key.TICK


def test_short_ok_after_a_held_ok_is_still_a_tick():
    """The OK that confirms the overlay must not be treated as the hold's release.

    Why: a latch leftover from the hold used to match the tap's TICK and drop
    it. Each completed down-wait is a new gesture. How a regression manifests:
    dispatchable_after_poll returns None for the tap, so takeback needs two
    presses.
    """
    clock = _Clock()
    hold = _wait(
        clock,
        Key.TICK_DOWN,
        Key.TICK,
        release_at=1.3,
        on_threshold=lambda: None,
    )
    tap_clock = _Clock()
    tap = _wait(
        tap_clock,
        Key.TICK_DOWN,
        Key.TICK,
        release_at=0.2,
        on_threshold=lambda: None,
    )
    assert hold is None
    assert dispatchable_after_poll(tap, completed_down_wait=True) is Key.TICK


def test_bounce_release_without_a_down_is_not_a_tap():
    """A leftover TICK after the hold must not count as the confirming OK.

    Why: contact bounce can enqueue a second key-up after the matching
    release was consumed. Dispatching it would select the highlighted overlay
    row the instant the user lets go. How a regression manifests:
    dispatchable_after_poll returns TICK for an unpaired up.
    """
    assert dispatchable_after_poll(Key.TICK, completed_down_wait=False) is None
    assert dispatchable_after_poll(None, completed_down_wait=True) is None


def test_play_shutdown_at_threshold_aborts_the_wait():
    """PLAY's countdown owns the hold; the wait must not sit for a second up.

    Why: shutdown_countdown already consumed PLAY's release. Waiting for it
    again would hang the events thread. How a regression manifests: the wait
    returns a Key instead of WAIT_ABORTED, or never returns.
    """
    clock = _Clock()
    result = _wait(
        clock,
        Key.PLAY_DOWN,
        Key.PLAY,
        release_at=10.0,
        on_threshold=lambda: WAIT_ABORTED,
    )
    assert result is WAIT_ABORTED
    assert clock.t >= 1.0
    assert clock.t < 1.0 + 0.05
