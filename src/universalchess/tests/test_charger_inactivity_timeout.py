"""Tests for the power-source-aware inactivity timeout selector.

Background / why these tests exist
-----------------------------------
The board's auto power-off timeout depends on where its power comes from:

* On battery, the configurable ``system.inactivity_timeout`` applies (default
  900s; 0 means disabled/infinite so a user can opt out of auto power-off).
* While the charger is connected the board must NOT run indefinitely, but it
  should tolerate a long idle period. It uses a fixed 24-hour timeout.
* When the power source is not known, nothing is powered off.

Previously a charging board never powered off: the events loop pushed the
deadline forward every iteration while charging (``to = now + 100000`` re-applied
each pass), so it could never elapse. ``effective_inactivity_timeout`` is the
single, pure place that decides the timeout for the current power source, so the
events loop can set the deadline once per power-source transition and let it
count down.

Why "unknown" is a state and not a default
------------------------------------------
The power source is only known because the baseboard answers
``DGT_SEND_BATTERY_INFO`` every five seconds. When it does not answer -- there is
no baseboard attached, or the serial link is down -- the poll returns early and
nothing is recorded.

That silence used to be indistinguishable from "the baseboard says the charger is
unplugged", because the charger flag was a plain bool initialised to False. A
mains-powered Raspberry Pi with no battery at all therefore took the battery
branch and powered itself off after fifteen idle minutes, which is a
battery-saving measure applied to a board with no battery to save. It also killed
any engine install running at the time.

This is the same distinction the module already draws for radios, where
``WIFI_ABSENT`` exists precisely because an absent radio is not a disabled one.

Silence after a successful reading is treated differently: the last known source
persists. A board that reported "on battery" and then lost its link really might
be running on a battery, and refusing to ever power off would flatten it. Only a
board that has never reported at all is unknown.
"""

import pytest

from universalchess.board.board import (
    CHARGER_CONNECTED_TIMEOUT,
    DISABLED_TIMEOUT_SENTINEL,
    INACTIVITY_TIMEOUT_DEFAULT,
    effective_inactivity_timeout,
)
from universalchess.state.system import (
    POWER_BATTERY,
    POWER_CHARGER,
    POWER_UNKNOWN,
    SystemState,
)

# A battery reading as the controller reports it: level in the low bits, charger
# state in bits 5-7. 0x12 is level 18, charger state 0 (not charging).
BATTERY_PACKET_DISCHARGING = 0x12
BATTERY_LEVEL = 18


