#!/usr/bin/env python3
"""Tests for e-paper panel power management (light-sensitivity hardening).

Background / why these tests exist
----------------------------------
The Waveshare 2.9" panel is a self-contained device: its electrical state is
whatever the last SPI command left it in. Powering off the Pi or stopping the
app does NOT change the panel state. Two distinct light-sensitivity failure
modes were observed and are guarded here:

(a) Active bias during use. Before the fix, every refresh ended with
    TurnOnDisplay() = 0x12 (refresh) only, leaving the panel's DC-DC booster and
    source/gate drivers powered. Under bright/IR light the biased TFTs leak and
    the image darkens. Fix: issue 0x02 (power off) after the refresh settles, and
    wake (0x04) before each draw path.

(b) Un-settled pixels even when unpowered. If the panel is never given the
    VCOM-settle + deep-sleep sequence, the pixels sit in a metastable charge
    state that drifts dark under light with no controller to correct it. Fix: when
    the display goes idle, issue the full settle/deep-sleep sequence
    (0x50->0xf7, 0x02, 0x07/0xA5) so it is robust even across a hard power cut.

These tests assert the exact command sequences (a regression that drops 0x02 or
0x04, or scrambles the idle sequence, re-introduces the darkening). They also
assert the scheduler enters idle-sleep after inactivity and fully wakes
(reset+init) before the next draw, while NOT re-initialising during normal
back-to-back navigation (which would make the UI slow/flashy).
"""

import time
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

from universalchess.epaper.framework.waveshare.epd2in9d import EPD
from universalchess.epaper.framework.scheduler import Scheduler

# Panel framebuffer length in bytes: 128 * 296 / 8.
BUF_LEN = int(128 * 296 / 8)

# Command/data opcodes referenced by the protocol under test.
CMD_POWER_ON = 0x04
CMD_DATA_OLD = 0x10
CMD_DATA_NEW = 0x13
CMD_REFRESH = 0x12
CMD_POWER_OFF = 0x02
CMD_DEEP_SLEEP = 0x07
CMD_VCOM_INTERVAL = 0x50
DATA_VCOM_SETTLE = 0xf7
DATA_DEEP_SLEEP = 0xA5


def _instrument(epd):
    """Replace the SPI-touching primitives with recorders.

    Returns a trace list of ('C', byte) for commands and ('D', byte) for single
    data bytes, in call order. send_data2 (bulk image), ReadBusy (busy-wait) and
    reset (GPIO toggling) are stubbed so the trace contains only the protocol
    opcodes we assert on. reset() is recorded as ('RESET',) because waking from
    deep sleep depends on it.
    """
    trace = []
    epd.send_command = lambda c: trace.append(('C', c))
    epd.send_data = lambda d: trace.append(('D', d))
    epd.send_data2 = lambda d: trace.append(('D2', len(d)))
    epd.ReadBusy = lambda: None
    epd.reset = lambda: trace.append(('RESET',))
    return trace


def _commands(trace):
    """Extract just the command opcodes from a trace, in order."""
    return [b for (kind, b) in trace if kind == 'C']


# ---------------------------------------------------------------------------
# Driver-level tests: per-refresh parking (mechanism a)
# ---------------------------------------------------------------------------

def test_turn_on_display_parks_panel_after_refresh():
    """TurnOnDisplay must follow the refresh (0x12) with a power-off (0x02).

    Guards mechanism (a). If the 0x02 is dropped, the panel is left biased after
    every refresh and darkens under bright/IR light during use. The failure
    manifests as the trace ending on 0x12 with no 0x02.
    """
    epd = EPD()
    trace = _instrument(epd)

    epd.TurnOnDisplay()

    assert _commands(trace) == [CMD_REFRESH, CMD_POWER_OFF]


def test_display_wakes_then_parks():
    """Full display() must power on (0x04) first and power off (0x02) last.

    Guards that the full-refresh path wakes a parked/asleep panel before driving
    it and re-parks afterwards. Failure (missing 0x04) manifests as a write to an
    unpowered panel (blank/garbled); missing 0x02 leaves it light-sensitive.
    """
    epd = EPD()
    trace = _instrument(epd)

    epd.display([0x00] * BUF_LEN)

    cmds = _commands(trace)
    assert cmds[0] == CMD_POWER_ON, "display() must power on before writing RAM"
    assert cmds[-1] == CMD_POWER_OFF, "display() must park the panel after refresh"
    assert cmds.index(CMD_POWER_ON) < cmds.index(CMD_REFRESH) < cmds.index(CMD_POWER_OFF)


