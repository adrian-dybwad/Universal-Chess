"""Which Bluetooth subsystems the board brings up at startup.

Some supported boards have no Bluetooth controller at all -- a plain Pi Zero has no
wireless die. Starting the stack there is not merely useless: the RFCOMM pairing
loop retries continuously against a missing adapter and BleManager's adapter calls
fail, burning the single ARMv6 core the board has and filling the log, with nothing
on screen to explain why it is slow.

The decision was two three-way conditions in the middle of the startup sequence,
each repeating "and the board has a controller". A subsystem added without that
term reintroduces the problem, so it is stated once here, where every combination
of the command-line flags and the hardware can be asserted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: The board has no Bluetooth controller, so nothing Bluetooth can run.
NO_CONTROLLER = "no Bluetooth controller"

#: The operator turned this transport off with --no-ble / --no-rfcomm.
DISABLED_BY_ARGUMENT = "disabled by command line argument"


@dataclass(frozen=True)
class BluetoothPlan:
    """What to start, and why anything skipped was skipped.

    The reasons are carried rather than logged here so the plan stays a pure
    decision. A skipped subsystem always has one: a silent skip in the log is how
    a board that simply has no Bluetooth comes to look like a board that broke.
    """

    start_ble: bool
    start_rfcomm: bool
    #: Whether to resolve the branded adapter alias, which reads the adapter's MAC.
    #: A property of the adapter rather than of either transport, so it is resolved
    #: whenever there is an adapter -- including with both transports turned off,
    #: which leaves a later subsystem an alias to brand itself with.
    resolve_adapter_alias: bool
    ble_skipped_because: Optional[str] = None
    rfcomm_skipped_because: Optional[str] = None


def plan_bluetooth(
    *, has_controller: bool, ble_requested: bool, rfcomm_requested: bool
) -> BluetoothPlan:
    """Decide what to bring up from the hardware and the command-line flags.

    A transport starts only when both the hardware is present and the operator
    asked for it. With no controller the flags are beside the point, so the missing
    hardware is the reason reported: an operator reading "disabled by command line
    argument" on a board they passed no arguments to would go looking in the wrong
    place.
    """
    if not has_controller:
        return BluetoothPlan(
            start_ble=False,
            start_rfcomm=False,
            resolve_adapter_alias=False,
            ble_skipped_because=NO_CONTROLLER,
            rfcomm_skipped_because=NO_CONTROLLER,
        )

    return BluetoothPlan(
        start_ble=ble_requested,
        start_rfcomm=rfcomm_requested,
        resolve_adapter_alias=True,
        ble_skipped_because=None if ble_requested else DISABLED_BY_ARGUMENT,
        rfcomm_skipped_because=None if rfcomm_requested else DISABLED_BY_ARGUMENT,
    )
