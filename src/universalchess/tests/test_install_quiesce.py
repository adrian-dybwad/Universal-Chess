"""Tests for stopping a running engine install before the Pi powers off.

Background / why these tests exist
-----------------------------------
An engine install can run for the better part of an hour, and the Pi can decide
to power off underneath it: the idle timeout fires, or (once it exists) the
battery runs low. Until now the install simply died with the process. The build
tree was left part-written and the install came back at next boot as
"interrupted" by startup reconciliation, rather than as the clean stop the user
would have got by pressing Stop.

Installs run in the web process, which owns the persisted state and the resume
points. Powering off is decided elsewhere -- usually by the board process, and by
the web itself when the board is not running to do it. So the power-off path has
to *ask* for the stop and then wait for it, rather than perform it.

What "wait for it" means
------------------------
Stopping is cooperative: the request sets a cancel flag, the build stops at the
next command boundary, its process group is terminated, and only then is the
resume point written and the active state cleared. The persisted state going
inactive is therefore the signal that the work is safely recorded -- which is why
this watches the state rather than trusting the reply to the stop request.

The wait is bounded. A shutdown that hangs because an install will not wind down
is worse than a shutdown that gives up on it: on low battery there may not be
time to spare, and the interrupted-install reconciliation still recovers what is
left. So the budget is spent and the power-off proceeds either way.
"""

import pytest

from universalchess.services.install_quiesce import (
    POWER_OFF_STOP_BUDGET_SECONDS,
    stop_install_for_board_power_off,
    stop_install_for_power_off,
)

ENGINE = "reckless"
OTHER_ENGINE = "stockfish"


class _Clock:
    """A clock that only moves when something sleeps on it.

    Real elapsed time would make the budget assertions flaky on a loaded Pi --
    and the machine this guards is a single-core Pi Zero under a compile.
    """

    def __init__(self):
        self.now_seconds = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now_seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now_seconds += seconds


class _Installs:
    """The install state a power-off watches, and the stop requests it makes.

    ``active_reads`` is how many reads report a running install before it winds
    down, which is how a cooperative stop looks from the outside: still running,
    still running, then gone.
    """

    def __init__(self, engine=None, active_reads=0, stop_reply=True):
        self.engine = engine
        self.remaining_active_reads = active_reads
        self.stop_reply = stop_reply
        self.stop_requests = []
        self.reads = 0

    def read_status(self):
        self.reads += 1
        if self.engine is None or self.remaining_active_reads <= 0:
            return {"active": False, "engine": None}
        self.remaining_active_reads -= 1
        return {"active": True, "engine": self.engine}

    def request_stop(self):
        self.stop_requests.append(self.engine)
        return self.stop_reply


def _quiesce(installs, clock, budget_seconds=POWER_OFF_STOP_BUDGET_SECONDS):
    """Run the power-off stop against the given fakes."""
    return stop_install_for_power_off(
        read_status=installs.read_status,
        request_stop=installs.request_stop,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        budget_seconds=budget_seconds,
    )


class TestNothingToStop:
    """The common case: the board powers off with no install running."""

    def test_an_idle_board_asks_for_nothing_and_waits_for_nothing(self):
        """With no install running, no stop is requested and no time is spent.

        Why: almost every power-off happens with nothing building, and the
        shutdown must not grow a delay -- or a spurious stop request that the web
        would refuse -- to pay for a case that is not happening.

        How a regression manifests: a stop is requested with no engine to stop,
        or the shutdown sleeps out its budget waiting for state that is already
        idle, adding seconds to every power-off.
        """
        installs = _Installs(engine=None)
        clock = _Clock()

        outcome = _quiesce(installs, clock)

        assert outcome.engine is None
        assert outcome.wound_down is True
        assert installs.stop_requests == []
        assert clock.sleeps == []