def test_clear_wakes_then_parks():
    """Clear() must power on (0x04) first and power off (0x02) last.

    Same contract as display(): Clear() runs on transitions/wake and must not
    assume the panel is already powered, and must re-park it afterwards.
    """
    epd = EPD()
    trace = _instrument(epd)

    epd.Clear()

    cmds = _commands(trace)
    assert cmds[0] == CMD_POWER_ON, "Clear() must power on before writing RAM"
    assert cmds[-1] == CMD_POWER_OFF, "Clear() must park the panel after refresh"


def test_display_partial_wakes_then_parks():
    """DisplayPartial must power on before the refresh and park (0x02) after.

    The partial path powers on via SetPartReg (0x04). This guards that a parked
    panel (powered off by the previous refresh) is woken before the partial
    refresh and re-parked afterwards, so steady-state navigation never leaves the
    panel biased. Failure manifests as no 0x04 before 0x12, or no trailing 0x02.
    """
    epd = EPD()
    trace = _instrument(epd)

    epd.DisplayPartial([0x00] * BUF_LEN)

    cmds = _commands(trace)
    assert CMD_POWER_ON in cmds, "partial refresh must power on (0x04) the panel"
    assert cmds[-1] == CMD_POWER_OFF, "partial refresh must park the panel after refresh"
    assert cmds.index(CMD_POWER_ON) < cmds.index(CMD_REFRESH) < (len(cmds) - 1)


# ---------------------------------------------------------------------------
# Driver-level tests: idle deep-sleep (mechanism b)
# ---------------------------------------------------------------------------

def test_idle_sleep_emits_full_settle_sequence():
    """idle_sleep() must emit VCOM-settle + power-off + deep-sleep, in order.

    Guards mechanism (b): the exact proven sequence that settles the pixels into a
    stable bistable state and parks the controller in deep sleep, so the image is
    robust against light even after a hard power cut. A regression that reorders
    or drops any opcode (e.g. sending only 0x02) re-introduces light-drift when
    the device is powered off un-settled.
    """
    epd = EPD()
    trace = _instrument(epd)

    epd.idle_sleep()

    assert trace == [
        ('C', CMD_VCOM_INTERVAL),
        ('D', DATA_VCOM_SETTLE),
        ('C', CMD_POWER_OFF),
        ('C', CMD_DEEP_SLEEP),
        ('D', DATA_DEEP_SLEEP),
    ]


def test_idle_sleep_keeps_spi_open():
    """idle_sleep() must NOT call module_exit (keep SPI/GPIO open).

    Waking from idle reuses the existing init()-based transition (which the
    scheduler already calls on full<->partial transitions without reopening SPI).
    If idle_sleep closed SPI via module_exit, that wake path would operate on a
    closed device and the shutdown path could double-close. Failure manifests as
    module_exit being called here.
    """
    epd = EPD()
    _instrument(epd)

    with patch(
        "universalchess.epaper.framework.waveshare.epd2in9d.epdconfig.module_exit"
    ) as mock_exit:
        epd.idle_sleep()

    mock_exit.assert_not_called()


# ---------------------------------------------------------------------------
# Scheduler-level tests: idle detection
# ---------------------------------------------------------------------------

def _make_scheduler():
    """Build a Scheduler with mock framebuffer + mock EPD (no real hardware/threads)."""
    framebuffer = MagicMock()
    epd = MagicMock()
    return Scheduler(framebuffer=framebuffer, epd=epd), epd


def test_should_idle_sleep_after_timeout():
    """Idle-sleep is due once inactivity exceeds the configured threshold.

    Guards the trigger condition. Failure (wrong comparison) means the panel
    never parks while idle and stays light-sensitive when the user walks away.
    """
    sched, _ = _make_scheduler()
    now = time.monotonic()
    sched._last_activity = now - (sched._idle_sleep_seconds + 1.0)
    sched._deep_asleep = False

    assert sched._should_idle_sleep(now) is True


def test_should_not_idle_sleep_before_timeout():
    """No idle-sleep while activity is recent.

    Guards against parking the panel mid-interaction (which would force a slow
    reset+Clear wake on the very next keypress).
    """
    sched, _ = _make_scheduler()
    now = time.monotonic()
    sched._last_activity = now  # just refreshed
    sched._deep_asleep = False

    assert sched._should_idle_sleep(now) is False


