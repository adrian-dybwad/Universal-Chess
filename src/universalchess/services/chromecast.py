"""
Chromecast streaming service.

Manages the Chromecast connection and streaming lifecycle. The board can stream
to several devices at once, so the service holds one independent connection
thread per device (see ``_DeviceStream``); the observable state lives in
state/chromecast.py.

Connection robustness notes (fixes for the previously "buggy" casting):
  * Discovery is *targeted* by friendly name (``get_listed_chromecasts``) instead
    of a full network scan on every connect/reconnect, so starting and recovering
    are fast and do not churn the zeroconf browser.
  * ``wait``/``block_until_active`` use explicit timeouts so a flaky device cannot
    hang a stream thread forever.
  * Liveness is detected via the socket-client connection flag rather than the
    receiver's app display name, which could be None and threw the old loop into
    a reconnect-thrash cycle.

Also provides the e-paper JPEG export function used for web/Chromecast streaming.
"""

import os
import threading
import time
from typing import Callable, Dict, List, Optional

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

# Path for e-paper static JPEG (used by web and Chromecast streaming)
from universalchess.paths import EPAPER_STATIC_JPG
from universalchess.state import get_chromecast as get_chromecast_state

# Connection tuning (seconds). Bounded so a single misbehaving device cannot
# wedge its stream thread.
_CONNECT_TIMEOUT = 15.0
_MEDIA_ACTIVE_TIMEOUT = 15.0
_RETRY_WAIT = 10.0
_MONITOR_POLL = 2.0
_CAST_CONTENT_TYPE = "image/jpeg"


def media_state_action(player_state: Optional[str]) -> str:
    """Return how the stream loop should respond to a Chromecast player state.

    Chromecast can pause a live MJPEG URL without dropping the socket. If the
    loop only watches socket liveness, the receiver sits paused until its
    screensaver takes over. ``PAUSED`` needs an explicit play command; ``IDLE``
    means the receiver has left media playback and the stream should reconnect.
    """
    normalized = (player_state or "").upper()
    if normalized == "PAUSED":
        return "play"
    if normalized == "IDLE":
        return "reconnect"
    return "keep"


def stream_path_for_source(use_live_board: bool) -> str:
    """Return the /video path for the selected Chromecast display source.

    The Chromecast receiver caches a media URL for the running stream. Carrying
    the source in the URL keeps a stream started from either UI tied to the
    selected layout instead of depending on later config reads.
    """
    source = "live_board" if use_live_board else "classic"
    return f"/video?source={source}"


def _config_bool(value: str, default: bool = True) -> bool:
    normalized = str(value).strip().lower()
    if normalized in ("true", "on", "1", "yes"):
        return True
    if normalized in ("false", "off", "0", "no"):
        return False
    return default


def compose_epaper_rgb(bw_image, red_image):
    """Compose a white/black/red RGB preview from the B/W and RED planes.

    Mirrors the tri-color panel: white background, black where the B/W plane is
    black, red where the red plane is set (red wins, matching the panel where a
    red pixel is forced white in the B/W buffer). Both planes share the same
    orientation/size.

    Args:
        bw_image: B/W plane (mode '1' or 'L'; black = 0).
        red_image: RED-plane mask (mode '1'; red = 0, not-red = 255).

    Returns:
        An RGB PIL Image.
    """
    from PIL import Image

    bw = bw_image.convert('1')
    rgb = Image.new('RGB', bw.size, (255, 255, 255))
    width, height = bw.size
    box = (0, 0, width, height)

    # Black where the B/W plane is black (mask = 255 at black pixels).
    black_mask = bw.point(lambda p: 255 if p == 0 else 0, mode='1')
    rgb.paste((0, 0, 0), box, black_mask)

    # Red where the red plane is set (painted after black so red wins).
    red_mask = red_image.convert('1').point(lambda p: 255 if p == 0 else 0, mode='1')
    rgb.paste((220, 0, 0), box, red_mask)
    return rgb


def write_epaper_jpg(image, red_image=None) -> str:
    """Write the provided Pillow Image to web/static/epaper.jpg for streaming.
    
    The image will be converted to a JPEG-compatible mode if needed.
    The image is rotated 180 degrees before saving to correct orientation
    for Chromecast streaming.
    
    Args:
        image: PIL Image to save (the black/white plane).
        red_image: Optional RED-plane mask (three-color mode). When provided, the
            saved preview is composed in RGB (white/black/red) so the dashboard
            mirrors the red ink; None saves the plain B/W image as before.
        
    Returns:
        Path where image was saved
        
    Raises:
        TypeError: If image is not a PIL Image
    """
    from PIL import Image
    
    if not isinstance(image, Image.Image):
        raise TypeError("write_epaper_jpg expects a PIL Image")
    
    # Ensure parent directory exists
    parent = os.path.dirname(EPAPER_STATIC_JPG)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except PermissionError:
            log.error(f"Permission denied creating directory: {parent}")
            raise
    
    if red_image is not None:
        img = compose_epaper_rgb(image, red_image)
    else:
        img = image
        if img.mode not in ("L", "RGB"):
            img = img.convert("L")
    
    # Rotate 180 degrees to correct orientation for streaming
    img = img.rotate(180)
    img.save(EPAPER_STATIC_JPG, format="JPEG")
    return EPAPER_STATIC_JPG


