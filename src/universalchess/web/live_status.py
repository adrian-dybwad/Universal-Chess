"""One sampler for the browser's persistent ``/events`` connection.

The navbar, the connectivity page, and the engine cards each used to poll
activity, Wi-Fi, Bluetooth, update status, and engine-install status on their
own timers. Every open tab repeated those requests. This module reads each
source once, on its own cadence, while at least one browser holds ``/events``,
and publishes a single ``live_status`` event when the combined snapshot
changes. The browsers already keep that connection open for game, battery,
and clock updates.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


EVENT_TYPE = "live_status"

# Activity percent is computed at read time and creeps during an install, so
# it is sampled often. Radio status shells out (ip, rfkill, BlueZ) and update
# status asks systemd whether an install unit is active, so those stay on the
# slower cadence the UI already used.
ACTIVITY_INTERVAL_SECONDS = 4.0
RADIO_INTERVAL_SECONDS = 10.0
UPDATES_INTERVAL_SECONDS = 10.0
# Install percent is computed from the clock at read time, so the build bar
# only moves when this source is read. Two seconds is the cadence the Settings
# cards used while an install was in flight. An idle dict does not change, so
# the pump does not publish it again.
ENGINE_INTERVAL_SECONDS = 2.0

# How often the pump thread wakes to see which sources are due. Shorter than
# the fastest source interval so a due read is not delayed by a full period.
PUMP_TICK_SECONDS = 1.0


class _Source:
    """Last value of one live-status source, refreshed when its interval elapses."""

    def __init__(self, read: Callable[[], object], interval_seconds: float):
        self._read = read
        self._interval_seconds = interval_seconds
        self._last_read_at: Optional[float] = None
        self.value: object = None

    def read_if_due(self, now: float) -> object:
        """Return the cached value, reading again only when the interval has elapsed.

        The first call always reads. A clock that jumps backwards reads again
        rather than waiting out a negative remaining interval.
        """
        if (
            self._last_read_at is not None
            and now >= self._last_read_at
            and now - self._last_read_at < self._interval_seconds
        ):
            return self.value
        self.value = self._read()
        self._last_read_at = now
        return self.value


class LiveStatusSchedule:
    """Assemble one ``live_status`` snapshot, reading each source only when due.

    A radio the board does not have is reported as ``None`` and is not read.
    That keeps a board with no wireless hardware from shelling out to ``rfkill``
    or BlueZ on every tick.
    """

    def __init__(
        self,
        *,
        read_activity: Callable[[], dict],
        read_engine: Callable[[], dict],
        read_wifi: Callable[[], dict],
        read_bluetooth: Callable[[], dict],
        read_updates: Callable[[], dict],
        read_capability: Callable[[], object],
        clock: Callable[[], float] = time.monotonic,
        activity_interval_seconds: float = ACTIVITY_INTERVAL_SECONDS,
        engine_interval_seconds: float = ENGINE_INTERVAL_SECONDS,
        radio_interval_seconds: float = RADIO_INTERVAL_SECONDS,
        updates_interval_seconds: float = UPDATES_INTERVAL_SECONDS,
    ):
        self._activity = _Source(read_activity, activity_interval_seconds)
        self._engine = _Source(read_engine, engine_interval_seconds)
        self._wifi = _Source(read_wifi, radio_interval_seconds)
        self._bluetooth = _Source(read_bluetooth, radio_interval_seconds)
        self._updates = _Source(read_updates, updates_interval_seconds)
        self._read_capability = read_capability
        self._clock = clock

    def snapshot(self) -> dict:
        """Return the current combined snapshot, refreshing due sources."""
        now = self._clock()
        capability = self._read_capability()
        has_wifi = bool(getattr(capability, "has_wifi", False))
        has_bluetooth = bool(getattr(capability, "has_bluetooth", False))
        activity = self._activity.read_if_due(now)
        engine = self._engine.read_if_due(now)
        wifi = self._wifi.read_if_due(now) if has_wifi else None
        bluetooth = self._bluetooth.read_if_due(now) if has_bluetooth else None
        updates = self._updates.read_if_due(now)
        return {
            "type": EVENT_TYPE,
            "activity": activity,
            "engine": engine,
            "wifi": wifi,
            "bluetooth": bluetooth,
            "updates": updates,
        }


class LiveStatusPump:
    """Sample live status while at least one SSE client is connected.

    ``snapshot`` returns the full event dict. ``publish`` receives it only when
    it differs from the previous sample, so an idle board does not write the
    stream on every tick. ``attach`` / ``detach`` count clients; the thread
    runs only while the count is positive.
    """

    def __init__(
        self,
        snapshot: Callable[[], dict],
        publish: Callable[[dict], None],
        *,
        tick_seconds: float = PUMP_TICK_SECONDS,
    ):
        self._snapshot = snapshot
        self._publish = publish
        self._tick_seconds = tick_seconds
        self._lock = threading.Lock()
        self._latest: Optional[dict] = None
        self._clients = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def latest(self) -> Optional[dict]:
        """The last snapshot published or sampled, or ``None`` before the first."""
        with self._lock:
            return self._latest

    def prime(self, snapshot: dict) -> None:
        """Store a snapshot without publishing. Used to seed a reconnecting client."""
        with self._lock:
            self._latest = snapshot

    def attach(self) -> None:
        """Count one SSE client and start sampling if this is the first."""
        with self._lock:
            self._clients += 1
            already_running = self._thread is not None and self._thread.is_alive()
            if already_running:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="live-status",
                daemon=True,
            )
            self._thread.start()

    def detach(self) -> None:
        """Count one SSE client gone and stop sampling when none remain."""
        with self._lock:
            self._clients = max(0, self._clients - 1)
            if self._clients == 0:
                self._stop.set()

    def run_once(self) -> bool:
        """Sample once. Publish and return True only when the snapshot changed."""
        snapshot = self._snapshot()
        with self._lock:
            changed = snapshot != self._latest
            self._latest = snapshot
        if changed:
            self._publish(snapshot)
        return changed

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("[LiveStatus] sample failed")
            if self._stop.wait(self._tick_seconds):
                return


_pump: Optional[LiveStatusPump] = None
_pump_lock = threading.Lock()


def configure_live_status_pump(snapshot: Callable[[], dict], publish: Callable[[dict], None]) -> LiveStatusPump:
    """Install the process-wide pump. Called once when the web app starts."""
    global _pump
    with _pump_lock:
        _pump = LiveStatusPump(snapshot, publish)
        return _pump


def get_live_status_pump() -> Optional[LiveStatusPump]:
    """The process-wide pump, or ``None`` when the web app has not configured it."""
    return _pump


def sample_live_status(schedule: Optional[LiveStatusSchedule] = None) -> dict:
    """Read the board's live sources into one ``live_status`` snapshot.

    ``schedule`` is injected in tests. The default schedule reads the same
    objects the old poll endpoints read: install/import/heal state, Wi-Fi,
    Bluetooth, and the update service.
    """
    if schedule is None:
        schedule = _default_schedule()
    return schedule.snapshot()


_default: Optional[LiveStatusSchedule] = None


def _default_schedule() -> LiveStatusSchedule:
    global _default
    if _default is None:
        _default = LiveStatusSchedule(
            read_activity=_read_activity,
            read_engine=_read_engine,
            read_wifi=_read_wifi,
            read_bluetooth=_read_bluetooth,
            read_updates=_read_updates,
            read_capability=_read_capability,
        )
    return _default


def _read_activity() -> dict:
    from universalchess.managers.bluez_patch_status import read_progress
    from universalchess.services.background_activity import activity_snapshot
    from universalchess.services.centaur_import.import_state import STORE as import_store
    from universalchess.services.engine_install_state import STORE as engine_store

    return activity_snapshot(
        engine_store.status_dict(),
        read_progress(),
        import_store.status_dict(),
    )


def _read_engine() -> dict:
    from universalchess.services.engine_install_state import STORE as engine_store

    return engine_store.status_dict()


def _read_wifi() -> dict:
    from universalchess.epaper.wifi_info import get_wifi_status

    return get_wifi_status()


def _read_bluetooth() -> dict:
    from universalchess.connectivity import bluetooth as bt

    return bt.get_status()


def _read_updates() -> dict:
    from universalchess.services.update_service import get_update_service

    return get_update_service().get_status_dict()


def _read_capability() -> object:
    from universalchess.board.wireless_capability import get_wireless_capability

    return get_wireless_capability()
