"""
Chromecast connection state.

Tracks the streaming state of every active Chromecast device. The board can
stream to several devices at once, so this holds a per-device map of
``name -> {state, error}``. Connection management lives in the chromecast
service; widgets and the web mirror observe this state.

A single e-paper status-bar icon can only show one indicator, so the
``state``/``device_name``/``is_*`` properties expose an *aggregate* view over
all devices (priority: streaming > connecting/reconnecting > error > idle),
while ``snapshot()`` exposes the full per-device list for callers that can show
more than one (the web UI and the board's Chromecast menu).
"""

import logging
from typing import Optional, Callable, List, Dict

log = logging.getLogger(__name__)


# Streaming states
STATE_IDLE = 0
STATE_CONNECTING = 1
STATE_STREAMING = 2
STATE_RECONNECTING = 3
STATE_ERROR = 4

# States that count as "active" (occupying a device / trying to stream).
_ACTIVE_STATES = (STATE_CONNECTING, STATE_STREAMING, STATE_RECONNECTING)

# Aggregate priority for the single status-bar icon: the first state present
# (scanning this order) wins. Streaming beats in-progress beats error.
_AGGREGATE_PRIORITY = (
    STATE_STREAMING,
    STATE_CONNECTING,
    STATE_RECONNECTING,
    STATE_ERROR,
)


class ChromecastState:
    """Observable multi-device Chromecast connection state.

    Holds one entry per device the board is streaming to (or attempting to),
    keyed by friendly name. Observers are notified on any change.
    """

    def __init__(self):
        """Initialize with no active devices."""
        # name -> {"state": int, "error": Optional[str]}. Insertion order is
        # preserved (Python dict), which keeps the web/menu lists stable.
        self._devices: Dict[str, Dict[str, object]] = {}
        self._observers: List[Callable[[], None]] = []

    # -------------------------------------------------------------------------
    # Aggregate view (single status-bar icon + back-compat single-device API)
    # -------------------------------------------------------------------------

    @property
    def state(self) -> int:
        """Representative state for the single status indicator.

        Picks the highest-priority state across all devices so the icon shows
        a live stream over an in-progress connection over an error.
        """
        present = {d["state"] for d in self._devices.values()}
        for candidate in _AGGREGATE_PRIORITY:
            if candidate in present:
                return candidate
        return STATE_IDLE

    @property
    def device_name(self) -> Optional[str]:
        """Name of an active device (first active, by start order), or None.

        Back-compat single-value accessor; multi-device callers use snapshot().
        """
        for name, info in self._devices.items():
            if info["state"] in _ACTIVE_STATES:
                return name
        return None

    @property
    def error_message(self) -> Optional[str]:
        """First device error message, or None if no device is errored."""
        for info in self._devices.values():
            if info["state"] == STATE_ERROR and info["error"]:
                return info["error"]  # type: ignore[return-value]
        return None

    @property
    def is_active(self) -> bool:
        """True if any device is streaming or attempting to stream."""
        return any(info["state"] in _ACTIVE_STATES for info in self._devices.values())

    @property
    def is_streaming(self) -> bool:
        """True if any device is actively streaming."""
        return any(info["state"] == STATE_STREAMING for info in self._devices.values())

    @property
    def is_idle(self) -> bool:
        """True if no device is tracked (nothing active or errored)."""
        return not self._devices

    @property
    def is_error(self) -> bool:
        """True only if there are devices and ALL of them are errored.

        A single errored device while another streams is not an aggregate
        error; the live stream takes precedence for the status icon.
        """
        return bool(self._devices) and all(
            info["state"] == STATE_ERROR for info in self._devices.values()
        )

    # -------------------------------------------------------------------------
    # Per-device view
    # -------------------------------------------------------------------------

    def snapshot(self) -> List[Dict[str, object]]:
        """Return an ordered list of ``{name, state, error}`` per device.

        ``state`` is the integer constant; the connectivity layer maps it to a
        stable string for the web payload.
        """
        return [
            {"name": name, "state": info["state"], "error": info["error"]}
            for name, info in self._devices.items()
        ]

    def active_device_names(self) -> List[str]:
        """Names of devices currently streaming/connecting/reconnecting."""
        return [
            name
            for name, info in self._devices.items()
            if info["state"] in _ACTIVE_STATES
        ]

    # -------------------------------------------------------------------------
    # Observer management
    # -------------------------------------------------------------------------

    def add_observer(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked (no args) on any state change."""
        if callback not in self._observers:
            self._observers.append(callback)

    def remove_observer(self, callback: Callable[[], None]) -> None:
        """Remove a previously registered callback."""
        if callback in self._observers:
            self._observers.remove(callback)

    def _notify(self) -> None:
        """Notify all observers.

        A failing observer is logged and skipped so one bad observer cannot
        break notification of the others (previously the error was swallowed
        silently, hiding observer bugs).
        """
        for callback in self._observers:
            try:
                callback()
            except Exception:
                log.exception("Chromecast observer callback failed")

    # -------------------------------------------------------------------------
    # State mutations (called by the chromecast service, per device)
    # -------------------------------------------------------------------------

    def _set(self, device_name: str, state: int, error: Optional[str]) -> None:
        self._devices[device_name] = {"state": state, "error": error}
        self._notify()

    def set_connecting(self, device_name: str) -> None:
        """Mark a device as connecting."""
        self._set(device_name, STATE_CONNECTING, None)

    def set_streaming(self, device_name: str) -> None:
        """Mark a device as actively streaming."""
        self._set(device_name, STATE_STREAMING, None)

    def set_reconnecting(self, device_name: str) -> None:
        """Mark a device as reconnecting after a lost connection."""
        self._set(device_name, STATE_RECONNECTING, None)

    def set_error(self, device_name: str, message: str) -> None:
        """Mark a device as errored with a short message."""
        self._set(device_name, STATE_ERROR, message)

    def set_idle(self, device_name: Optional[str] = None) -> None:
        """Stop tracking a device, or all devices when ``device_name`` is None.

        Removing the entry (rather than keeping a STATE_IDLE row) keeps the
        snapshot free of stale "not streaming" rows after a stop.
        """
        if device_name is None:
            if not self._devices:
                return
            self._devices.clear()
        else:
            if device_name not in self._devices:
                return
            del self._devices[device_name]
        self._notify()


# -----------------------------------------------------------------------------
# Singleton instance
# -----------------------------------------------------------------------------

_instance: Optional[ChromecastState] = None


def get_chromecast() -> ChromecastState:
    """Get the singleton ChromecastState instance."""
    global _instance
    if _instance is None:
        _instance = ChromecastState()
    return _instance


def reset_chromecast() -> ChromecastState:
    """Reset the singleton to a fresh instance (primarily for testing)."""
    global _instance
    _instance = ChromecastState()
    return _instance
