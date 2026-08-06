"""Tests for the poll that bridges card preparation and the host DNS check.

The wait is what lets one script span both phases: it holds while the user moves
the card into the Pi and connects it, then hands control to the check as soon as
the host creates the interface facing the board.

Clock and sleep are injected throughout, so none of these tests spend real time.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hostcheck

BRIDGE_ADDRESS = "192.168.2.1"
BRIDGE_UP = (
    "bridge100: flags=8a63<UP,BROADCAST,SMART,RUNNING> mtu 1500\n"
    f"\tinet {BRIDGE_ADDRESS} netmask 0xffffff00 broadcast 192.168.2.255\n"
)
NO_BRIDGE = (
    "en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
    "\tinet 10.0.0.42 netmask 0xffffff00 broadcast 10.0.0.255\n"
)
TIMEOUT_SECONDS = 30
POLL_SECONDS = 3


class _Clock:
    """A monotonic clock that only advances when the code under test sleeps."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


class _Sequence:
    """Returns each queued output in turn, repeating the last one forever."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def __call__(self, _command):
        output = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return output


def _wait(run, clock, timeout_seconds=TIMEOUT_SECONDS):
    return hostcheck.wait_for_shared_link(
        hostcheck.PLATFORMS["macos"],
        run,
        timeout_seconds=timeout_seconds,
        poll_seconds=POLL_SECONDS,
        sleep=clock.sleep,
        now=clock.monotonic,
    )


class TestWaitForSharedLink:
    def test_returns_immediately_when_the_link_is_already_up(self):
        """A board already connected must not cost the user a poll interval.

        Why this test exists: re-running the tool on a working setup is common,
        and an unnecessary sleep there makes the tool feel broken.

        How the regression manifests: clock.slept is non-empty, meaning the loop
        slept before its first check.
        """
        clock = _Clock()
        run = _Sequence([BRIDGE_UP])

        found = _wait(run, clock)

        assert found is not None
        assert found.name == "bridge100"
        assert found.address == BRIDGE_ADDRESS
        assert clock.slept == []
        assert run.calls == 1

    def test_keeps_polling_until_the_board_finishes_booting(self):
        """The link appearing late must still be caught.

        Why this test exists: this is the normal case. A Pi Zero runs cloud-init
        on slow hardware, so the interface routinely shows up long after the
        cable goes in. Giving up after one look would make the check useless.

        How the regression manifests: None returned despite the bridge appearing
        while time remained, or the poll stopping early.
        """
        clock = _Clock()
        run = _Sequence([NO_BRIDGE, NO_BRIDGE, NO_BRIDGE, BRIDGE_UP])

        found = _wait(run, clock)

        assert found is not None
        assert found.address == BRIDGE_ADDRESS
        assert run.calls == 4
        assert clock.slept == [POLL_SECONDS] * 3

    def test_gives_up_at_the_deadline(self):
        """The wait must be bounded, and bounded by the timeout it was given.

        Why this test exists: an unbounded poll would hang the tool forever when
        a board is faulty or never connected, which is precisely the situation
        where the user most needs it to come back and say something.

        How the regression manifests: the test does not terminate, or the
        elapsed time on the fake clock runs past the timeout.
        """
        clock = _Clock()
        run = _Sequence([NO_BRIDGE])

        found = _wait(run, clock)

        assert found is None
        assert clock.now <= TIMEOUT_SECONDS + POLL_SECONDS

    def test_a_zero_timeout_checks_once_and_stops(self):
        """The degenerate timeout must still look exactly once.

        Why this test exists: zero is the boundary between "check now" and
        "wait", and an off-by-one here either skips the check entirely or sleeps
        when explicitly told not to wait. Both are silent failures.

        How the regression manifests: run.calls is 0, or clock.slept is
        non-empty.
        """
        clock = _Clock()
        run = _Sequence([NO_BRIDGE])

        found = _wait(run, clock, timeout_seconds=0)

        assert found is None
        assert run.calls == 1
        assert clock.slept == []

    def test_ignores_interfaces_that_are_not_the_gadget_link(self):
        """Unrelated interfaces must never be mistaken for the board.

        Why this test exists: hosts have many interfaces up at once. Latching
        onto the wrong one would point the whole DNS diagnosis at an address the
        Pi cannot see, producing confident and wrong advice.

        How the regression manifests: en0's 10.0.0.42 returned as the link.
        """
        clock = _Clock()
        run = _Sequence([NO_BRIDGE])

        assert _wait(run, clock, timeout_seconds=0) is None
