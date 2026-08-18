"""Tests for the board's teardown on the way out.

These exist because shutdown was eleven copies of "log the intent, call the
teardown, catch and log anything it raised". Nothing checked that a step which
failed left the rest to run, and nothing checked that the controller is always
told to sleep -- a controller that stays awake drains the battery flat while the
Pi is off, which is the one failure here the user actually feels.
"""

import pytest

from universalchess.app.shutdown import (
    LED_CASCADE_SQUARES,
    quiesce_controller,
    released_by,
    run_teardown,
)


class _Subsystem:
    """A subsystem that records its teardown against a shared log."""

    def __init__(self, name: str, calls: list, *, raises: bool = False):
        self._name = name
        self._calls = calls
        self._raises = raises
        self.kwargs = None

    def stop(self, **kwargs):
        self.kwargs = kwargs
        self._calls.append(self._name)
        if self._raises:
            raise RuntimeError(f"{self._name} would not stop")

    cleanup = stop


class _Board:
    """A stand-in for the board module, recording what shutdown asks of it."""

    SOUND_POWER_OFF = "power-off"

    def __init__(self, *, beep_raises=False, led_raises=False, slept=True):
        self.calls = []
        self.lit = []
        self._beep_raises = beep_raises
        self._led_raises = led_raises
        self._slept = slept

    def beep(self, sound):
        self.calls.append(f"beep:{sound}")
        if self._beep_raises:
            raise RuntimeError("no speaker")

    def led(self, square, **kwargs):
        self.lit.append(square)
        if self._led_raises:
            raise RuntimeError("no LEDs")

    def sleep_controller(self):
        self.calls.append("sleep")
        return self._slept


@pytest.fixture
def calls():
    return []


class TestRunTeardown:
    def test_every_step_runs_in_the_order_given(self, calls):
        # The order is required: the display is released after the shutdown
        # message is drawn on it, and the serial port last of all. A runner that
        # reorders or drops steps shows up here as a different sequence.
        steps = [
            ("first", _Subsystem("first", calls).stop),
            ("second", _Subsystem("second", calls).stop),
            ("third", _Subsystem("third", calls).stop),
        ]

        assert run_teardown(steps) == []
        assert calls == ["first", "second", "third"]

    def test_a_failing_step_does_not_strand_the_ones_after_it(self, calls):
        # This is the whole point of isolating each step. Without it, a subsystem
        # that raises on stop takes the rest of shutdown with it: the controller is
        # never told to sleep and the battery drains. The failure manifests as a
        # short call list, ending at the step that raised.
        steps = [
            ("first", _Subsystem("first", calls).stop),
            ("second", _Subsystem("second", calls, raises=True).stop),
            ("third", _Subsystem("third", calls).stop),
        ]

        assert run_teardown(steps) == ["second"]
        assert calls == ["first", "second", "third"]

    def test_every_failure_is_reported_not_just_the_first(self, calls):
        # The caller logs what failed. Reporting only the first failure hides the
        # rest, and the log is the only record of why a board came back up dirty.
        steps = [
            ("first", _Subsystem("first", calls, raises=True).stop),
            ("second", _Subsystem("second", calls).stop),
            ("third", _Subsystem("third", calls, raises=True).stop),
        ]

        assert run_teardown(steps) == ["first", "third"]
        assert calls == ["first", "second", "third"]

    def test_a_subsystem_that_was_never_started_is_skipped(self, calls):
        # Most subsystems are optional: BLE, RFCOMM and the relay are absent on a
        # board that never started them, and a game's handles are absent in the
        # menu. Skipping must not count as a failure, or every ordinary shutdown
        # reports errors and the report becomes worthless.
        steps = [
            ("absent", None),
            ("present", _Subsystem("present", calls).stop),
        ]

        assert run_teardown(steps) == []
        assert calls == ["present"]

    def test_nothing_to_tear_down_is_not_a_failure(self):
        # The null case: shutdown before anything was built, which is how a boot
        # that fails early exits.
        assert run_teardown([]) == []


class TestReleasedBy:
    def test_nothing_is_released_for_a_subsystem_that_is_absent(self):
        # Absent subsystems must produce no step at all, rather than a call that
        # raises AttributeError on None inside the runner and is logged as a
        # failure of a subsystem that was never there.
        assert released_by(None, "stop") is None

    def test_the_named_teardown_is_called_with_its_keywords(self, calls):
        # The display is released with for_shutdown=True and the board with
        # leds_off=True; those keywords change what the panel and LEDs are left
        # showing. Dropping them leaves the board lit after power-off.
        subsystem = _Subsystem("display", calls)

        released_by(subsystem, "cleanup", for_shutdown=True)()

        assert calls == ["display"]
        assert subsystem.kwargs == {"for_shutdown": True}


class TestQuiesceController:
    def test_the_board_signals_off_and_then_sleeps(self):
        # The cascade runs h8 down to h1 as the visible sign that power-off was
        # accepted, and the sleep command must come after it -- a controller asleep
        # first would not light anything. Order and squares are both asserted
        # because a reversed cascade is a real regression a count would miss.
        board = _Board()

        assert quiesce_controller(board, sleep=lambda _: None) is True
        assert board.lit == LED_CASCADE_SQUARES
        assert board.lit == [7, 6, 5, 4, 3, 2, 1, 0]
        assert board.calls == ["beep:power-off", "sleep"]

    @pytest.mark.parametrize(
        "failure", [{"beep_raises": True}, {"led_raises": True}], ids=["beep", "leds"]
    )
    def test_feedback_that_fails_still_reaches_the_sleep_command(self, failure):
        # The beep and the LEDs are feedback; the sleep command is what stops the
        # battery draining while the Pi is off. If a failure in either aborts the
        # sequence, the board looks off and flattens its battery overnight -- the
        # failure is invisible until the user finds a dead board.
        board = _Board(**failure)

        assert quiesce_controller(board, sleep=lambda _: None) is True
        assert "sleep" in board.calls

    def test_a_controller_that_does_not_acknowledge_is_reported(self):
        # The caller logs that the battery may drain, which is the only warning
        # anyone gets. Reporting success unconditionally would lose it.
        board = _Board(slept=False)

        assert quiesce_controller(board, sleep=lambda _: None) is False
