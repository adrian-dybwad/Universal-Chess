"""Tests for the SyncCentaur serial debug capture.

Serial debug logging exists to diagnose boards whose discovery handshake never
completes - notably v1 boards, whose firmware power-on LED animation (spinning
circles) is only cancelled once discovery succeeds and ledsOff() is sent. When
no packet ever validates, the framed-packet logs show nothing, so the raw byte
stream is the only evidence of what the board sent. These tests verify the
capture emits the raw RX stream and TX packets only when enabled, so the
downloaded debug log actually contains the data support needs.
"""

import logging

import pytest

pytest.importorskip("serial")

from universalchess.board.sync_centaur import SyncCentaur, command


def _make_controller(serial_debug: bool) -> SyncCentaur:
    """Build a controller without touching hardware.

    auto_init=False skips the background thread that opens /dev/serial0, so the
    instance exists purely to exercise the debug-capture helpers. serial_debug
    is set explicitly because the config file is absent in the test environment.
    """
    controller = SyncCentaur(developer_mode=False, auto_init=False)
    controller.serial_debug = serial_debug
    return controller


def test_rx_bytes_logged_as_hex_row_when_enabled(caplog):
    """Received bytes must be logged in hex once a full row accumulates.

    Why this test exists: the raw inbound stream is the primary diagnostic for a
    v1 board (its responses never form a valid packet). If RX capture is broken,
    the downloaded log is empty exactly when it is needed most.

    How a regression manifests: feeding a full row of bytes produces no
    "[SERIAL RX]" line, or the hex does not match the bytes fed.
    """
    controller = _make_controller(serial_debug=True)
    row = list(range(controller.SERIAL_DEBUG_RX_ROW))  # exactly one row

    with caplog.at_level(logging.INFO, logger="universalchess.board.sync_centaur"):
        for b in row:
            controller._serial_debug_rx(b)

    rx_lines = [r.message for r in caplog.records if "[SERIAL RX]" in r.message]
    assert len(rx_lines) == 1
    expected_hex = " ".join(f"{b:02x}" for b in row)
    assert rx_lines[0] == f"[SERIAL RX] {expected_hex}"
    # The buffer is flushed, so a partial next row does not duplicate bytes.
    assert controller._rx_debug_buffer == bytearray()


def test_partial_rx_row_flushes_on_demand(caplog):
    """A partial row (board went idle) must flush via _serial_debug_flush_rx.

    Why this test exists: the typical v1 symptom is a short burst followed by
    silence; the listener flushes on its read timeout so those bytes are not
    stranded in the buffer. Asserts a sub-row burst is logged exactly once and
    only after the explicit flush.

    How a regression manifests: the partial burst is never logged, or is logged
    before the flush (row threshold misfiring on a partial row).
    """
    controller = _make_controller(serial_debug=True)
    partial = [0xAA, 0xBB, 0xCC]  # fewer than SERIAL_DEBUG_RX_ROW

    with caplog.at_level(logging.INFO, logger="universalchess.board.sync_centaur"):
        for b in partial:
            controller._serial_debug_rx(b)
        # Nothing logged yet: the row is not full.
        assert not [r for r in caplog.records if "[SERIAL RX]" in r.message]
        controller._serial_debug_flush_rx()

    rx_lines = [r.message for r in caplog.records if "[SERIAL RX]" in r.message]
    assert rx_lines == ["[SERIAL RX] aa bb cc"]


def test_no_rx_logging_when_disabled(caplog):
    """With capture off, processResponse must not emit RX debug lines.

    Why this test exists: capture is verbose and opt-in; logging when disabled
    would flood the normal debug log and defeat the switch. processResponse is
    the real entry point (the listener calls it per byte), so the gate is tested
    there, not only on the helper.

    How a regression manifests: an RX line appears even though serial_debug is
    False (the guard in processResponse was dropped).
    """
    controller = _make_controller(serial_debug=False)

    with caplog.at_level(logging.INFO, logger="universalchess.board.sync_centaur"):
        for b in (0x01, 0x02, 0x03):
            controller.processResponse(b)

    assert not [r for r in caplog.records if "[SERIAL RX]" in r.message]


def test_tx_logged_only_when_enabled(caplog):
    """_serial_debug_tx must log the labelled hex packet only when enabled.

    Why this test exists: the outbound side (discovery 0x4d/0x4e, the 0x87
    address probe) is half of the handshake; without it the log cannot show
    whether the Pi even asked the board for its address. The same helper must
    stay silent when disabled so it is safe on the hot polling path.

    How a regression manifests: no "[SERIAL TX]" line when enabled, the hex/label
    is wrong, or a line is emitted when disabled.
    """
    enabled = _make_controller(serial_debug=True)
    with caplog.at_level(logging.INFO, logger="universalchess.board.sync_centaur"):
        enabled._serial_debug_tx("discovery-init", bytes([0x4d, 0x4e]))
    tx_lines = [r.message for r in caplog.records if "[SERIAL TX]" in r.message]
    assert tx_lines == ["[SERIAL TX] discovery-init 4d 4e"]

    caplog.clear()
    disabled = _make_controller(serial_debug=False)
    with caplog.at_level(logging.INFO, logger="universalchess.board.sync_centaur"):
        disabled._serial_debug_tx(command.DGT_BUS_SEND_87, bytes([0x87, 0x00]))
    assert not [r for r in caplog.records if "[SERIAL TX]" in r.message]


@pytest.mark.parametrize(
    "raw_value,expected",
    [
        ("True", True),
        ("true", True),
        ("on", True),
        ("1", True),
        ("yes", True),
        ("False", False),
        ("off", False),
        ("0", False),
        ("", False),
        ("garbage", False),
    ],
)
def test_read_serial_debug_setting_parses_spellings(monkeypatch, raw_value, expected):
    """The flag reader must accept every truthy/falsey spelling the UI may store.

    Why this test exists: the web POST writes a Python bool ("True"/"False") via
    configparser, but a user or older config could hold on/1/yes; matching only
    one spelling silently disables capture for the others. Parameterised over the
    representations the settings layer can persist.

    How a regression manifests: a listed spelling maps to the wrong boolean
    (e.g. "on" read as False), so the switch appears on but nothing is captured.
    """
    monkeypatch.setattr(
        "universalchess.board.settings.Settings.read",
        staticmethod(lambda section, key, default="": raw_value),
    )
    assert SyncCentaur._read_serial_debug_setting() is expected


def test_read_serial_debug_setting_defaults_off_on_error(monkeypatch):
    """A failing settings read must default the flag off, never raise.

    Why this test exists: this runs inside __init__, before the board is up. A
    corrupt/missing config must not crash controller construction (that would
    break boot to read a diagnostic flag). Forces Settings.read to raise.

    How a regression manifests: the exception propagates out of
    _read_serial_debug_setting instead of returning False.
    """
    def _boom(section, key, default=""):
        raise OSError("config unavailable")

    monkeypatch.setattr(
        "universalchess.board.settings.Settings.read", staticmethod(_boom)
    )
    assert SyncCentaur._read_serial_debug_setting() is False
