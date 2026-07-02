"""Tests for the Centaur serial tap relay (transport + port lifecycle).

These pin the two behaviours that make the tap safe to run against the live
board: (1) the port swap and its guaranteed restore -- a botched restore strands
the real device behind a symlink and breaks both Centaur and UC -- and (2) the
verbatim forward path, which must never gate the board bytes on decoding while
still surfacing events and the hold-to-exit trigger. The privileged/OS ops are
injected, and the threaded case uses a real PTY so forwarding is exercised
end-to-end without hardware or root.
"""

import os
import threading
import time
from typing import List, Optional

import pytest

from universalchess.services.centaur_serial.decoder import (
    EventDecoder,
    HoldToExitDetector,
    KeyEvent,
    checksum,
)
from universalchess.services.centaur_serial.relay import (
    SerialTap,
    ThreadedSerialTap,
    heal_swapped_serial_node,
    pump_commands,
    pump_events,
    resolve_tap_device,
)


def build_frame(frame_type: int, payload: bytes) -> bytes:
    """Build a valid board -> app frame (see decoder tests for the layout)."""
    total = 1 + 2 + 2 + len(payload) + 1
    header = bytes((frame_type, (total >> 7) & 0x7F, total & 0x7F, 0x00, 0x00))
    body = header + payload
    return body + bytes((checksum(body),))


BACK_DOWN_FRAME = build_frame(0xB1, bytes((0x00, 0x14, 0x0A, 0x05, 0x01)))


