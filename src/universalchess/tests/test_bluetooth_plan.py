"""Tests for deciding which Bluetooth subsystems the board brings up.

The decision was two three-way conditions in the middle of a 990-line startup,
each repeating "and the board has a controller". A subsystem added without that
term starts against a missing adapter: the RFCOMM pairing loop then retries
continuously, burning the single ARMv6 core a Pi Zero has and filling the log,
with nothing on screen to say why the board is slow. The condition is now in one
place, and every combination of the two flags and the controller is asserted.
"""

import itertools

import pytest

from universalchess.app.bluetooth_plan import (
    DISABLED_BY_ARGUMENT,
    NO_CONTROLLER,
    plan_bluetooth,
)

BOTH_FLAGS = list(itertools.product([True, False], repeat=2))
EVERY_COMBINATION = [
    (controller, ble, rfcomm)
    for controller in (True, False)
    for ble, rfcomm in BOTH_FLAGS
]


class TestABoardWithAController:
    def test_both_subsystems_start_when_both_are_asked_for(self):
        # The ordinary board. The adapter alias is resolved here too, and only here,
        # because resolving it reads the adapter's own MAC.
        plan = plan_bluetooth(has_controller=True, ble_requested=True, rfcomm_requested=True)

        assert plan.start_ble
        assert plan.start_rfcomm
        assert plan.resolve_adapter_alias
        assert plan.ble_skipped_because is None
        assert plan.rfcomm_skipped_because is None

    def test_no_ble_leaves_rfcomm_running(self):
        # --no-ble is for testing the classic transport on its own, so RFCOMM must
        # be unaffected. The reason names the argument, since the hardware is fine.
        plan = plan_bluetooth(has_controller=True, ble_requested=False, rfcomm_requested=True)

        assert not plan.start_ble
        assert plan.start_rfcomm
        assert plan.ble_skipped_because == DISABLED_BY_ARGUMENT

    def test_no_rfcomm_leaves_ble_running(self):
        # The mirror of the above, and the case that had no log branch at all: a
        # board started with --no-rfcomm said nothing about RFCOMM, so its absence
        # looked identical to a failure to start.
        plan = plan_bluetooth(has_controller=True, ble_requested=True, rfcomm_requested=False)

        assert plan.start_ble
        assert not plan.start_rfcomm
        assert plan.rfcomm_skipped_because == DISABLED_BY_ARGUMENT

    def test_the_alias_is_still_resolved_with_both_transports_off(self):
        # The alias is a property of the adapter, not of either transport, and is
        # cheap to resolve. Tying it to a transport would leave a later subsystem
        # with no alias to brand itself with.
        plan = plan_bluetooth(has_controller=True, ble_requested=False, rfcomm_requested=False)

        assert plan.resolve_adapter_alias


class TestABoardWithNoController:
    @pytest.mark.parametrize(("ble", "rfcomm"), BOTH_FLAGS)
    def test_nothing_bluetooth_is_attempted_whatever_the_flags_say(self, ble, rfcomm):
        # The reason this exists. A plain Pi Zero has no controller, and the flags
        # default to "enabled" -- so without this, every such board runs the pairing
        # retry loop on its only core forever. The alias is skipped for the same
        # reason: reading the adapter MAC has no adapter to read.
        plan = plan_bluetooth(
            has_controller=False, ble_requested=ble, rfcomm_requested=rfcomm
        )

        assert not plan.start_ble
        assert not plan.start_rfcomm
        assert not plan.resolve_adapter_alias

    def test_the_missing_controller_is_the_reason_given(self):
        # With no controller the flag is beside the point, so the log must say the
        # hardware -- an operator reading "disabled by command line argument" on a
        # board they passed no arguments to would go looking in the wrong place.
        plan = plan_bluetooth(
            has_controller=False, ble_requested=False, rfcomm_requested=False
        )

        assert plan.ble_skipped_because == NO_CONTROLLER
        assert plan.rfcomm_skipped_because == NO_CONTROLLER


class TestEveryCombination:
    @pytest.mark.parametrize(("controller", "ble", "rfcomm"), EVERY_COMBINATION)
    def test_a_reason_is_recorded_exactly_when_a_subsystem_is_skipped(
        self, controller, ble, rfcomm
    ):
        # The structural invariant across all eight cases: a subsystem that does not
        # start always says why, and one that starts never claims to have been
        # skipped. Either half breaking leaves a silent skip in the log, which is
        # how a board that quietly has no Bluetooth looks like a board that broke.
        plan = plan_bluetooth(
            has_controller=controller, ble_requested=ble, rfcomm_requested=rfcomm
        )

        assert plan.start_ble is (plan.ble_skipped_because is None)
        assert plan.start_rfcomm is (plan.rfcomm_skipped_because is None)

    @pytest.mark.parametrize(("controller", "ble", "rfcomm"), EVERY_COMBINATION)
    def test_a_subsystem_never_starts_without_both_the_flag_and_the_hardware(
        self, controller, ble, rfcomm
    ):
        # Stated as the requirement rather than as the implementation, so a plan
        # that starts something on either condition alone fails here.
        plan = plan_bluetooth(
            has_controller=controller, ble_requested=ble, rfcomm_requested=rfcomm
        )

        assert plan.start_ble == (controller and ble)
        assert plan.start_rfcomm == (controller and rfcomm)
