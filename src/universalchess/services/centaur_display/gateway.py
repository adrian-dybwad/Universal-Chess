"""UC-side endpoint of the centaur display-translation gateway.

Receives centaur's DC-tagged SPI stream from the LD_PRELOAD shim over a unix
socket, decodes it into framebuffer images, and renders each completed frame
through UC's driver stack via an injected ``render_fn`` (typically
``board.display_manager.display_frame``).

The decode/render loop (``run_stream``) is pure with respect to the transport:
it takes a recv-like ``read_fn`` so it can be unit-tested over a byte buffer.
The socket setup/accept loop (``serve``) is the thin boundary around it.
"""

import logging
import os
import socket
import threading
from typing import Callable, List, Optional

from PIL import Image

from .decoder import CentaurDisplayDecoder
from .observed_io import write_observed_io
from .protocol import (
    RECORD_GPIO_PINS,
    RECORD_SPI_COMMAND,
    RECORD_SPI_DATA,
    RECORD_SPI_PATH,
    ReadFn,
    decode_gpio_mask,
    decode_spi_path,
    gpio_mask_to_pins,
    read_record,
)

log = logging.getLogger(__name__)

RenderFn = Callable[[Image.Image], object]
PersistObservedIo = Callable[[List[int], List[str]], None]


def render_and_signal(render_fn: RenderFn, event: threading.Event) -> RenderFn:
    """Wrap ``render_fn`` so ``event`` is set after each successful frame.

    Translate mode holds board->Centaur serial until the first painted frame
    (Centaur's T5D driver crashes if a battery event reaches ``update()`` while
    the framebuffer is still ``None``). The wrapper is the signal that a frame
    made it through the gateway; a raising ``render_fn`` must not set the event,
    because that frame did not reach the panel.
    """
    def wrapped(frame: Image.Image):
        result = render_fn(frame)
        event.set()
        return result
    return wrapped

# Default socket path. Lives under the runtime dir; the shim is told the same
# path via an environment variable at launch.
DEFAULT_SOCKET_PATH = "/run/universalchess/centaur-display.sock"


class CentaurDisplayGateway:
    """Decode centaur's SPI stream and render each frame through UC's drivers.

    Args:
        render_fn: Called with each decoded PIL frame (e.g. display_frame). Its
            return value is ignored.
        decoder: Decoder to use; a fresh ``CentaurDisplayDecoder`` by default.
        persist_observed_io: Called with the BCM pins and SPI paths seen on
            this connection whenever an observation record arrives. Defaults to
            writing ``centaur_io_observed.json`` so the Settings card can show
            them after Universal Chess restarts. Observation kinds must not
            reach the framebuffer decoder (kinds 2/3 look like DC-high data).
    """

    def __init__(
        self,
        render_fn: RenderFn,
        decoder: Optional[CentaurDisplayDecoder] = None,
        persist_observed_io: Optional[PersistObservedIo] = None,
    ):
        self._render_fn = render_fn
        self._decoder = decoder if decoder is not None else CentaurDisplayDecoder()
        self._persist_observed = (
            persist_observed_io if persist_observed_io is not None else write_observed_io
        )

    def run_stream(self, read_fn: ReadFn) -> None:
        """Consume records from ``read_fn`` until EOF, rendering completed frames.

        SPI records (kind 0/1) are fed to the decoder; whenever a refresh opcode
        completes a frame, ``render_fn`` is invoked with it. GPIO/SPI observation
        records are persisted and never decoded as panel data. Returns when the
        stream closes.
        """
        gpio_pins: set[int] = set()
        spi_devices: List[str] = []
        while True:
            record = read_record(read_fn)
            if record is None:
                return
            kind, payload = record
            if kind == RECORD_GPIO_PINS:
                mask = decode_gpio_mask(payload)
                if mask is None:
                    log.warning("ignoring GPIO observation with length %s", len(payload))
                    continue
                gpio_pins.update(gpio_mask_to_pins(mask))
                self._persist_observation(sorted(gpio_pins), list(spi_devices))
                continue
            if kind == RECORD_SPI_PATH:
                path = decode_spi_path(payload)
                if path is None:
                    log.warning("ignoring SPI path observation of %s bytes", len(payload))
                    continue
                if path not in spi_devices:
                    spi_devices.append(path)
                    self._persist_observation(sorted(gpio_pins), list(spi_devices))
                continue
            if kind not in (RECORD_SPI_COMMAND, RECORD_SPI_DATA):
                log.warning("ignoring unknown centaur-display record kind %s", kind)
                continue
            frame = self._decoder.feed(kind, payload)
            if frame is not None:
                self._render_fn(frame)

    def _persist_observation(self, gpio_pins: List[int], spi_devices: List[str]) -> None:
        try:
            self._persist_observed(gpio_pins, spi_devices)
        except OSError:
            log.exception("failed to persist centaur IO observation")

    def serve(self, socket_path: str = DEFAULT_SOCKET_PATH,
              stop_check: Optional[Callable[[], bool]] = None) -> None:
        """Bind a unix socket and serve shim connections until stopped.

        One connection at a time (centaur is a single panel client). ``stop_check``
        lets the host stop the accept loop (e.g. when centaur exits). This is the
        I/O boundary around ``run_stream`` and is intentionally thin.
        """
        os.makedirs(os.path.dirname(socket_path), exist_ok=True)
        # Remove a stale socket from a previous run so bind() does not fail.
        with _suppress_file_errors():
            os.unlink(socket_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(socket_path)
            server.listen(1)
            server.settimeout(1.0)
            log.info("Centaur display gateway listening at %s", socket_path)
            while stop_check is None or not stop_check():
                try:
                    conn, _ = server.accept()
                except socket.timeout:  # noqa: S112 - timeout is the normal idle poll; nothing to log
                    continue
                with conn:
                    self.run_stream(lambda n: conn.recv(n))
        finally:
            server.close()
            with _suppress_file_errors():
                os.unlink(socket_path)


class ThreadedGatewayServer:
    """Run a ``CentaurDisplayGateway`` accept loop on a background thread.

    The live Universal Chess process owns the panel, so the gateway must serve
    on a side thread while the main thread launches and blocks on centaur. The
    gateway's frames are rendered (via the gateway's ``render_fn``) onto the
    panel UC already drives, which is the whole point of "translate" mode.

    ``start`` spawns the serve loop; ``stop`` signals the accept loop's
    ``stop_check`` and joins the thread. The thread is a daemon so a crash on
    the main path can never leave the process unkillable.
    """

    def __init__(self, gateway: CentaurDisplayGateway,
                 socket_path: str = DEFAULT_SOCKET_PATH):
        self._gateway = gateway
        self._socket_path = socket_path
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the serve loop on a daemon thread (no-op if already running)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._gateway.serve,
            kwargs={"socket_path": self._socket_path,
                    "stop_check": self._stop.is_set},
            name="centaur-display-gateway",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the accept loop to exit and join the thread."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)


class _suppress_file_errors:
    """Context manager that swallows FileNotFoundError/OSError from path cleanup.

    Used only around best-effort socket-file unlink: a missing file (first run)
    or a races-with-cleanup error must not abort serving.
    """

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, OSError)
