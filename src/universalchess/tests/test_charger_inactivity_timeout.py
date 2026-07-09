"""Tests for the power-source-aware inactivity timeout selector.

Background / why these tests exist
-----------------------------------
The board's auto power-off timeout depends on the power source:

* On battery, the configurable ``system.inactivity_timeout`` applies (default
  900s; 0 means disabled/infinite so a user can opt out of auto power-off).
* While the charger is connected the board must NOT run indefinitely, but it
  should tolerate a long idle period. It uses a fixed 24-hour timeout.

Previously a charging board never powered off: the events loop pushed the
deadline forward every iteration while charging (``to = now + 100000`` re-applied
each pass), so it could never elapse. ``effective_inactivity_timeout`` is the
single, pure place that decides the timeout for the current power source, so the
events loop can set the deadline once per power-source transition and let it
count down.
"""

import pytest

from universalchess.board.board import (
    CHARGER_CONNECTED_TIMEOUT,
    DISABLED_TIMEOUT_SENTINEL,
    INACTIVITY_TIMEOUT_DEFAULT,
    effective_inactivity_timeout,
)


def test_charger_connected_uses_fixed_24h_timeout():
    """While charging, the timeout is the fixed 24h value and is not disabled.

    Why: a mains-powered board must still power off eventually (24h) instead of
    running forever, which was the regression when the deadline was continually
    re-applied while charging.

    How the regression manifests: the returned timeout would be the battery
    setting (or an effectively-infinite sentinel) instead of exactly 24h, so a
    charging board never reaches its power-off.
    """
    seconds, disabled = effective_inactivity_timeout(
        charger_connected=True, configured_timeout=INACTIVITY_TIMEOUT_DEFAULT)
    assert seconds == CHARGER_CONNECTED_TIMEOUT
    assert CHARGER_CONNECTED_TIMEOUT == 86400  # 24 hours in seconds
    assert disabled is False


def test_charger_connected_overrides_disabled_battery_setting():
    """Charging enforces 24h even when the battery timeout is disabled (0).

    Why: the 24h charger cap is a safety limit for a plugged-in board and is
    intentionally independent of the battery opt-out. A user who disables the
    battery timeout still gets the charger cap.

    How the regression manifests: a 0 setting leaks through while charging and
    returns the disabled sentinel, so the board never powers off on the charger.
    """
    seconds, disabled = effective_inactivity_timeout(
        charger_connected=True, configured_timeout=0)
    assert seconds == CHARGER_CONNECTED_TIMEOUT
    assert disabled is False


def test_battery_uses_configured_timeout():
    """On battery, the configured timeout is returned verbatim and enabled.

    Why: battery behavior must be unchanged by the charger feature.

    How the regression manifests: the charger branch is taken on battery, so
    the board would use 24h (never sleeps on battery) instead of the setting.
    """
    seconds, disabled = effective_inactivity_timeout(
        charger_connected=False, configured_timeout=900)
    assert seconds == 900
    assert disabled is False


def test_battery_zero_setting_is_disabled_and_infinite():
    """On battery, a 0 setting reports disabled with an effectively-infinite value.

    Why: 0 is the user's explicit opt-out of auto power-off on battery. The
    ``disabled`` flag suppresses the countdown UI; the sentinel keeps the
    deadline far in the future so the loop never elapses it.

    How the regression manifests: a 0 setting returns 0 seconds (immediate
    power-off) or disabled=False (a countdown is shown), either of which powers
    off a user who opted out.
    """
    seconds, disabled = effective_inactivity_timeout(
        charger_connected=False, configured_timeout=0)
    assert seconds == DISABLED_TIMEOUT_SENTINEL
    assert disabled is True


@pytest.mark.parametrize("configured", [60, 300, 900, 1800])
def test_battery_passthrough_various_values(configured):
    """Any positive battery setting passes through unchanged when on battery.

    Why: guards against accidental clamping/rounding of the user's setting.

    How the regression manifests: the returned seconds differ from the input,
    so the configured battery timeout is not honored.
    """
    seconds, disabled = effective_inactivity_timeout(
        charger_connected=False, configured_timeout=configured)
    assert seconds == configured
    assert disabled is False