class FakeSerial:
    """In-memory stand-in for pyserial.Serial with a thread-safe read queue."""

    def __init__(self) -> None:
        self._reads: List[bytes] = []
        self.written = bytearray()
        self.closed = False
        self._lock = threading.Lock()

    def queue(self, data: bytes) -> None:
        with self._lock:
            self._reads.append(data)

    def read(self, size: int) -> bytes:
        with self._lock:
            return self._reads.pop(0) if self._reads else b""

    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.extend(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def make_tap(*, real_exists: bool = False, is_link: bool = True, run_log: Optional[List] = None):
    """Build a SerialTap with fully injected fakes (no /dev, no root, no pyserial)."""
    log = run_log if run_log is not None else []
    serial = FakeSerial()

    tap = SerialTap(
        device="/dev/ttyS0",
        baud=1000000,
        run_cmd=lambda cmd: log.append(cmd),
        openpty_fn=lambda: (9001, 9002),  # high ints: os.close raises EBADF (caught)
        ttyname_fn=lambda fd: "/dev/pts/7",
        configure_pty_fn=lambda m, s: None,
        serial_open_fn=lambda dev, baud: serial,
        exists_fn=lambda path: real_exists,
        islink_fn=lambda path: is_link,
    )
    return tap, log, serial


# ---------------------------------------------------------------------------
# Port lifecycle
# ---------------------------------------------------------------------------


def test_setup_performs_full_swap_sequence():
    """setup() runs the privileged swap in order and opens the real device.

    Why this test exists: the swap is the load-bearing step; a wrong order (e.g.
    moving the node after symlinking, or skipping the baud set) leaves Centaur
    talking to the wrong endpoint. Asserts the exact command sequence, the PTY
    wiring, and that the real device (``.real``) is what gets opened. Regression
    manifests as a missing/reordered command or the master fd not being captured.
    """
    tap, run_log, serial = make_tap(real_exists=False)
    tap.setup()

    assert run_log == [
        ["sudo", "systemctl", "stop", "serial-getty@ttyS0.service"],
        ["sudo", "fuser", "-k", "/dev/ttyS0"],
        ["sudo", "fuser", "-k", "/dev/ttyS0.real"],
        ["sudo", "mv", "/dev/ttyS0", "/dev/ttyS0.real"],
        ["sudo", "chmod", "666", "/dev/ttyS0.real"],
        ["sudo", "stty", "-F", "/dev/ttyS0.real", "1000000"],
        ["sudo", "ln", "-sf", "/dev/pts/7", "/dev/ttyS0"],
        ["sudo", "chmod", "666", "/dev/pts/7"],
    ]
    assert tap.master_fd == 9001
    assert tap.serial is serial


def test_setup_self_heals_stale_real_node():
    """When ``.real`` already exists (crashed prior run), the move-aside is skipped.

    Why this test exists: repeating ``mv /dev/ttyS0 /dev/ttyS0.real`` after a
    crash would move the fresh symlink over the preserved real device and destroy
    the only handle to the hardware. Regression manifests as the mv command being
    present when ``.real`` already exists.
    """
    tap, run_log, _ = make_tap(real_exists=True)
    tap.setup()

    assert ["sudo", "mv", "/dev/ttyS0", "/dev/ttyS0.real"] not in run_log


def test_restore_removes_symlink_and_moves_real_back():
    """restore() closes the handle, drops the symlink, and moves ``.real`` back.

    Why this test exists: this is the guarantee that the board is usable again
    after the tap stops. Asserts the serial is closed and the two node commands
    run in order. Regression manifests as the real device left behind a symlink.
    """
    tap, run_log, serial = make_tap(real_exists=True, is_link=True)
    tap.setup()
    run_log.clear()

    tap.restore()

    assert serial.closed is True
    assert run_log == [
        ["sudo", "rm", "-f", "/dev/ttyS0"],
        ["sudo", "mv", "/dev/ttyS0.real", "/dev/ttyS0"],
    ]


def test_restore_is_noop_when_not_swapped():
    """restore() touches no device node when ``.real`` is absent.

    Why this test exists: guards the switch-back regression that stranded the
    board. ``/dev/ttyS0`` is normally a udev symlink to the real UART, so the old
    restore -- which removed it whenever it was *any* symlink -- would delete the
    restored node on a second (idempotent) restore call, leaving UC with no port
    to open and retrying forever. With ``.real`` absent (already restored / never
    swapped) restore must issue no rm and no mv. Regression manifests as an
    ``rm``/``mv`` appearing here, i.e. the real device being deleted.
    """
    tap, run_log, _ = make_tap(real_exists=False, is_link=True)
    run_log.clear()

    tap.restore()

    assert run_log == []


def test_restore_heals_when_device_missing_but_real_present():
    """restore() moves ``.real`` back even when our symlink is already gone.

    Why this test exists: a teardown interrupted after the symlink was removed
    (device missing) but before the move-back leaves the real device parked at
    ``.real``. Re-running restore must complete the move-back and must NOT try to
    rm the (nonexistent) device. Regression manifests as a spurious ``rm`` or the
    ``mv`` being skipped, leaving the board unusable until reboot.
    """
    tap, run_log, _ = make_tap(real_exists=True, is_link=False)
    run_log.clear()

    tap.restore()

    assert run_log == [["sudo", "mv", "/dev/ttyS0.real", "/dev/ttyS0"]]


def test_restore_moves_real_back_even_if_symlink_removal_raises():
    """A failure removing the symlink must not prevent moving ``.real`` back.

    Why this test exists: the move-back is the step that actually restores the
    hardware; it must be attempted even if the prior step throws. A raising
    run_cmd on the ``rm`` is simulated. Regression manifests as the mv being
    skipped, stranding the device behind a symlink until reboot.
    """
    calls: List[List[str]] = []

    def flaky_run(cmd: List[str]) -> None:
        calls.append(cmd)
        if cmd[:2] == ["sudo", "rm"]:
            raise RuntimeError("rm failed")

    tap = SerialTap(
        run_cmd=flaky_run,
        openpty_fn=lambda: (9001, 9002),
        ttyname_fn=lambda fd: "/dev/pts/7",
        configure_pty_fn=lambda m, s: None,
        serial_open_fn=lambda dev, baud: FakeSerial(),
        exists_fn=lambda path: True,
        islink_fn=lambda path: True,
    )
    tap.setup()
    calls.clear()

    tap.restore()  # must not raise

    assert ["sudo", "mv", "/dev/ttyS0.real", "/dev/ttyS0"] in calls


# ---------------------------------------------------------------------------
# Tap device selection (must be the node Centaur actually opens)
# ---------------------------------------------------------------------------


def test_resolve_tap_device_defaults_to_proxy_node(monkeypatch):
    """Absent an override, the tap targets the reference proxy's /dev/ttyS0.

    Why this test exists: the Centaur binary opens /dev/ttyS0 directly on the
    Zero/Zero2W (confirmed by strace, and matching tools/dev-tools/proxies/
    centaur.py). A prior default of /dev/serial0 -- a node Centaur never opens --
    made the tap swap the wrong node: Centaur grabbed the real UART directly while
    the tap also held it, starving Centaur of board replies so it hung before
    drawing. Regression manifests as a default other than /dev/ttyS0.
    """
    monkeypatch.delenv("UC_CENTAUR_SERIAL_DEVICE", raising=False)
    assert resolve_tap_device() == "/dev/ttyS0"


def test_resolve_tap_device_honors_override(monkeypatch):
    """UC_CENTAUR_SERIAL_DEVICE overrides the node for other hardware.

    Why this test exists: on CM5/Pi5 there is no /dev/ttyS0 and Centaur falls back
    to /dev/ttyAMA0, so the tap must be pointable at a different node without a
    code change. Regression manifests as the override being ignored.
    """
    monkeypatch.setenv("UC_CENTAUR_SERIAL_DEVICE", "/dev/ttyAMA0")
    assert resolve_tap_device() == "/dev/ttyAMA0"


# ---------------------------------------------------------------------------
# Swapped-node self-heal (called by UC's board controller before opening)
# ---------------------------------------------------------------------------


def _exists_map(present):
    """Return an exists_fn that reports True only for paths in ``present``."""
    return lambda path: path in present


def test_heal_moves_real_back_when_device_missing():
    """When the device is gone but ``.real`` remains, the real node is moved back.

    Why this test exists: this is the switch-back recovery. If a tap teardown was
    interrupted (UC killed mid-restore), /dev/serial0 is missing while
    /dev/serial0.real holds the hardware; UC must move it back before opening or
    it retries the open forever. Asserts the exact mv and a True (healed) result.
    Regression manifests as no mv issued (board never reconnects) or a wrong path.
    """
    calls = []
    healed = heal_swapped_serial_node(
        "/dev/serial0",
        exists_fn=_exists_map({"/dev/serial0.real"}),
        run_cmd=calls.append,
    )
    assert healed is True
    assert calls == [["sudo", "mv", "/dev/serial0.real", "/dev/serial0"]]


def test_heal_is_noop_when_device_present():
    """A present device is left untouched (the normal, non-swapped case).

    Why this test exists: the heal must never disturb a healthy node. With the
    device present, no command may run and the result is False. Regression
    manifests as a spurious mv that would clobber the live port.
    """
    calls = []
    healed = heal_swapped_serial_node(
        "/dev/serial0",
        exists_fn=_exists_map({"/dev/serial0", "/dev/serial0.real"}),
        run_cmd=calls.append,
    )
    assert healed is False
    assert calls == []


def test_heal_is_noop_when_nothing_to_restore():
    """No device and no ``.real`` -> nothing to do (not a swapped state).

    Why this test exists: absent both nodes, there is no parked real device to
    restore, so heal must not fabricate a move. Regression manifests as an mv of
    a nonexistent ``.real`` that would fail or create a broken node.
    """
    calls = []
    healed = heal_swapped_serial_node(
        "/dev/serial0",
        exists_fn=_exists_map(set()),
        run_cmd=calls.append,
    )
    assert healed is False
    assert calls == []


# ---------------------------------------------------------------------------
# Pure pumps
# ---------------------------------------------------------------------------


def _draining_reader(chunks: List[bytes]):
    """Return (read_fn, should_stop): yields chunks then stops once drained."""
    state = {"i": 0}

    def read_fn() -> bytes:
        if state["i"] < len(chunks):
            chunk = chunks[state["i"]]
            state["i"] += 1
            return chunk
        return b""

    def should_stop() -> bool:
        return state["i"] >= len(chunks)

    return read_fn, should_stop


def test_pump_commands_forwards_bytes_verbatim():
    """The app -> board pump forwards every non-empty chunk unchanged.

    Why this test exists: this path carries Centaur's board commands; any
    reordering, coalescing loss, or dropping of a chunk would break the board
    protocol. An empty chunk in the middle must be a no-op, not a stop.
    Regression manifests as forwarded bytes differing from the input stream.
    """
    read_fn, should_stop = _draining_reader([b"abc", b"", b"de"])
    written = bytearray()
    pump_commands(read_fn, written.extend, should_stop, sleep_fn=lambda s: None)
    assert bytes(written) == b"abcde"


def test_pump_commands_forwards_verbatim_and_surfaces_led_commands():
    """The app -> board pump forwards bytes AND decodes LED commands on the side.

    Why this test exists: capturing the stock Centaur software's LED intensity
    depends on this observer. A LED set frame must be (1) forwarded byte-for-byte
    to the board and (2) decoded into an LedCommand delivered to on_led, without
    the observer altering or gating the forward path. Regression manifests as
    altered forwarded bytes or a missing/incorrect LED command.
    """
    from universalchess.services.centaur_serial.command_decoder import (
        LedCommand,
        LedCommandDecoder,
    )

    # A real LED set frame: intensity 7 at square 0x38, speed 3, repeat 0.
    led_frame = bytes.fromhex("b000" "0b" "3e5e" "0503000738" "1e".replace(" ", ""))
    # Recompute the checksum so the frame is valid regardless of the literal above.
    body = led_frame[:-1]
    led_frame = body + bytes((checksum(body),))

    read_fn, should_stop = _draining_reader([led_frame])
    written = bytearray()
    leds: List = []
    pump_commands(
        read_fn,
        written.extend,
        should_stop,
        sleep_fn=lambda s: None,
        led_decoder=LedCommandDecoder(),
        on_led=leds.append,
    )

    assert bytes(written) == led_frame  # forwarded unchanged
    assert leds == [LedCommand(off=False, intensity=7, speed=3, repeat=0, squares=[0x38])]


def test_pump_events_forwards_verbatim_and_decodes_and_exits():
    """The board -> app pump forwards bytes, surfaces events, and fires exit.

    Why this test exists: this is the whole value of the tap. A key-down BACK
    frame must be (1) forwarded byte-for-byte to Centaur, (2) decoded into a
    KeyEvent delivered to on_event, and (3) with a zero hold threshold, trigger
    on_exit exactly once. Regression manifests as altered forwarded bytes, a
    missing event, or the exit failing to fire.
    """
    read_fn, should_stop = _draining_reader([BACK_DOWN_FRAME])
    written = bytearray()
    events: List = []
    exits = {"n": 0}

    pump_events(
        read_fn,
        written.extend,
        should_stop,
        decoder=EventDecoder(),
        detector=HoldToExitDetector(button="BACK", hold_seconds=0.0),
        on_event=events.append,
        on_exit=lambda: exits.__setitem__("n", exits["n"] + 1),
        sleep_fn=lambda s: None,
    )

    assert bytes(written) == BACK_DOWN_FRAME  # forwarded unchanged
    assert events == [KeyEvent(button="BACK", code=0x01, is_down=True)]
    assert exits["n"] == 1


def test_pump_events_forwards_unknown_bytes_without_events_or_exit():
    """Non-protocol bytes are still forwarded verbatim and trigger nothing.

    Why this test exists: the forward path must be transport-transparent even for
    bytes the decoder cannot frame, and garbage must never fire the exit gesture.
    Regression manifests as dropped bytes, a spurious event, or a false exit.
    """
    read_fn, should_stop = _draining_reader([b"\x11\x22\x33"])
    written = bytearray()
    events: List = []
    exits = {"n": 0}

    pump_events(
        read_fn,
        written.extend,
        should_stop,
        decoder=EventDecoder(),
        detector=HoldToExitDetector(button="BACK", hold_seconds=0.0),
        on_event=events.append,
        on_exit=lambda: exits.__setitem__("n", exits["n"] + 1),
        sleep_fn=lambda s: None,
    )

    assert bytes(written) == b"\x11\x22\x33"
    assert events == []
    assert exits["n"] == 0


# ---------------------------------------------------------------------------
# Threaded lifecycle (real PTY, no hardware/root)
# ---------------------------------------------------------------------------


class PtyTap:
    """A SerialTap-shaped fake backed by a real PTY, for the threaded relay test.

    Uses os.openpty() so the master fd is genuinely bidirectional (a pipe is not),
    letting the board -> app forward be observed on the PTY slave. run_cmd is a
    no-op so nothing touches /dev.
    """

    def __init__(self) -> None:
        self._serial = FakeSerial()
        self._master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.restored = False

    def setup(self) -> None:
        import tty

        master_fd, slave_fd = os.openpty()
        tty.setraw(master_fd)
        tty.setraw(slave_fd)
        os.set_blocking(master_fd, False)
        self._master_fd = master_fd
        self.slave_fd = slave_fd

    @property
    def master_fd(self) -> Optional[int]:
        return self._master_fd

    @property
    def serial(self) -> FakeSerial:
        return self._serial

    def restore(self) -> None:
        self.restored = True
        for fd in (self._master_fd, self.slave_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:  # noqa: S110 - test teardown; fd may already be closed
                    pass


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_threaded_tap_forwards_board_to_app_and_restores_on_stop():
    """start() swaps + pumps; a queued frame is forwarded to the PTY and decoded;
    stop() restores the port and ends the threads.

    Why this test exists: proves the thread wiring actually connects the pumps to
    the real fds and that stop() cleanly tears down. A queued BACK-down frame must
    reach the PTY slave verbatim and surface as an event; after stop() the tap
    must be restored and no relay thread left alive. Regression manifests as the
    frame not reaching the slave, no event, or a lingering thread after stop.
    """
    tap = PtyTap()
    events: List = []
    threaded = ThreadedSerialTap(
        tap,
        on_event=events.append,
        stop_centaur_fn=None,  # exit gesture disabled; lifecycle-only test
    )

    threaded.start()
    try:
        tap.serial.queue(BACK_DOWN_FRAME)
        assert _wait_until(lambda: events), "event was never decoded from the relayed frame"
        forwarded = _read_all(tap.slave_fd, min_len=len(BACK_DOWN_FRAME))
        assert forwarded == BACK_DOWN_FRAME
        assert events == [KeyEvent(button="BACK", code=0x01, is_down=True)]
    finally:
        threaded.stop(timeout=2.0)

    assert tap.restored is True
    assert all(not t.is_alive() for t in threading.enumerate() if t.name.startswith("centaur-serial"))


def _read_all(fd: int, *, min_len: int, timeout: float = 1.0) -> bytes:
    """Accumulate at least ``min_len`` bytes from ``fd`` within the timeout window."""
    os.set_blocking(fd, False)
    out = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(out) < min_len:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            chunk = b""
        if chunk:
            out.extend(chunk)
        else:
            time.sleep(0.01)
    return bytes(out)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