class _DeviceStream:
    """One Chromecast device's connection + monitor thread.

    Owns a single pychromecast device, its background thread, and a stop event.
    Reports progress by writing this device's entry in the shared
    ChromecastState (keyed by friendly name), so multiple streams coexist.
    """

    def __init__(self, name: str, state):
        self.name = name
        self._state = state
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cc = None

    def start(self) -> None:
        """Spawn the connection/monitor thread for this device."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"chromecast-{self.name}",
            daemon=True,
        )
        self._thread.start()
        log.info(f"[ChromecastService] Starting stream to: {self.name}")

    def stop(self) -> None:
        """Signal the thread to stop, stop media, and disconnect."""
        self._stop_event.set()
        cc = self._cc
        if cc is not None:
            try:
                cc.media_controller.stop()
            except Exception as e:
                log.debug(f"[ChromecastService] Error stopping media for {self.name}: {e}")
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                log.warning(f"[ChromecastService] {self.name} thread did not stop in time")
            self._thread = None
        self._disconnect()

    def _disconnect(self) -> None:
        cc = self._cc
        self._cc = None
        if cc is not None:
            try:
                cc.disconnect()
            except Exception:
                pass

    def _loop(self) -> None:
        """Connect to this device and keep the stream alive until stopped."""
        try:
            import pychromecast
            from universalchess.board import network
        except ImportError as e:
            log.error(f"[ChromecastService] Missing dependency: {e}")
            self._state.set_error(self.name, "Missing pychromecast")
            return

        while not self._stop_event.is_set():
            browser = None
            try:
                # Targeted discovery: only look for this device, not the whole
                # network. Much faster than get_chromecasts() and avoids
                # churning a full zeroconf browser on every (re)connect.
                chromecasts, browser = pychromecast.get_listed_chromecasts(
                    friendly_names=[self.name]
                )
                if self._stop_event.is_set():
                    break
                if not chromecasts:
                    self._state.set_error(self.name, "Device not found")
                    if self._stop_event.wait(_RETRY_WAIT):
                        break
                    self._state.set_reconnecting(self.name)
                    continue

                cc = chromecasts[0]
                self._cc = cc
                cc.wait(timeout=_CONNECT_TIMEOUT)
                # Discovery is no longer needed once connected; release it so we
                # are not holding a background mDNS listener while streaming.
                _safe_stop_discovery(browser)
                browser = None
                if self._stop_event.is_set():
                    break

                ip = network.check_network()
                if not ip:
                    self._state.set_error(self.name, "No network")
                    if self._stop_event.wait(_RETRY_WAIT):
                        break
                    self._state.set_reconnecting(self.name)
                    continue

                mc = cc.media_controller
                from universalchess.board.settings import Settings

                use_live_board = _config_bool(Settings.read(
                    "chromecast", "use_live_board", "True"), default=True)
                stream_url = f"http://{ip}{stream_path_for_source(use_live_board)}&t={time.time()}"
                log.info(f"[ChromecastService] {self.name}: streaming {stream_url}")
                mc.play_media(stream_url, _CAST_CONTENT_TYPE, stream_type="LIVE", autoplay=True)
                mc.block_until_active(timeout=_MEDIA_ACTIVE_TIMEOUT)
                mc.play()
                self._state.set_streaming(self.name)
                log.info(f"[ChromecastService] Streaming to {self.name}")

                # Liveness: poll the socket connection rather than the receiver
                # app name (which can be None and previously caused reconnect
                # thrashing). Break out to reconnect if the socket drops.
                while not self._stop_event.is_set():
                    sc = getattr(cc, "socket_client", None)
                    if sc is not None and getattr(sc, "is_connected", True) is False:
                        log.info(f"[ChromecastService] {self.name}: connection dropped")
                        break
                    try:
                        mc.update_status()
                        player_state = getattr(mc.status, "player_state", None)
                        action = media_state_action(player_state)
                        if action == "play":
                            log.info(f"[ChromecastService] {self.name}: media paused, resuming")
                            mc.play()
                        elif action == "reconnect":
                            log.info(f"[ChromecastService] {self.name}: media idle, reconnecting")
                            break
                    except Exception as e:
                        log.debug(f"[ChromecastService] {self.name}: media status check failed: {e}")
                    if self._stop_event.wait(_MONITOR_POLL):
                        break

                if self._stop_event.is_set():
                    break
                self._state.set_reconnecting(self.name)
            except Exception as e:  # noqa: BLE001 - keep the stream thread alive
                log.error(f"[ChromecastService] {self.name}: {e}")
                self._state.set_error(self.name, str(e)[:30])
                if self._stop_event.wait(_RETRY_WAIT):
                    break
                self._state.set_reconnecting(self.name)
            finally:
                _safe_stop_discovery(browser)
                self._disconnect()

        self._disconnect()
        log.info(f"[ChromecastService] {self.name}: stream loop ended")


def _safe_stop_discovery(browser) -> None:
    if browser is not None:
        try:
            browser.stop_discovery()
        except Exception:
            pass


class ChromecastService:
    """Service managing Chromecast streaming to one or more devices.

    Holds one ``_DeviceStream`` per active device. Starting a new device adds a
    stream without disturbing the others; stopping targets a single device or,
    with no argument, every device. State is published to the shared
    ChromecastState which notifies observers (status bar + web mirror).

    Widgets should import from state/, not this service.
    """

    def __init__(self, stream_factory: Optional[Callable[[str], object]] = None):
        """Initialize the service.

        Args:
            stream_factory: Optional factory ``name -> stream`` used to create a
                per-device stream. Injected in tests; defaults to a real
                ``_DeviceStream`` bound to the shared state.
        """
        self._state = get_chromecast_state()
        self._streams: Dict[str, object] = {}
        self._lock = threading.Lock()
        self._stream_factory = stream_factory or self._default_stream_factory

    def _default_stream_factory(self, name: str):
        return _DeviceStream(name, self._state)

    # -------------------------------------------------------------------------
    # Properties (delegate to state for reads)
    # -------------------------------------------------------------------------

    @property
    def state(self) -> int:
        """Aggregate streaming state across all devices."""
        return self._state.state

    @property
    def device_name(self) -> Optional[str]:
        """Name of an active device (back-compat single value)."""
        return self._state.device_name

    @property
    def error_message(self) -> Optional[str]:
        """First device error message, if any."""
        return self._state.error_message

    @property
    def is_active(self) -> bool:
        """True if any device is streaming or attempting to stream."""
        return self._state.is_active

    @property
    def active_devices(self) -> List[str]:
        """Names of devices with a live stream object, in start order."""
        with self._lock:
            return list(self._streams.keys())

    # -------------------------------------------------------------------------
    # Observer management (delegate to state)
    # -------------------------------------------------------------------------

    def add_observer(self, callback) -> None:
        """Add an observer to be notified on state changes."""
        self._state.add_observer(callback)

    def remove_observer(self, callback) -> None:
        """Remove an observer."""
        self._state.remove_observer(callback)

    # -------------------------------------------------------------------------
    # Streaming control
    # -------------------------------------------------------------------------

    def start_streaming(self, device_name: Optional[str]) -> bool:
        """Start streaming to a device, adding it to the active set.

        Starting a device that is already tracked refreshes that device's stream
        instead of returning a no-op. A Chromecast can abandon media playback
        while the board still has a reconnecting thread for the same friendly
        name; refreshing avoids leaving the UI stuck on a stale attempt.

        Args:
            device_name: Friendly name of the Chromecast device.

        Returns:
            True if the device is (now) streaming/connecting, False if the name
            was empty.
        """
        if not device_name:
            return False
        old_stream = None
        with self._lock:
            if device_name in self._streams:
                old_stream = self._streams.pop(device_name)

        if old_stream is not None:
            log.info(f"[ChromecastService] Refreshing stream to: {device_name}")
            old_stream.stop()
            self._state.set_idle(device_name)

        with self._lock:
            stream = self._stream_factory(device_name)
            self._streams[device_name] = stream
        self._state.set_connecting(device_name)
        stream.start()
        return True

    def stop_streaming(self, device_name: Optional[str] = None) -> None:
        """Stop one device's stream, or all devices when ``device_name`` is None."""
        with self._lock:
            if device_name is None:
                streams = list(self._streams.items())
                self._streams.clear()
            else:
                stream = self._streams.pop(device_name, None)
                streams = [(device_name, stream)] if stream is not None else []

        if device_name is None:
            log.info("[ChromecastService] Stopping all streams")
        for name, stream in streams:
            stream.stop()
            self._state.set_idle(name)


# -----------------------------------------------------------------------------
# Singleton instance
# -----------------------------------------------------------------------------

_instance: Optional[ChromecastService] = None
_lock = threading.Lock()


def get_chromecast_service() -> ChromecastService:
    """Get the global Chromecast service instance."""
    global _instance

    with _lock:
        if _instance is None:
            _instance = ChromecastService()
        return _instance
