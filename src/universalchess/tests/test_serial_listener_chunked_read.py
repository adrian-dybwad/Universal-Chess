"""Tests that the serial listener drains buffered bytes per burst, not per byte.

Why these tests exist:
    The board streams continuously at 1 Mbps. The listener used to call
    ``self.ser.read(1)`` once per byte, paying a syscall + Python-call cost for
    every byte; on a single armv6 core that made ``_listener_thread`` the top CPU
    consumer (~9%). The fix reads the first byte (blocking, honoring the port
    timeout) and then drains whatever is already buffered in a single
    ``read(in_waiting)`` call, feeding each byte to ``processResponse`` exactly as
    before. Parsing is byte-identical; only the number of read() calls changes.

How a regression manifests:
    Reverting to per-byte reads makes ``test_listener_drains_buffered_burst``
    fail two ways: the second read no longer requests ``in_waiting`` bytes (the
    recorded read sizes become ``[1, 1]`` instead of ``[1, N-1]``) and the bytes
    handed to ``processResponse`` no longer match the full burst in order. The
    idle-path test guards that an empty (timed-out) read still flushes the serial
    debug buffer and never calls ``processResponse``.
"""

import pytest

pytest.importorskip("serial")

from universalchess.board.sync_centaur import SyncCentaur


def _make_controller(serial_debug: bool = False) -> SyncCentaur:
    """Build a controller without touching hardware.

    auto_init=False skips the background thread that opens /dev/serial0, leaving
    a bare instance whose _listener_thread can be driven against a fake port.
    """
    controller = SyncCentaur(developer_mode=False, auto_init=False)
    controller.serial_debug = serial_debug
    return controller


class _BurstSerial:
    """Fake serial that delivers one buffered burst, then stops the listener.

    read(1) returns the first byte; ``in_waiting`` reports the remaining buffered
    count; the second read returns that remainder in one call and flips
    ``listener_running`` off so the loop exits after exactly one burst. Records
    every requested read size so the test can prove the drain happened in a
    single call rather than byte by byte.
    """

    def __init__(self, controller: SyncCentaur, burst: bytes):
        self.is_open = True
        self._controller = controller
        self._burst = burst
        self._stage = 0
        self.read_sizes = []

    @property
    def in_waiting(self):
        # Everything after the first byte is already buffered.
        return len(self._burst) - 1

    def read(self, size):
        self.read_sizes.append(size)
        if self._stage == 0:
            self._stage = 1
            return self._burst[:1]
        # Second read drains the buffered remainder and ends the loop.
        self._controller.listener_running = False
        return self._burst[1:]


class _IdleSerial:
    """Fake serial whose read times out (returns b''), then stops the listener."""

    def __init__(self, controller: SyncCentaur):
        self.is_open = True
        self._controller = controller

    @property
    def in_waiting(self):
        return 0

    def read(self, size):
        self._controller.listener_running = False
        return b''


def test_listener_drains_buffered_burst_in_one_read():
    # A 5-byte burst with N-1 == 4 buffered bytes makes the drained read size (4)
    # distinguishable from the per-byte size (1), so a revert to read(1)-per-byte
    # is caught by the read-size assertion below.
    burst = bytes([0x85, 0x00, 0x10, 0x20, 0x7F])
    controller = _make_controller(serial_debug=False)
    controller.ser = _BurstSerial(controller, burst)

    processed = []
    controller.processResponse = lambda b: processed.append(b)

    controller._listener_thread()

    # Every byte must reach processResponse exactly once, in order: catches
    # dropped bytes (count), reordering, and duplication from a bad drain.
    assert processed == list(burst)
    # Chunked drain: one blocking read(1) for the first byte, then a single
    # read(in_waiting) for the remaining 4. Per-byte reads would record [1, 1].
    assert controller.ser.read_sizes == [1, len(burst) - 1]


def test_listener_idle_timeout_flushes_debug_and_skips_processing():
    # Guards the idle path the drain change must preserve: a timed-out read
    # (board silent) flushes any buffered debug bytes and never parses a byte.
    controller = _make_controller(serial_debug=True)
    controller.ser = _IdleSerial(controller)

    flushes = []
    controller._serial_debug_flush_rx = lambda: flushes.append(True)
    controller.processResponse = lambda b: (_ for _ in ()).throw(
        AssertionError("processResponse must not be called on an idle read")
    )

    controller._listener_thread()

    # Exactly one flush for the single idle read; no parsing occurred.
    assert flushes == [True]