class TestStoppingARunningInstall:
    """An install is running when the power-off begins."""

    def test_the_install_is_stopped_and_waited_for(self):
        """A running install is asked to stop, and the wait ends when it has.

        Why: this is the whole point. The resume point is written by the build as
        it unwinds, so returning before the state clears would let the poweroff
        cut the process mid-write -- exactly the interrupted install this
        replaces.

        How a regression manifests: the outcome reports success while the state
        still says active, meaning the caller powers off during the unwind.
        """
        installs = _Installs(engine=ENGINE, active_reads=3)
        clock = _Clock()

        outcome = _quiesce(installs, clock)

        assert outcome.engine == ENGINE
        assert outcome.wound_down is True
        assert installs.stop_requests == [ENGINE]

    def test_the_stop_is_requested_once_however_long_the_wait(self):
        """Polling the state must not re-issue the stop on every pass.

        Why: the stop terminates the build's process group. Repeating it each
        quarter-second while the build unwinds is at best noise in the log and at
        worst a second termination arriving during cleanup.

        How a regression manifests: the request moves inside the polling loop and
        the count rises with the length of the wait.
        """
        installs = _Installs(engine=ENGINE, active_reads=12)
        clock = _Clock()

        outcome = _quiesce(installs, clock)

        assert outcome.wound_down is True
        assert installs.stop_requests == [ENGINE]
        assert len(clock.sleeps) >= 11  # it really did wait out those reads

    def test_it_returns_as_soon_as_the_install_is_gone(self):
        """The wait ends on the state clearing, not on the budget expiring.

        Why: a stop usually lands in well under a second. Spending the whole
        budget anyway would add that delay to every power-off that happens to
        catch an install, including one on a dying battery.

        How a regression manifests: the loop sleeps out the full budget before
        checking, so a fast stop costs as much as a stuck one.
        """
        installs = _Installs(engine=ENGINE, active_reads=1)
        clock = _Clock()

        _quiesce(installs, clock)

        assert clock.now_seconds < POWER_OFF_STOP_BUDGET_SECONDS

    def test_the_engine_reported_is_the_one_that_was_running(self):
        """The outcome names the install it acted on, for the log and the screen.

        Why: the caller tells the user what it is waiting for, and records what it
        stopped. Reporting the wrong name -- or none -- makes a shutdown that
        pauses for several seconds look unexplained.

        How a regression manifests: the engine is read from the final (idle)
        status rather than the first, so it comes back None on every successful
        stop.
        """
        installs = _Installs(engine=OTHER_ENGINE, active_reads=2)
        clock = _Clock()

        outcome = _quiesce(installs, clock)

        assert outcome.engine == OTHER_ENGINE


class TestWhenTheInstallWillNotStop:
    """The build ignores the stop, or the web process is wedged."""

    def test_the_power_off_is_not_blocked_for_ever(self):
        """A stop that never lands gives up inside the budget.

        Why: the caller is on its way to powering the Pi off, sometimes because
        the battery is nearly flat. Waiting indefinitely for a build that will not
        unwind turns a tidy shutdown into a hang, and systemd would kill it
        anyway. The interrupted-install recovery still catches what is left.

        How a regression manifests: the loop has no exit but the state clearing,
        so an install stuck mid-compile hangs the shutdown until systemd's stop
        timeout kills it.
        """
        installs = _Installs(engine=ENGINE, active_reads=10_000)
        clock = _Clock()

        outcome = _quiesce(installs, clock)

        assert outcome.wound_down is False
        assert outcome.engine == ENGINE
        assert clock.now_seconds <= POWER_OFF_STOP_BUDGET_SECONDS

    @pytest.mark.parametrize("budget_seconds", [0.5, 2.0, 20.0])
    def test_the_budget_is_honoured_whatever_it_is(self, budget_seconds):
        """Any budget is spent and no more.

        Why: a low-battery shutdown will want a shorter budget than an idle one,
        so the bound has to be the caller's to set rather than a constant baked
        into the loop.

        How a regression manifests: the budget is ignored (the loop counts
        iterations instead of time), so a shorter budget waits just as long.
        """
        installs = _Installs(engine=ENGINE, active_reads=10_000)
        clock = _Clock()

        outcome = _quiesce(installs, clock, budget_seconds=budget_seconds)

        assert outcome.wound_down is False
        assert clock.now_seconds <= budget_seconds

    def test_a_refused_stop_is_not_taken_as_a_stop(self):
        """The state decides the outcome, not the reply to the request.

        Why: the board's stop request is answered by the web, and an accepted
        reply only means the flag was set -- the build still has to reach a
        command boundary and write its resume point. Trusting the reply would
        return before any of that, which is the mid-write power-off this exists
        to prevent.

        How a regression manifests: the reply is used as the completion signal,
        so the outcome claims the install wound down the instant it was asked to.
        """
        installs = _Installs(engine=ENGINE, active_reads=10_000, stop_reply=True)
        clock = _Clock()

        outcome = _quiesce(installs, clock, budget_seconds=1.0)

        assert outcome.wound_down is False

    def test_a_refused_stop_still_ends_the_wait_when_the_install_is_gone(self):
        """A refusal with no install actually running does not burn the budget.

        Why: the two sides can disagree -- the state said active, the web says
        there is nothing to stop (it finished in between). The state going idle
        must end the wait normally rather than leaving the shutdown to time out
        on an install that is already gone.

        How a regression manifests: a refused request is treated as fatal and the
        wait is skipped or extended, either powering off too early or too late.
        """
        installs = _Installs(engine=ENGINE, active_reads=1, stop_reply=False)
        clock = _Clock()

        outcome = _quiesce(installs, clock)

        assert outcome.wound_down is True
        assert installs.stop_requests == [ENGINE]