class TestChoosingTheTimeout:
    """The pure selector, one case per power source."""

    def test_charger_connected_uses_fixed_24h_timeout(self):
        """While charging, the timeout is the fixed 24h value and is not disabled.

        Why: a mains-powered board must still power off eventually (24h) instead of
        running forever, which was the regression when the deadline was continually
        re-applied while charging.

        How the regression manifests: the returned timeout would be the battery
        setting (or an effectively-infinite sentinel) instead of exactly 24h, so a
        charging board never reaches its power-off.
        """
        seconds, disabled = effective_inactivity_timeout(
            power_source=POWER_CHARGER, configured_timeout=INACTIVITY_TIMEOUT_DEFAULT)
        assert seconds == CHARGER_CONNECTED_TIMEOUT
        assert CHARGER_CONNECTED_TIMEOUT == 86400  # 24 hours in seconds
        assert disabled is False

    def test_charger_connected_overrides_disabled_battery_setting(self):
        """Charging enforces 24h even when the battery timeout is disabled (0).

        Why: the 24h charger cap is a safety limit for a plugged-in board and is
        intentionally independent of the battery opt-out. A user who disables the
        battery timeout still gets the charger cap.

        How the regression manifests: a 0 setting leaks through while charging and
        returns the disabled sentinel, so the board never powers off on the charger.
        """
        seconds, disabled = effective_inactivity_timeout(
            power_source=POWER_CHARGER, configured_timeout=0)
        assert seconds == CHARGER_CONNECTED_TIMEOUT
        assert disabled is False

    def test_battery_uses_configured_timeout(self):
        """On battery, the configured timeout is returned verbatim and enabled.

        Why: battery behavior must be unchanged by the charger and unknown cases.

        How the regression manifests: another branch is taken on battery, so the
        board would use 24h or never sleep at all, draining the battery it exists
        to protect.
        """
        seconds, disabled = effective_inactivity_timeout(
            power_source=POWER_BATTERY, configured_timeout=900)
        assert seconds == 900
        assert disabled is False

    def test_battery_zero_setting_is_disabled_and_infinite(self):
        """On battery, a 0 setting reports disabled with an effectively-infinite value.

        Why: 0 is the user's explicit opt-out of auto power-off on battery. The
        ``disabled`` flag suppresses the countdown UI; the sentinel keeps the
        deadline far in the future so the loop never elapses it.

        How the regression manifests: a 0 setting returns 0 seconds (immediate
        power-off) or disabled=False (a countdown is shown), either of which powers
        off a user who opted out.
        """
        seconds, disabled = effective_inactivity_timeout(
            power_source=POWER_BATTERY, configured_timeout=0)
        assert seconds == DISABLED_TIMEOUT_SENTINEL
        assert disabled is True

    @pytest.mark.parametrize("configured", [60, 300, 900, 1800])
    def test_battery_passthrough_various_values(self, configured):
        """Any positive battery setting passes through unchanged when on battery.

        Why: guards against accidental clamping/rounding of the user's setting.

        How the regression manifests: the returned seconds differ from the input,
        so the configured battery timeout is not honored.
        """
        seconds, disabled = effective_inactivity_timeout(
            power_source=POWER_BATTERY, configured_timeout=configured)
        assert seconds == configured
        assert disabled is False

    def test_an_unknown_power_source_never_powers_off(self):
        """With no idea where the power comes from, nothing is powered off.

        Why: the only reason to power off an idle board is to save its battery,
        and a board that has never reported a battery may not have one. This is
        the case that powered off a mains-fed Pi with no baseboard attached after
        fifteen minutes, taking any running engine install with it.

        How the regression manifests: the unknown source falls through to the
        battery branch and a board with no battery gets a battery-saving
        power-off.
        """
        seconds, disabled = effective_inactivity_timeout(
            power_source=POWER_UNKNOWN, configured_timeout=INACTIVITY_TIMEOUT_DEFAULT)
        assert seconds == DISABLED_TIMEOUT_SENTINEL
        assert disabled is True

    @pytest.mark.parametrize("configured", [0, 60, 900, 86400])
    def test_the_battery_setting_cannot_re_enable_an_unknown_source(self, configured):
        """No battery setting brings the power-off back for an unknown source.

        Why: the setting describes what to do *on battery*. Letting it apply to a
        source that was never identified is how the original defect worked -- the
        default 900 was applied to a board that had never said it had a battery.

        How the regression manifests: the guard is written as a special case of
        the battery branch, so a configured value reaches it and the power-off
        returns.
        """
        seconds, disabled = effective_inactivity_timeout(
            power_source=POWER_UNKNOWN, configured_timeout=configured)
        assert seconds == DISABLED_TIMEOUT_SENTINEL
        assert disabled is True


class TestKnowingThePowerSource:
    """What the shared system state reports, and when."""

    def test_a_board_that_has_never_reported_is_unknown(self):
        """Before any battery reading the source is unknown, not battery.

        Why: this is the initial value every process starts with, and the one the
        events loop reads on a board whose baseboard never answers. Defaulting it
        to "on battery" is a fabricated reading that looks exactly like a real
        one, which is why the original defect was invisible.

        How the regression manifests: a fresh state claims to be on battery and
        the fifteen-minute power-off arms itself against a board that may be
        mains-powered.
        """
        assert SystemState().power_source == POWER_UNKNOWN

    @pytest.mark.parametrize("charger_connected,expected", [
        (False, POWER_BATTERY),
        (True, POWER_CHARGER),
    ])
    def test_a_reading_establishes_the_source(self, charger_connected, expected):
        """A successful reading resolves the source either way.

        Why: the unknown state must be genuinely transitional. If a reading did
        not clear it, a board on battery would never power off and would run
        itself flat.

        How the regression manifests: the source stays unknown after the
        baseboard reports, so the battery timeout never applies to a real
        battery.
        """
        state = SystemState()

        state.set_battery(BATTERY_LEVEL, charger_connected)

        assert state.power_source == expected

    def test_the_source_survives_the_baseboard_going_quiet(self):
        """A board that reported and then went silent keeps its last known source.

        Why: silence after a reading is not the same as silence from the start. A
        board that said "on battery" and then lost its serial link really might be
        running on a battery, so it must keep powering off; only a board that has
        never reported at all is genuinely unknown. The poll simply stops calling
        in, so this asserts the state does not decay on its own.

        How the regression manifests: the source reverts to unknown when readings
        stop, and a board on battery with a flaky link runs until it is flat.
        """
        state = SystemState()
        state.set_battery(BATTERY_LEVEL, charger_connected=False)

        # No further readings arrive; the poll returns early and records nothing.

        assert state.power_source == POWER_BATTERY

    def test_unplugging_the_charger_moves_the_source_back_to_battery(self):
        """The source follows the hardware in both directions.

        Why: the events loop switches its deadline on the transition, so a source
        that only ever moved one way would leave a board on battery holding the
        24-hour charger deadline.

        How the regression manifests: unplugging leaves the board on the charging
        deadline and it never powers off on battery.
        """
        state = SystemState()
        state.set_battery(BATTERY_LEVEL, charger_connected=True)

        state.set_battery(BATTERY_LEVEL, charger_connected=False)

        assert state.power_source == POWER_BATTERY

    def test_the_charger_flag_stays_false_while_the_source_is_unknown(self):
        """Display code still sees "not charging" before anything is known.

        Why: the status bar draws a charging bolt from this flag, and an unknown
        source must not draw one. Keeping the flag a plain bool means the
        indicators are unchanged by the new state; only the power-off decision
        reads the three-way source.

        How the regression manifests: the flag becomes tri-state and every
        ``if charger_connected`` consumer -- battery icon, splash screen, the
        Pegasus emulator, the web status payload -- silently changes behaviour.
        """
        state = SystemState()

        assert state.charger_connected is False
        assert state.power_source == POWER_UNKNOWN


