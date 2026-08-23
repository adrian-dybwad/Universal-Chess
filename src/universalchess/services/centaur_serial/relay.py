"""PTY-based serial man-in-the-middle for Centaur "translate" mode.

In translate mode the original Centaur binary opens the board serial port
directly, so UC sees nothing of the physical board. This relay transparently
inserts itself between them (the proven pattern from
``tools/dev-tools/proxies/centaur.py``): the real device is moved aside to
``/dev/ttyS0.real`` and ``/dev/ttyS0`` is replaced with a PTY that Centaur opens
none the wiser. Two pumps forward bytes verbatim in each direction; the board ->
app direction is additionally fed to the :class:`EventDecoder` so UC can observe
lift/place and key events, and a held exit button can return control to UC.

Design constraints (see the plan's risk section):
  - The forward path is a dumb, verbatim byte pump; the decoder is a passive
    observer on a copy. Framing is never allowed to gate the live board bytes, so
    a decode bug can never corrupt or stall the board link.
  - Translate mode may hold board->app bytes until the display gateway has
    rendered one frame (or a timeout). That hold is optional, time-bounded, and
    independent of decoding: Centaur's T5D driver crashes if a battery event
    reaches ``update()`` while the framebuffer is still ``None``, and the shim
    makes the first paint slower than native serial. Direct mode does not hold.
  - Port restoration is guaranteed on stop, and a stale ``.real`` from a crashed
    prior run is healed on start; a botched restore would leave both Centaur and
    UC unable to use the board.

The pure pump functions and the port-lifecycle steps take injected dependencies
so the whole flow is unit-testable without real serial hardware or root.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, List, Optional, Protocol, Tuple

from universalchess.services.centaur_serial.command_decoder import (
    LedCommand,
    LedCommandDecoder,
)
from universalchess.services.centaur_serial.decoder import (
    EventDecoder,
    HoldToExitDetector,
    KeyEvent,
    PieceEvent,
)

import logging

log = logging.getLogger(__name__)

DEFAULT_DEVICE = "/dev/ttyS0"
DEFAULT_BAUD = 1000000
# How long translate mode may hold board->Centaur bytes waiting for the first
# painted frame. Long enough for shimmed GPIO/SPI init; short enough that a
# silent gateway cannot deadlock the board link.
SERIAL_HOLD_TIMEOUT_SECONDS = 10.0
# Env flag that turns on passive logging of the LED commands Centaur issues, for
# calibrating UC's own LED intensity against the stock software. Off by default
# so normal translate-mode play is not made noisy; set to "1" to capture.
LED_LOG_ENV_VAR = "UC_LOG_CENTAUR_LED"
# Read chunk for the hardware side; large enough for a full frame, small enough
# to keep latency negligible at 1 Mbaud.
_READ_CHUNK = 4096
# Idle backoff when a non-blocking read yields nothing, to avoid a busy spin
# (matches the dev-tool proxy's 1ms pacing).
_IDLE_SLEEP_SECONDS = 0.001


class SerialLike(Protocol):
    """Minimal serial interface the relay needs (satisfied by pyserial.Serial)."""

    def read(self, size: int) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


EventCallback = Callable[["PieceEvent | KeyEvent"], None]
LedCallback = Callable[["LedCommand"], None]


def pump_commands(
    read_fn: Callable[[], bytes],
    write_fn: Callable[[bytes], None],
    should_stop: Callable[[], bool],
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    led_decoder: Optional[LedCommandDecoder] = None,
    on_led: Optional[LedCallback] = None,
) -> None:
    """Forward the app -> board direction verbatim until stopped.

    This carries Centaur's commands (polls, LED/sound, discovery) to the real
    board. The forward path is a pure byte relay: bytes are written to the board
    first and unchanged. When ``led_decoder`` and ``on_led`` are supplied, a copy
    of each chunk is additionally fed to the decoder and each decoded LED command
    is passed to ``on_led`` -- a passive observer that never gates the forward
    path (its errors are caught) so Centaur's board link is never stalled by it.
    ``read_fn`` may return empty bytes when nothing is available (the PTY master
    is non-blocking); a short sleep then avoids a busy loop. IO errors (fd closed
    during teardown) end the pump.
    """
    while not should_stop():
        try:
            data = read_fn()
        except BlockingIOError:
            sleep_fn(_IDLE_SLEEP_SECONDS)
            continue
        except OSError:
            break
        if data:
            try:
                write_fn(data)
            except OSError:
                break
            if led_decoder is not None and on_led is not None:
                # Decoding must never break the relay; a decode fault is caught
                # here so the live board link keeps forwarding regardless.
                try:
                    for command in led_decoder.feed(data):
                        _safe_call(on_led, command)
                except Exception as exc:  # noqa: BLE001 - observer must not break the pump
                    log.error("[SerialTap] LED decode error: %s", exc)
        else:
            sleep_fn(_IDLE_SLEEP_SECONDS)


def _wait_for_release(
    release_event: threading.Event,
    timeout_seconds: float,
    should_stop: Callable[[], bool],
    clock_fn: Callable[[], float],
) -> None:
    """Block until ``release_event`` is set, ``should_stop``, or ``timeout_seconds``.

    Wait is sliced so a stop during the hold still unwinds the pump instead of
    sitting until the full timeout. ``timeout_seconds <= 0`` returns immediately.
    """
    deadline = clock_fn() + max(0.0, timeout_seconds)
    while not release_event.is_set() and not should_stop():
        remaining = deadline - clock_fn()
        if remaining <= 0:
            return
        release_event.wait(timeout=min(0.1, remaining))


def pump_events(
    read_fn: Callable[[], bytes],
    write_fn: Callable[[bytes], None],
    should_stop: Callable[[], bool],
    *,
    decoder: EventDecoder,
    detector: Optional[HoldToExitDetector] = None,
    clock_fn: Callable[[], float] = time.monotonic,
    on_event: Optional[EventCallback] = None,
    on_exit: Optional[Callable[[], None]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    release_event: Optional[threading.Event] = None,
    release_timeout_seconds: float = SERIAL_HOLD_TIMEOUT_SECONDS,
) -> None:
    """Forward the board -> app direction verbatim, decoding events on the side.

    Every byte is forwarded to Centaur unchanged (the board link is never gated
    on decoding). A copy is fed to ``decoder``; each decoded event is passed to
    ``on_event`` (for web feedback) and to ``detector`` (for the exit gesture).
    ``on_exit`` is invoked at most once, when the exit button has been held past
    its threshold -- checked every iteration (even on an idle read) so a hold
    with no further traffic still triggers.

    When ``release_event`` is supplied, the first board chunk is held until that
    event is set or ``release_timeout_seconds`` elapses. Later chunks pass
    immediately. The hold is the translate-mode first-frame gate, not a decode
    gate: a decode bug still cannot stall the link.
    """
    released = release_event is None
    while not should_stop():
        try:
            data = read_fn()
        except OSError:
            break
        if data:
            if not released:
                _wait_for_release(
                    release_event, release_timeout_seconds, should_stop, clock_fn
                )
                released = True
            try:
                write_fn(data)
            except OSError:
                break
            now = clock_fn()
            for event in decoder.feed(data):
                if on_event is not None:
                    _safe_call(on_event, event)
                if detector is not None:
                    detector.observe(event, now)
        else:
            sleep_fn(_IDLE_SLEEP_SECONDS)
        if detector is not None and detector.expired(clock_fn()):
            if on_exit is not None:
                _safe_call(on_exit)


def _log_led_command(command: "LedCommand") -> None:
    """Log one decoded Centaur LED command at INFO (env-gated diagnostic).

    Emits the raw protocol bytes (intensity/speed/repeat and the lit squares) so
    the stock software's values can be read straight from the service log.
    """
    if command.off:
        log.info("[CentaurLED] off")
    else:
        log.info(
            "[CentaurLED] intensity=%s speed=%s repeat=%s squares=%s",
            command.intensity,
            command.speed,
            command.repeat,
            command.squares,
        )


def _safe_call(fn: Callable, *args) -> None:
    """Invoke a relay callback, logging and swallowing any error.

    Callbacks (web publish, centaur kill) must never take down a relay thread and
    with it the live board link; a failure is logged and the pump continues.
    """
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - a callback must never break the relay
        log.error("[SerialTap] relay callback error: %s", exc)


def _default_run_cmd(cmd: List[str]) -> None:
    """Run a privileged device-node command (best-effort).

    The port swap needs root (stty/mv/ln on /dev), so commands are run via sudo
    exactly as the reference proxy does; injected so tests assert the sequence
    without touching /dev.
    """
    import subprocess  # nosec B404 - fixed device-node maintenance commands only

    subprocess.run(cmd, check=False)  # noqa: S603  # nosec B603


def _default_configure_pty(master_fd: int, slave_fd: int) -> None:
    """Make the PTY a transparent binary pipe.

    A PTY defaults to cooked mode, whose line discipline mangles binary bytes
    (OPOST/ONLCR rewrites 0x0A, ICRNL rewrites 0x0D, etc.). The board protocol is
    raw binary, so both endpoints must be raw or forwarded frames get corrupted.
    The master is also made non-blocking so its read never blocks the pump.
    """
    import tty

    tty.setraw(master_fd)
    tty.setraw(slave_fd)
    os.set_blocking(master_fd, False)


def _default_serial_open(device: str, baud: int) -> SerialLike:
    """Open the real serial device (lazy import so the module loads without pyserial)."""
    import serial  # pyserial

    return serial.Serial(device, baudrate=baud, timeout=0.2)


def resolve_tap_device() -> str:
    """The serial node the Centaur binary opens -- and therefore the node to tap.

    The tap only works if it swaps the *exact* node Centaur opens: Centaur is then
    handed the PTY and the tap becomes the sole opener of the real UART. Get this
    wrong and Centaur opens the real UART directly while the tap also holds it,
    and the two fight over the board's replies (Centaur hangs before it draws).

    Defaults to ``/dev/ttyS0`` -- the mini-UART on the Pi Zero / Zero 2W GPIO
    header that the Centaur binary opens directly, exactly as the reference proxy
    ``tools/dev-tools/proxies/centaur.py`` does. Overridable via
    ``UC_CENTAUR_SERIAL_DEVICE`` for hardware where Centaur opens a different node
    (verify on-device with strace/lsof first): e.g. the CM5/Pi5 has no
    ``/dev/ttyS0`` and Centaur falls back to ``/dev/ttyAMA0``.

    Note this is distinct from UC's own board node (``/dev/serial0``); Centaur
    never opens ``/dev/serial0``, which is why it must not be tapped.
    """
    return os.environ.get("UC_CENTAUR_SERIAL_DEVICE", DEFAULT_DEVICE)


def heal_swapped_serial_node(
    device: str,
    *,
    exists_fn: Callable[[str], bool] = os.path.exists,
    run_cmd: Callable[[List[str]], None] = _default_run_cmd,
) -> bool:
    """Move a real serial node back if the tap left it swapped aside.

    The tap parks the real device at ``{device}.real`` while a PTY stands in at
    ``device``. :meth:`SerialTap.restore` normally undoes that, but it can only
    run if the launching process survives to call it -- if the Universal Chess
    service is killed mid-teardown, ``device`` is left missing (its symlink
    removed) while ``{device}.real`` still holds the hardware. The next process
    to open the board (UC's controller at startup) then finds no port and retries
    the open indefinitely, which looks like a hang on return from Centaur.

    This heal detects exactly that state -- ``device`` absent while
    ``{device}.real`` present -- and moves the real node back before the open. It
    is a no-op in the normal case (``device`` present), so it is safe to call
    unconditionally at startup. Best-effort: any failure surfaces on the
    subsequent open rather than here. Returns True iff a move was issued.

    Complements (does not replace) :meth:`SerialTap.restore`, which self-heals
    only when it actually gets to run.
    """
    real = f"{device}.real"
    if not exists_fn(device) and exists_fn(real):
        log.warning(
            "[SerialTap] %s missing but %s present; restoring swapped serial node",
            device,
            real,
        )
        run_cmd(["sudo", "mv", real, device])
        return True
    return False


class SerialTap:
    """Owns the /dev/ttyS0 <-> PTY swap and the real-device handle.

    Responsible only for the port lifecycle (setup/restore) and opening the real
    device; the byte pumping lives in the module-level pump functions and the
    threading in :class:`ThreadedSerialTap`. All OS/privileged operations are
    injected so the swap sequence and the guaranteed restore are unit-testable
    without root or hardware.
    """

    def __init__(
        self,
        *,
        device: str = DEFAULT_DEVICE,
        baud: int = DEFAULT_BAUD,
        run_cmd: Callable[[List[str]], None] = _default_run_cmd,
        openpty_fn: Callable[[], Tuple[int, int]] = os.openpty,
        ttyname_fn: Callable[[int], str] = os.ttyname,
        configure_pty_fn: Callable[[int, int], None] = _default_configure_pty,
        serial_open_fn: Callable[[str, int], SerialLike] = _default_serial_open,
        exists_fn: Callable[[str], bool] = os.path.exists,
        islink_fn: Callable[[str], bool] = os.path.islink,
    ) -> None:
        self._device = device
        self._real = f"{device}.real"
        self._baud = baud
        self._run_cmd = run_cmd
        self._openpty_fn = openpty_fn
        self._ttyname_fn = ttyname_fn
        self._configure_pty_fn = configure_pty_fn
        self._serial_open_fn = serial_open_fn
        self._exists_fn = exists_fn
        self._islink_fn = islink_fn

        self._master_fd: Optional[int] = None
        self._slave_fd: Optional[int] = None
        self._serial: Optional[SerialLike] = None

    @property
    def master_fd(self) -> Optional[int]:
        return self._master_fd

    @property
    def serial(self) -> Optional[SerialLike]:
        return self._serial

    def setup(self) -> None:
        """Swap the port for a PTY and open the real device.

        Sequence (from the reference proxy): stop the login getty, free the port,
        move the real node aside to ``.real`` (only if not already moved -- so a
        stale ``.real`` from a crashed run is reused rather than clobbering the
        real device with a symlink), fix permissions/baud, create the PTY, point
        ``/dev/ttyS0`` at it, and open the real device.
        """
        basename = os.path.basename(self._device)
        self._run_cmd(["sudo", "systemctl", "stop", f"serial-getty@{basename}.service"])
        self._run_cmd(["sudo", "fuser", "-k", self._device])
        self._run_cmd(["sudo", "fuser", "-k", self._real])

        # Move the real node aside only if it has not already been moved. If
        # ``.real`` already exists, a prior run crashed before restoring; reuse it
        # (self-heal) instead of moving the now-symlinked /dev/ttyS0 over it.
        if not self._exists_fn(self._real):
            self._run_cmd(["sudo", "mv", self._device, self._real])

        self._run_cmd(["sudo", "chmod", "666", self._real])
        self._run_cmd(["sudo", "stty", "-F", self._real, str(self._baud)])

        master_fd, slave_fd = self._openpty_fn()
        self._master_fd, self._slave_fd = master_fd, slave_fd
        self._configure_pty_fn(master_fd, slave_fd)
        slave_name = self._ttyname_fn(slave_fd)

        self._run_cmd(["sudo", "ln", "-sf", slave_name, self._device])
        self._run_cmd(["sudo", "chmod", "666", slave_name])

        self._serial = self._serial_open_fn(self._real, self._baud)

    def restore(self) -> None:
        """Tear down the PTY and put ``/dev/ttyS0`` back, best-effort but complete.

        Each step is attempted independently so one failure does not abort the
        rest -- leaving the real device stranded behind a symlink would break both
        Centaur and UC until the next boot. Safe to call more than once.
        """
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001,S110 - best-effort close on teardown  # nosec B110
                pass
            self._serial = None

        for fd_attr in ("_master_fd", "_slave_fd"):
            fd = getattr(self, fd_attr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError as exc:
                    # Expected when the fd is already closed (e.g. a relay thread
                    # closed it) or was never a real fd; log at debug and continue
                    # so the rest of the restore still runs.
                    log.debug("[SerialTap] fd close during restore failed: %s", exc)
                setattr(self, fd_attr, None)

        # Restore the device nodes ONLY when the real device is actually parked
        # at ``.real`` -- that presence is the marker that we are in the swapped
        # state. If ``.real`` is absent (never swapped, or a prior restore already
        # completed) do nothing: in that state ``self._device`` is the real device
        # itself (a udev symlink to the UART), and removing it would strand the
        # board until reboot. Gating on ``.real`` this way makes restore
        # idempotent (safe to call more than once -- the second call is a no-op
        # instead of deleting the just-restored node) and lets it self-heal a
        # half-torn-down state: if the symlink was already removed (device
        # missing) but ``.real`` remains, the move-back still runs. The rm is
        # attempted before the mv but guarded independently so its failure does
        # not skip the (more important) move-back.
        if self._exists_fn(self._real):
            if self._islink_fn(self._device):
                self._run_node_cmd(["sudo", "rm", "-f", self._device])
            self._run_node_cmd(["sudo", "mv", self._real, self._device])

    def _run_node_cmd(self, cmd: List[str]) -> None:
        """Run a device-node command during restore, logging any failure.

        Restore must be best-effort but complete: one failed step must not abort
        the others, so a raising ``run_cmd`` is caught here rather than
        propagating out of :meth:`restore`.
        """
        try:
            self._run_cmd(cmd)
        except Exception as exc:  # noqa: BLE001 - restore must attempt every step
            log.error("[SerialTap] restore step failed (%s): %s", cmd, exc)


class ThreadedSerialTap:
    """Run a :class:`SerialTap`'s two pumps on background threads.

    Mirrors :class:`ThreadedGatewayServer`: the live UC process launches and
    blocks on Centaur, so the relay must pump on side threads. ``start`` performs
    the port swap and spawns the pumps; ``stop`` signals them, joins, and restores
    the port. Threads are daemons so a stuck pump can never make the process
    unkillable.

    Args:
        tap: The port-lifecycle owner.
        on_event: Optional per-event callback (e.g. web piece-in-hand feedback).
        stop_centaur_fn: Called when the exit button is held; returns control to
            UC by terminating Centaur (which unblocks the launcher and triggers
            teardown). None disables the exit gesture.
        exit_button / hold_seconds: Configure the exit gesture (default: hold BACK
            for 1s).
        release_event / release_timeout_seconds: Optional first-frame hold for
            translate mode (see :func:`pump_events`). None means no hold.
    """

    def __init__(
        self,
        tap: SerialTap,
        *,
        on_event: Optional[EventCallback] = None,
        on_led: Optional[LedCallback] = None,
        stop_centaur_fn: Optional[Callable[[], None]] = None,
        exit_button: str = "BACK",
        hold_seconds: float = 1.0,
        clock_fn: Callable[[], float] = time.monotonic,
        release_event: Optional[threading.Event] = None,
        release_timeout_seconds: float = SERIAL_HOLD_TIMEOUT_SECONDS,
    ) -> None:
        self._tap = tap
        self._on_event = on_event
        self._on_led = on_led
        self._stop_centaur_fn = stop_centaur_fn
        self._exit_button = exit_button
        self._hold_seconds = hold_seconds
        self._clock_fn = clock_fn
        self._release_event = release_event
        self._release_timeout_seconds = release_timeout_seconds
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    def start(self) -> None:
        """Swap the port and start the two relay pumps (no-op if already running)."""
        if self._threads:
            return
        self._stop.clear()
        try:
            self._start_locked()
        except BaseException:
            # Any failure during the swap/spawn must restore the port; a
            # half-swapped node would leave both Centaur and UC unable to use the
            # board. restore() is idempotent, so a partial setup is safe to undo.
            self._tap.restore()
            raise

    def _start_locked(self) -> None:
        self._tap.setup()

        master_fd = self._tap.master_fd
        serial = self._tap.serial
        if master_fd is None or serial is None:
            raise RuntimeError("SerialTap.setup did not open the port")

        decoder = EventDecoder()
        detector = (
            HoldToExitDetector(button=self._exit_button, hold_seconds=self._hold_seconds)
            if self._stop_centaur_fn is not None
            else None
        )

        def read_master() -> bytes:
            return os.read(master_fd, _READ_CHUNK)

        def write_master(data: bytes) -> None:
            os.write(master_fd, data)

        def read_real() -> bytes:
            return serial.read(_READ_CHUNK)

        def write_real(data: bytes) -> None:
            serial.write(data)
            serial.flush()

        # Effective LED observer: an explicit on_led callback if provided, else an
        # env-gated INFO logger so the LED bytes Centaur sends can be captured
        # without wiring code (see LED_LOG_ENV_VAR).
        on_led = self._on_led
        if on_led is None and os.environ.get(LED_LOG_ENV_VAR) == "1":
            on_led = _log_led_command
        led_decoder = LedCommandDecoder() if on_led is not None else None
        commands = threading.Thread(
            target=pump_commands,
            args=(read_master, write_real, self._stop.is_set),
            kwargs={"led_decoder": led_decoder, "on_led": on_led},
            name="centaur-serial-commands",
            daemon=True,
        )
        events = threading.Thread(
            target=pump_events,
            args=(read_real, write_master, self._stop.is_set),
            kwargs={
                "decoder": decoder,
                "detector": detector,
                "clock_fn": self._clock_fn,
                "on_event": self._on_event,
                "on_exit": self._stop_centaur_fn,
                "release_event": self._release_event,
                "release_timeout_seconds": self._release_timeout_seconds,
            },
            name="centaur-serial-events",
            daemon=True,
        )
        self._threads = [commands, events]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the pumps to stop, join them, and restore the port."""
        self._stop.set()
        threads, self._threads = self._threads, []
        for thread in threads:
            thread.join(timeout=timeout)
        self._tap.restore()