class TestTheBoardsWiring:
    """What the board process plugs into the wait when it powers the Pi off.

    The board does not run installs. It asks the web over the socket that already
    connects them, and watches the state file the web writes.
    """

    class _Control:
        """Stands in for the board's install control client."""

        def __init__(self):
            self.stops = 0

        def stop(self):
            self.stops += 1
            return True

    class _Store:
        """An install state store that distinguishes cached from re-read."""

        def __init__(self, statuses):
            self.statuses = list(statuses)
            self.cached_reads = 0
            self.disk_reads = 0

        def status_dict(self):
            self.cached_reads += 1
            return self._next()

        def observed_status_dict(self):
            self.disk_reads += 1
            return self._next()

        def _next(self):
            return self.statuses.pop(0) if self.statuses else {"active": False, "engine": None}

    def test_the_board_asks_the_web_to_stop_the_install(self):
        """The board issues one stop request through its control client.

        Why: engine installs run in the web process alone. The board has no
        manager to cancel and no resume point to write, so its only move is to
        ask -- and it must actually ask, or the power-off proceeds over a build
        that was never told to stop.

        How a regression manifests: the board calls a local stop that no longer
        exists, or asks nobody, and the install dies with the Pi.
        """
        control = self._Control()
        store = self._Store([{"active": True, "engine": ENGINE},
                             {"active": False, "engine": None}])
        clock = _Clock()

        outcome = stop_install_for_board_power_off(
            control=control, store=store, sleep=clock.sleep, monotonic=clock.monotonic)

        assert control.stops == 1
        assert outcome.engine == ENGINE
        assert outcome.wound_down is True

    def test_the_board_re_reads_the_state_the_web_writes(self):
        """The board watches the file, not a copy it loaded earlier.

        Why: the web process is what clears the active install, by writing the
        shared state file. The board's in-memory copy is whatever it last
        happened to load and would never change, so the wait would run to its
        full budget on every power-off and then report failure on an install that
        stopped immediately.

        How a regression manifests: the wiring reads status_dict() instead of
        observed_status_dict(), so the cached counter moves and the disk one does
        not.
        """
        control = self._Control()
        store = self._Store([{"active": True, "engine": ENGINE},
                             {"active": False, "engine": None}])
        clock = _Clock()

        stop_install_for_board_power_off(
            control=control, store=store, sleep=clock.sleep, monotonic=clock.monotonic)

        assert store.disk_reads > 0
        assert store.cached_reads == 0

    def test_an_idle_board_does_not_ask_the_web_for_anything(self):
        """With nothing installing, the board sends no request at all.

        Why: every ordinary power-off takes this path. A stop request the web
        would only refuse costs a socket round trip on a single-core Pi that is
        already busy shutting down, and puts a refusal in the log for a situation
        that is entirely normal.

        How a regression manifests: the request is sent unconditionally and every
        shutdown logs a refused stop.
        """
        control = self._Control()
        store = self._Store([{"active": False, "engine": None}])
        clock = _Clock()

        outcome = stop_install_for_board_power_off(
            control=control, store=store, sleep=clock.sleep, monotonic=clock.monotonic)

        assert control.stops == 0
        assert outcome.engine is None
        assert outcome.wound_down is True