class TestWhatThePollerRecords:
    """The boundary where a silent baseboard becomes an unknown power source."""

    @pytest.fixture
    def state(self, monkeypatch):
        """Bind a fresh SystemState to the polling service."""
        state = SystemState()
        monkeypatch.setattr(
            "universalchess.services.system.get_system", lambda: state
        )
        return state

    @staticmethod
    def _poll_with(monkeypatch, response):
        """Run one battery poll against a controller returning ``response``."""
        from types import SimpleNamespace

        from universalchess.services.system import SystemPollingService

        monkeypatch.setattr(
            "universalchess.board.board.controller",
            SimpleNamespace(request_response=lambda _command: response),
            raising=False,
        )
        SystemPollingService()._poll_battery()

    def test_a_timed_out_battery_request_records_nothing(self, state, monkeypatch):
        """A request that times out leaves the source unknown.

        Why: this is the defect as it actually occurred. The board logged
        "Request timeout for DGT_SEND_BATTERY_INFO" every five seconds, the poll
        returned early, and the never-written charger flag read as "on battery",
        so a mains-powered board powered itself off mid-install.

        How the regression manifests: the poller invents a reading from a failed
        request, and the board acts on a battery state no hardware ever reported.
        """
        self._poll_with(monkeypatch, None)

        assert state.power_source == POWER_UNKNOWN
        assert state.battery_level is None

    def test_an_empty_response_records_nothing(self, state, monkeypatch):
        """A truncated reply is treated as no reply.

        Why: the poll indexes ``resp[0]``, so an empty response is unusable. It is
        the same absence of information as a timeout and must reach the same
        state rather than raising on the polling thread.

        How the regression manifests: an IndexError kills the battery thread, so
        the source is frozen for the rest of the session.
        """
        self._poll_with(monkeypatch, b"")

        assert state.power_source == POWER_UNKNOWN

    def test_a_real_reading_is_recorded(self, state, monkeypatch):
        """A successful reply resolves the source and the level.

        Why: the null case for the two above -- proof the poll still works and
        that the tests are not passing because nothing is ever recorded. The
        packet is the one this board actually reported: level 18, not charging.

        How the regression manifests: readings stop being recorded at all and
        every board looks unknown, so nothing ever powers off.
        """
        self._poll_with(monkeypatch, bytes([BATTERY_PACKET_DISCHARGING]))

        assert state.power_source == POWER_BATTERY
        assert state.battery_level == BATTERY_LEVEL

    def test_a_reading_then_a_timeout_keeps_the_reading(self, state, monkeypatch):
        """Losing the link after a reading does not erase what was learned.

        Why: the counterpart to the unknown case, at the boundary where it is
        decided. A board on battery whose link drops must keep its battery
        deadline; only a board that never reported is exempt from powering off.

        How the regression manifests: an intermittent serial link flips the board
        between "on battery" and "never powers off", and an idle board on battery
        runs itself flat whenever the last poll happened to fail.
        """
        self._poll_with(monkeypatch, bytes([BATTERY_PACKET_DISCHARGING]))

        self._poll_with(monkeypatch, None)

        assert state.power_source == POWER_BATTERY
        assert state.battery_level == BATTERY_LEVEL
