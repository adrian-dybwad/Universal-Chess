"""SyncCentaur opens the board-profile UART, not a hardcoded Pi alias.

Why these tests exist:
    SyncCentaur.SERIAL_DEVICE is ``/dev/serial0``, a Raspberry Pi udev alias.
    The Orange Pi Zero 2W has no that node; the chess MCU is on ``/dev/ttyS0``,
    now free of a kernel console. Opening serial0 on that board fails; opening
    serial0 on an unknown board would guess a Pi node. _initialize must take
    the UART from the board profile and refuse when the profile has none.

How a regression manifests:
    - Orange Pi still opening serial0: FileNotFoundError on the bring-up board.
    - Unknown still opening serial0: silent talk to the wrong device later.
    - Pi opening ttyS0: every shipping Centaur loses the chess MCU link.
"""

from __future__ import annotations

import pytest

pytest.importorskip("serial")

from universalchess.board import sync_centaur
from universalchess.board.profile import (
    UnconfiguredBoardError,
    profile_for_model,
)
from universalchess.board.sync_centaur import SyncCentaur

MODEL_PI = "Raspberry Pi Zero 2 W Rev 1.0"
MODEL_ORANGEPI = "OrangePi Zero 2W"


def _stub_serial_open(monkeypatch):
    monkeypatch.setattr(sync_centaur, "heal_swapped_serial_node", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sync_centaur, "resolve_tap_device", lambda: "/dev/ttyS0")
    sync_centaur.serial.Serial.reset_mock()


def _opened_device():
    return sync_centaur.serial.Serial.call_args.args[0]


def test_initialize_opens_serial0_on_a_raspberry_pi(monkeypatch):
    # Why: shipping Centaurs talk to the MCU through /dev/serial0. A regression
    # that switched them to ttyS0 would drop the board link on every Pi.
    _stub_serial_open(monkeypatch)
    monkeypatch.setattr(
        "universalchess.board.profile.get_board_profile",
        lambda: profile_for_model(MODEL_PI),
    )
    SyncCentaur(auto_init=False)._initialize()  # noqa: SLF001
    assert _opened_device() == "/dev/serial0"


def test_initialize_opens_ttys0_on_orangepi_zero2w(monkeypatch):
    # Why: the bring-up board's MCU UART is ttyS0. Opening serial0 (missing)
    # is the failure this wiring exists to prevent.
    _stub_serial_open(monkeypatch)
    monkeypatch.setattr(
        "universalchess.board.profile.get_board_profile",
        lambda: profile_for_model(MODEL_ORANGEPI),
    )
    SyncCentaur(auto_init=False)._initialize()  # noqa: SLF001
    assert _opened_device() == "/dev/ttyS0"


def test_initialize_refuses_to_guess_uart_when_the_profile_has_none(monkeypatch):
    # Why: an unrecognized board must not inherit /dev/serial0. Guessing would
    # look like support while talking to whatever that node is on the next SoC.
    _stub_serial_open(monkeypatch)
    monkeypatch.setattr(
        "universalchess.board.profile.get_board_profile",
        lambda: profile_for_model(None),
    )
    with pytest.raises(UnconfiguredBoardError, match="UART"):
        SyncCentaur(auto_init=False)._initialize()  # noqa: SLF001
    sync_centaur.serial.Serial.assert_not_called()
