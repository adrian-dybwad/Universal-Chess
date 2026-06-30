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
from typing import Callable, Optional

from PIL import Image

from .decoder import CentaurDisplayDecoder
from .protocol import ReadFn, read_record

log = logging.getLogger(__name__)

RenderFn = Callable[[Image.Image], object]

# Default socket path. Lives under the runtime dir; the shim is told the same
# path via an environment variable at launch.
DEFAULT_SOCKET_PATH = "/run/universalchess/centaur-display.sock"


class CentaurDisplayGateway:
    """Decode centaur's SPI stream and render each frame through UC's drivers.

    Args:
        render_fn: Called with each decoded PIL frame (e.g. display_frame). Its
            return value is ignored.
        decoder: Decoder to use; a fresh ``CentaurDisplayDecoder`` by default.
    """

    def __init__(self, render_fn: RenderFn, decoder: Optional[CentaurDisplayDecoder] = None):
        self._render_fn = render_fn
        self._decoder = decoder if decoder is not None else CentaurDisplayDecoder()

    def run_stream(self, read_fn: ReadFn) -> None:
        """Consume records from ``read_fn`` until EOF, rendering completed frames.

        Each record is fed to the decoder; whenever a refresh opcode completes a
        frame, ``render_fn`` is invoked with it. Returns when the stream closes.
        """
        while True:
            record = read_record(read_fn)
            if record is None:
                return
            dc, payload = record
            frame = self._decoder.feed(dc, payload)
            if frame is not None:
                self._render_fn(frame)

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
