"""UI-agnostic Chromecast helpers shared by the board and the web app.

Two concerns live here:

  * Discovery (``discover``) is a stateless mDNS scan that can run in either
    process; the web app calls it directly to populate its device picker.
  * Status mapping (``status_payload``) turns the observable ChromecastState into
    a plain dict for broadcasting to the web.

Starting/stopping a stream is deliberately NOT here: the active stream is owned
by the ChromecastService singleton in the board (main) process, which also
writes the e-paper snapshots the stream serves. The web app issues start/stop as
board commands rather than instantiating a second, conflicting streamer.
"""

import logging
from typing import List, Optional

from universalchess.state.chromecast import (
    STATE_IDLE,
    STATE_CONNECTING,
    STATE_STREAMING,
    STATE_RECONNECTING,
    STATE_ERROR,
)

_DEFAULT_LOG = logging.getLogger(__name__)

# pychromecast classifies each device by cast_type: "cast" is a video-capable
# device (Chromecast / Chromecast-built-in TV), while "audio" (e.g. Google Home,
# Nest Audio) and "group" (audio groups) cannot display the board's video feed.
# Exclude the audio types so the picker only offers devices that can actually
# show the stream. A None/unknown cast_type is kept rather than dropped, so an
# older pychromecast that does not report the type still lists video devices.
_NON_VIDEO_CAST_TYPES = {"audio", "group"}

# Stable string names for the numeric states, for a JSON status payload the web
# can switch on without depending on the integer constants.
STATE_NAMES = {
    STATE_IDLE: "idle",
    STATE_CONNECTING: "connecting",
    STATE_STREAMING: "streaming",
    STATE_RECONNECTING: "reconnecting",
    STATE_ERROR: "error",
}


def _resolve_log(log: Optional[logging.Logger]) -> logging.Logger:
    return log if log is not None else _DEFAULT_LOG


def _video_cast_name(cc) -> Optional[str]:
    """Return a discovered cast's friendly name if it is video-capable, else None.

    Reads the friendly name and cast_type from whichever attribute the installed
    pychromecast exposes (``cast_info`` on newer releases, ``device`` on older).
    Audio-only devices and audio groups are excluded; see _NON_VIDEO_CAST_TYPES.
    """
    info = getattr(cc, "cast_info", None) or getattr(cc, "device", None)
    name = getattr(info, "friendly_name", None)
    cast_type = getattr(info, "cast_type", None)
    if not name or cast_type in _NON_VIDEO_CAST_TYPES:
        return None
    return name


def discover(timeout: int = 10, log: Optional[logging.Logger] = None) -> List[str]:
    """Discover video-capable Chromecast device names on the network.

    Runs a bounded pychromecast scan and always stops the discovery browser so it
    does not leak a background mDNS listener. Audio-only devices and audio groups
    are filtered out (they cannot display the board's video feed). Returns a
    sorted, de-duplicated list of friendly names, or an empty list if pychromecast
    is unavailable or the scan fails (the caller surfaces "no devices found").
    """
    log = _resolve_log(log)
    try:
        import pychromecast
    except ImportError as e:
        log.warning(f"[Chromecast] pychromecast unavailable: {e}")
        return []

    browser = None
    try:
        chromecasts, browser = pychromecast.get_chromecasts(timeout=timeout)
        names = {name for cc in chromecasts if (name := _video_cast_name(cc))}
        return sorted(names)
    except Exception as e:  # noqa: BLE001 - discovery is best-effort
        log.warning(f"[Chromecast] Discovery failed: {e}")
        return []
    finally:
        if browser is not None:
            try:
                browser.stop_discovery()
            except Exception:
                pass


def status_payload(chromecast_state) -> dict:
    """Map a ChromecastState to a broadcastable dict for the web.

    The board can stream to several devices at once, so the payload carries a
    per-device ``devices`` list (each ``{name, state, error}`` with ``state`` a
    stable string). The top-level ``state``/``device``/``error`` are an aggregate
    view (used by the status icon and any single-value consumer): ``state`` is
    the highest-priority device state, falling back to "idle". Unknown numeric
    states map to "idle" so the web never receives an undefined state.
    """
    devices = [
        {
            "name": entry["name"],
            "state": STATE_NAMES.get(entry["state"], "idle"),
            "error": entry["error"],
        }
        for entry in chromecast_state.snapshot()
    ]
    return {
        "state": STATE_NAMES.get(chromecast_state.state, "idle"),
        "device": chromecast_state.device_name,
        "error": chromecast_state.error_message,
        "devices": devices,
    }