def test_should_not_idle_sleep_when_never_refreshed():
    """No idle-sleep before the very first refresh.

    _last_activity is None at startup; parking before the first draw would put a
    never-initialised panel to sleep. Failure manifests as idle_sleep firing on a
    fresh, un-refreshed scheduler.
    """
    sched, _ = _make_scheduler()
    sched._last_activity = None
    sched._deep_asleep = False

    assert sched._should_idle_sleep(time.monotonic()) is False


def test_should_not_idle_sleep_when_already_asleep():
    """No repeated idle-sleep once already parked.

    Guards against re-sending the deep-sleep sequence every loop tick (which would
    spam SPI). Failure manifests as _should_idle_sleep staying True after parking.
    """
    sched, _ = _make_scheduler()
    sched._last_activity = time.monotonic() - 1000.0
    sched._deep_asleep = True

    assert sched._should_idle_sleep(time.monotonic()) is False


def test_enter_idle_sleep_parks_and_sets_state():
    """_enter_idle_sleep drives the panel into deep sleep and records the state.

    Asserts the driver call AND the bookkeeping that forces the next draw to
    re-init: _deep_asleep True, _in_partial_mode False, partial count reset.
    Failure in any of these would either skip the wake (blank panel) or skip the
    park (light-sensitive).
    """
    sched, epd = _make_scheduler()
    sched._partial_refresh_count = 17
    sched._in_partial_mode = True

    sched._enter_idle_sleep()

    epd.idle_sleep.assert_called_once()
    assert sched._deep_asleep is True
    assert sched._in_partial_mode is False
    assert sched._partial_refresh_count == 0


# ---------------------------------------------------------------------------
# Scheduler-level tests: wake-on-draw
# ---------------------------------------------------------------------------

def test_partial_refresh_wakes_from_deep_sleep():
    """A partial refresh after idle-sleep must reset+init+Clear before drawing.

    Waking from deep sleep (0x07/0xA5) requires a hardware reset, performed by
    init(). Failure (drawing without init) writes to a deep-asleep panel -> the
    pieces-on-blank-board symptom. Asserts init+Clear+DisplayPartial all run and
    the asleep flag clears.
    """
    sched, epd = _make_scheduler()
    sched._deep_asleep = True
    sched._in_partial_mode = False
    future = Future()

    sched._execute_partial_refresh_single(False, future, image=MagicMock())

    epd.init.assert_called()
    epd.Clear.assert_called()
    epd.DisplayPartial.assert_called_once()
    assert sched._deep_asleep is False
    assert sched._in_partial_mode is True


def test_full_refresh_wakes_from_deep_sleep():
    """A full refresh after idle-sleep must init() (reset) before display().

    The full path normally only re-inits when leaving partial mode; after idle
    sleep it must also init to exit deep sleep. Failure (no init) writes to a
    deep-asleep panel. Asserts init+display run and the asleep flag clears.
    """
    sched, epd = _make_scheduler()
    sched._deep_asleep = True
    sched._in_partial_mode = False
    future = Future()

    sched._execute_full_refresh_single(True, future, image=MagicMock())

    epd.init.assert_called()
    epd.display.assert_called_once()
    assert sched._deep_asleep is False


def test_normal_partial_does_not_reinit_when_already_partial():
    """Steady-state navigation must NOT init()/Clear() each partial refresh.

    This is the performance contract: once in partial mode and awake, consecutive
    partial refreshes go straight to DisplayPartial. A regression that re-inits
    every time would make navigation slow and flashy (the cumbersome behaviour we
    explicitly avoided). Asserts init/Clear are not called.
    """
    sched, epd = _make_scheduler()
    sched._deep_asleep = False
    sched._in_partial_mode = True
    future = Future()

    sched._execute_partial_refresh_single(False, future, image=MagicMock())

    epd.init.assert_not_called()
    epd.Clear.assert_not_called()
    epd.DisplayPartial.assert_called_once()


def test_refresh_marks_activity_for_idle_tracking():
    """Each refresh updates the activity timestamp used for idle detection.

    Without this, _last_activity never advances and the idle timer would fire a
    fixed time after startup regardless of use. Asserts a partial refresh sets a
    numeric _last_activity.
    """
    sched, epd = _make_scheduler()
    sched._last_activity = None
    sched._deep_asleep = False
    sched._in_partial_mode = True
    future = Future()

    sched._execute_partial_refresh_single(False, future, image=MagicMock())

    assert isinstance(sched._last_activity, float)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
