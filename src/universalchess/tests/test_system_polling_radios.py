"""Tests for radio polling on boards that have no radio.

Why these tests exist
---------------------
``SystemPollingService`` polled Wi-Fi and Bluetooth every 10 seconds by shelling
out to ``rfkill``, ``iwconfig`` and ``hcitool``. On a plain Raspberry Pi Zero
(no "W") there is no wireless die at all, so those four subprocesses per cycle
are pure waste on a single 1GHz ARMv6 core -- and worse, they are *misread*:
``rfkill list wifi`` prints nothing for a radio that does not exist, and the old
parser reads "nothing blocked" as "enabled", so the status bar showed a Wi-Fi and
a Bluetooth indicator permanently stuck at "on but never connected".

The fix reports the honest state (``WIFI_ABSENT`` / ``BT_ABSENT``, which the
indicators treat as not-enabled and hide) and stops polling hardware that is not
there. These tests pin both halves: the absent state is published, and no
subprocess is ever spawned for a missing radio.
"""

import pytest

from universalchess.board.wireless_capability import WirelessCapability
from universalchess.services import system as system_service
from universalchess.state import get_system
from universalchess.state.system import (
    BT_ABSENT,
    BT_DISCONNECTED,
    WIFI_ABSENT,
    WIFI_DISCONNECTED,
)


def _capability(*, has_wifi, has_bluetooth):
    return WirelessCapability(
        has_wifi=has_wifi, has_bluetooth=has_bluetooth, pi_model="Raspberry Pi Zero Rev 1.3"
    )


@pytest.fixture
def no_subprocess(monkeypatch):
    """Fail loudly if any radio command is spawned.

    The point of the gate is that nothing is executed for a radio the board does
    not have, so the assertion belongs in the boundary itself rather than in a
    call-count check after the fact.
    """
    def _forbidden(*args, **kwargs):
        raise AssertionError(f"spawned a radio command for absent hardware: {args!r}")

    monkeypatch.setattr(system_service.subprocess, "run", _forbidden)


@pytest.fixture
def state():
    """The process-wide SystemState, restored to its neutral start values.

    The service publishes into the singleton, so the test both seeds and reads it
    here; resetting afterwards keeps a radio-absent verdict from leaking into
    unrelated tests that observe the same singleton.
    """
    current = get_system()
    current.set_wifi(WIFI_DISCONNECTED, 0, None)
    current.set_bluetooth(BT_DISCONNECTED, None, None)
    yield current
    current.set_wifi(WIFI_DISCONNECTED, 0, None)
    current.set_bluetooth(BT_DISCONNECTED, None, None)


def test_absent_wifi_is_reported_without_running_any_command(state, no_subprocess):
    # Why: the misread that put a permanent Wi-Fi indicator on a plain Zero --
    # empty rfkill output parsed as "enabled". Manifests as either the fixture
    # firing (a command was spawned for hardware that does not exist) or the state
    # not being ABSENT, which leaves the indicator visible.
    service = system_service.SystemPollingService(
        capability=_capability(has_wifi=False, has_bluetooth=True)
    )
    service._poll_wifi()

    assert state.wifi_state == WIFI_ABSENT
    assert state.wifi_enabled is False  # what the status-bar indicator hides on
    assert state.wifi_ssid is None
    assert state.wifi_signal_strength == 0


def test_absent_bluetooth_is_reported_without_running_any_command(state, no_subprocess):
    # Why: same misread on the Bluetooth side, and the same indicator symptom.
    service = system_service.SystemPollingService(
        capability=_capability(has_wifi=True, has_bluetooth=False)
    )
    service._poll_bluetooth()

    assert state.bt_state == BT_ABSENT
    assert state.bt_enabled is False
    assert state.bt_device_name is None


def test_network_thread_is_not_started_when_the_board_has_no_radios(state, no_subprocess):
    # Why: with neither radio there is nothing for the 10-second loop to observe,
    # so the thread must not exist at all rather than wake up forever doing
    # nothing on a 1GHz single core. The absent states must still be published
    # once, otherwise the indicators keep the neutral "disconnected" state they
    # start in and stay visible.
    #
    # Manifests as a live network thread (wasted wakeups) or as indicators that
    # never hide because nothing ever published the absent state.
    service = system_service.SystemPollingService(
        capability=_capability(has_wifi=False, has_bluetooth=False)
    )
    try:
        service.start()
        assert service._network_thread is None
        assert state.wifi_state == WIFI_ABSENT
        assert state.bt_state == BT_ABSENT
    finally:
        service.stop()


def test_network_thread_still_starts_when_one_radio_is_present(state):
    # Why: the gate must be per-board-capability, not an excuse to stop polling
    # everywhere. A Zero W (or a Zero with a single dongle) still needs live
    # status. Manifests as the thread missing here, which freezes the status bar
    # on every equipped board.
    service = system_service.SystemPollingService(
        capability=_capability(has_wifi=True, has_bluetooth=False)
    )
    try:
        service.start()
        assert service._network_thread is not None
        assert service._network_thread.is_alive()
    finally:
        service.stop()
