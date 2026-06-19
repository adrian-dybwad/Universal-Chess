"""Live Bluetooth status engine (board process).

The board (main) process owns the :class:`~universalchess.managers.ble.BleManager`
and the RFCOMM server, so it is the only process that knows, moment to moment:

* whether the BLE advertisements registered (and therefore whether phone chess
  apps can discover the board at all),
* whether a chess app is connected and *which emulator* is in play
  (Millennium / Pegasus / Chessnut) over which transport (BLE or RFCOMM),
* which Bluetooth devices (phones, keyboards) are connected at the OS level.

All of that changes continuously and from outside our own calls -- a central
connecting pauses LE advertising, ``bluetoothd`` restarts, and
``btmgmt``/``bluetoothctl``/``rfkill`` may be run in a console. A one-shot
snapshot (or a value written only when *we* register/release) drifts. This
engine is a single, thread-safe, in-process source of truth that is mutated from
every one of those event sources and broadcasts the full picture to the web on
every change (see :data:`EVENT_TYPE`). The web caches the last payload and
re-emits it over SSE, so both surfaces stay live without polling.

The advertising sub-block reuses
:func:`universalchess.managers.ble_advertising_status.make_status` so the
``expected``/``registered``/``failed``/``ok``/``error``/``names`` schema (and the
failure wording) cannot drift from what the rest of the code already expects.
"""

import threading
import time
from typing import Callable, Dict, List, Optional

from universalchess.managers.ble_advertising_status import make_status
from universalchess.managers.bluez_patch_status import (
    heal_label,
    make_progress,
    unknown_status as stack_unknown_status,
)

# Event type used for the board -> web broadcast (over the game socket) and the
# SSE message the web forwards. Kept here so the publisher and both consumers
# (web subscriber cache, React card) reference one constant.
EVENT_TYPE = "bt_status"

# Closed set of advertising states. ``adv_state`` is unambiguous where
# ``ActiveInstances`` alone is not (0 means both "registration failed" and
# "paused because a central is connected").
ADV_ADVERTISING = "advertising"        # all adverts registered, none paused
ADV_PAUSED_CONNECTED = "paused_connected"  # a BLE central is connected (LE adverts pause)
ADV_HEALING = "healing"                # self-heal running: advertising is being repaired
ADV_FAILED = "failed"                  # BlueZ rejected one or more adverts
ADV_RADIO_OFF = "radio_off"            # adapter unpowered or radio soft-blocked
ADV_UNKNOWN = "unknown"                # nothing attempted yet / still pending

# Link transports.
TRANSPORT_BLE = "ble"
TRANSPORT_RFCOMM = "rfcomm"


class BluetoothStatusState:
    """Thread-safe live Bluetooth status, fed by BleManager/RFCOMM/BlueZ events.

    Every mutator updates the state under a lock and then broadcasts a full
    snapshot via the injected ``broadcast`` callback. The callback is injected
    (defaulting, in the singleton, to the game broadcaster) so the engine has no
    hard dependency on the IPC layer and is trivially testable: a fake callback
    records each emitted snapshot.
    """

    def __init__(
        self,
        broadcast: Optional[Callable[[str, dict], None]] = None,
        clock: Callable[[], float] = time.time,
    ):
        """Create the engine.

        Args:
            broadcast: ``broadcast(event_type, payload)`` invoked on every
                change with :data:`EVENT_TYPE` and :meth:`to_dict`. ``None``
                disables broadcasting (used in tests that only read state).
            clock: Source of ``connected_since`` timestamps (injectable so a
                test can assert a deterministic value).
        """
        self._lock = threading.RLock()
        self._broadcast = broadcast
        self._clock = clock
        # In-process observers (e.g. the open board menu) notified on every
        # change so they can redraw live. Separate from the IPC ``broadcast``
        # sink, which carries the state to the web process.
        self._observers: List[Callable[[], None]] = []

        # Radio. Default powered/enabled True so the brief window before the
        # adapter is probed does not render as ``radio_off``; the real values
        # arrive from BleManager.start() and the Adapter1 ``Powered`` signal.
        self._powered = True
        self._enabled = True
        self._active_instances: Optional[int] = None

        # Advertising registration counters (the board's intent + BlueZ result).
        self._expected = 0
        self._registered = 0
        self._failed = 0
        self._error: Optional[str] = None
        self._names: List[str] = []

        # Link / active app.
        self._connected = False
        self._transport: Optional[str] = None
        self._emulator: Optional[str] = None
        self._peer: Optional[dict] = None
        self._connected_since: Optional[float] = None

        # OS-level connected devices, keyed by address so repeated signals
        # de-duplicate and a name that resolves later overwrites the address.
        self._devices: Dict[str, str] = {}

        # Whether the board runs a patched (non-stock) bluetoothd. A static-ish
        # system fact (set once at BLE bring-up from the self-heal marker), not
        # an event stream, but carried in the same snapshot so the web and the
        # device screen can warn about the deviation over the existing channel.
        # Defaults to unknown (non-alarming) until the marker is read.
        self._stack: dict = stack_unknown_status()

        # Whether the bluez self-heal is actively running (and which phase). Fed
        # by a lightweight poll of the self-heal progress file. While true, the
        # derived adv_state is ADV_HEALING so the UI shows "repairing advertising"
        # instead of the bare ADV_FAILED that stock BlueZ produces during the
        # multi-minute on-board rebuild.
        self._healing = False
        self._heal_phase: Optional[str] = None

    # -- advertising registration ----------------------------------------

    def begin_advertising(self, expected: int, names: List[str]) -> None:
        """Reset counters for a fresh registration of ``expected`` adverts.

        Called when :class:`BleManager` (re)registers its advertisements so a
        reader during the brief async window sees ``failed == 0`` (pending, not
        a false failure) rather than a stale prior result.
        """
        with self._lock:
            self._expected = int(expected)
            self._registered = 0
            self._failed = 0
            self._error = None
            self._names = list(names)
        self._publish()

    def advertisement_registered(self) -> None:
        """Record that BlueZ accepted one advertisement."""
        with self._lock:
            self._registered += 1
        self._publish()

    def advertisement_failed(self, error) -> None:
        """Record that BlueZ rejected one advertisement, keeping the reason.

        The common cause is the service user lacking passwordless ``btmgmt``
        access, leaving the controller un-configured for LE adverts; surfacing
        the error explains *why* apps cannot discover the board.
        """
        with self._lock:
            self._failed += 1
            self._error = str(error)
        self._publish()

    def advertisement_released(self) -> None:
        """Record that BlueZ dropped a previously-registered advertisement.

        BlueZ calls ``LEAdvertisement1.Release`` when it tears an advert down
        (e.g. on a controller reset); the active count drops so the state can
        leave ``advertising`` without us re-registering.
        """
        with self._lock:
            if self._registered > 0:
                self._registered -= 1
        self._publish()

    # -- radio -----------------------------------------------------------

    def set_powered(self, powered: bool) -> None:
        """Update adapter power (from BleManager.start / Adapter1 signal)."""
        with self._lock:
            changed = self._powered != bool(powered)
            self._powered = bool(powered)
        if changed:
            self._publish()

    def set_enabled(self, enabled: bool) -> None:
        """Update radio soft-block state (rfkill)."""
        with self._lock:
            changed = self._enabled != bool(enabled)
            self._enabled = bool(enabled)
        if changed:
            self._publish()

    def set_active_instances(self, count: Optional[int]) -> None:
        """Record ``LEAdvertisingManager1.ActiveInstances`` (informational).

        Stored for diagnostics; ``adv_state`` is derived from the registration
        result and connection state, not from this value, because 0 is
        ambiguous (failed vs paused-because-connected).
        """
        with self._lock:
            changed = self._active_instances != count
            self._active_instances = count
        if changed:
            self._publish()

    # -- link / active app ----------------------------------------------

    def client_connected(
        self,
        transport: str,
        emulator: Optional[str] = None,
        peer: Optional[dict] = None,
    ) -> None:
        """Record that a chess app connected over ``transport``.

        Args:
            transport: :data:`TRANSPORT_BLE` or :data:`TRANSPORT_RFCOMM`.
            emulator: Active emulator for a BLE link (``millennium`` /
                ``pegasus`` / ``chessnut``); ``None`` for RFCOMM.
            peer: Optional ``{"address", "name"}`` of the connected device.
        """
        with self._lock:
            self._connected = True
            self._transport = transport
            self._emulator = emulator
            self._peer = dict(peer) if peer else None
            self._connected_since = self._clock()
        self._publish()

    def client_disconnected(self) -> None:
        """Clear the link when the chess app disconnects."""
        with self._lock:
            self._connected = False
            self._transport = None
            self._emulator = None
            self._peer = None
            self._connected_since = None
        self._publish()

    # -- OS-level devices ------------------------------------------------

    def device_connected(self, address: str, name: Optional[str] = None) -> None:
        """Record a BlueZ device connecting (phone, keyboard, ...)."""
        if not address:
            return
        with self._lock:
            self._devices[address] = name or address
        self._publish()

    def device_disconnected(self, address: str) -> None:
        """Record a BlueZ device disconnecting."""
        with self._lock:
            existed = self._devices.pop(address, None) is not None
        if existed:
            self._publish()

    def set_devices(self, devices: List[dict]) -> None:
        """Replace the connected-device set (e.g. from an initial enumeration)."""
        with self._lock:
            self._devices = {
                d["address"]: (d.get("name") or d["address"])
                for d in devices
                if d.get("address")
            }
        self._publish()

    # -- bluetooth stack (patched vs stock) ------------------------------

    def set_stack_status(self, status: dict) -> None:
        """Record the active bluetoothd stack (patched vs stock).

        Fed once at BLE bring-up from
        :func:`universalchess.managers.bluez_patch_status.read_status`. Broadcasts
        only on change so a re-set with the same value does not churn the web.
        """
        with self._lock:
            changed = self._stack != status
            self._stack = dict(status)
        if changed:
            self._publish()

    # -- self-heal progress ----------------------------------------------

    def set_heal_status(self, running: bool, phase: Optional[str] = None) -> None:
        """Record whether the bluez self-heal is running (and its phase).

        Fed by a poll of the self-heal progress file. Broadcasts only on change
        so the periodic poll does not churn the live web view when nothing moved.
        While running, :meth:`_derive_adv_state` reports ``healing`` so the bare
        advertising failure that stock BlueZ produces mid-rebuild is replaced by
        a "repairing advertising" message.
        """
        running = bool(running)
        phase = phase if running else None
        with self._lock:
            changed = self._healing != running or self._heal_phase != phase
            self._healing = running
            self._heal_phase = phase
        if changed:
            self._publish()

    # -- derived state + serialization -----------------------------------

    def _derive_adv_state(self) -> str:
        """Fuse registration result, radio, connection, and self-heal into one state.

        Order matters:

        * a powered-off radio overrides everything;
        * a connected BLE central means LE advertising is paused (so 0 active
          instances is expected, not a failure);
        * a fully-registered, none-failed result is healthy -- reported even
          while a heal happens to be running, so a working state is never hidden;
        * an active self-heal reports ``healing`` -- it takes precedence over
          ``failed``/``unknown`` because the bare failure stock BlueZ produces
          during the on-board rebuild is exactly what the heal is repairing;
        * a recorded rejection (no heal running) is the failure that hides the
          board from scans;
        * anything else (nothing attempted, still pending) is unknown.
        """
        if not self._powered or not self._enabled:
            return ADV_RADIO_OFF
        if self._connected and self._transport == TRANSPORT_BLE:
            return ADV_PAUSED_CONNECTED
        if self._expected > 0 and self._registered >= self._expected:
            return ADV_ADVERTISING
        if self._healing:
            return ADV_HEALING
        if self._failed > 0:
            return ADV_FAILED
        return ADV_UNKNOWN

    def to_dict(self) -> dict:
        """Return a JSON-serializable snapshot of the full live status."""
        with self._lock:
            return {
                "enabled": self._enabled,
                "powered": self._powered,
                "active_instances": self._active_instances,
                "advertising": make_status(
                    self._expected, self._registered, self._failed,
                    error=self._error, names=self._names,
                ),
                "advertised_names": list(self._names),
                "adv_state": self._derive_adv_state(),
                "connected": self._connected,
                "transport": self._transport,
                "emulator": self._emulator,
                "peer": dict(self._peer) if self._peer else None,
                "connected_since": self._connected_since,
                "devices": [
                    {"address": addr, "name": name}
                    for addr, name in self._devices.items()
                ],
                "stack": dict(self._stack),
                "heal": self._heal_block(),
            }

    def _heal_block(self) -> dict:
        """Build the self-heal sub-block (running/phase + shared label).

        Carries the pre-formatted ``label`` so the web card and the device
        screen render identical wording without each re-deriving it. Caller holds
        the lock.
        """
        progress = make_progress(self._healing, self._heal_phase)
        return {
            "running": progress["running"],
            "phase": progress["phase"],
            "label": heal_label(progress),
        }

    def add_observer(self, callback: Callable[[], None]) -> None:
        """Register an in-process observer notified on every state change.

        Used by the open board Bluetooth menu to redraw live; the callback takes
        no arguments and reads the current state via :meth:`to_dict`.
        """
        with self._lock:
            self._observers.append(callback)

    def remove_observer(self, callback: Callable[[], None]) -> None:
        """Unregister an observer (idempotent)."""
        with self._lock:
            if callback in self._observers:
                self._observers.remove(callback)

    def republish(self) -> None:
        """Re-broadcast the current snapshot without changing state.

        Answers a web-side ``request_bt_status`` (web mounted/restarted with no
        cached snapshot): the board -> web broadcast is one-way with no replay,
        so this pushes the current state on demand.
        """
        self._publish()

    def _publish(self) -> None:
        """Emit a snapshot to the IPC sink and in-process observers.

        Snapshots under the lock then notifies outside it. Status reporting must
        never break BLE bring-up, so a broadcast/observer failure is swallowed
        (the web falls back to a request/response resync; the menu to its poll).
        """
        payload = self.to_dict()
        if self._broadcast is not None:
            try:
                self._broadcast(EVENT_TYPE, payload)
            except Exception:  # noqa: BLE001 - reporting must not break BT
                pass
        with self._lock:
            observers = list(self._observers)
        for observer in observers:
            try:
                observer()
            except Exception:  # noqa: BLE001 - an observer must not break BT
                pass


# -----------------------------------------------------------------------------
# Singleton
# -----------------------------------------------------------------------------

_state: Optional[BluetoothStatusState] = None


def _default_broadcast(event_type: str, payload: dict) -> None:
    """Broadcast via the game broadcaster (board -> web over the game socket).

    Imported lazily so importing this module never pulls in the IPC layer (keeps
    it usable from tests and tools without a socket).
    """
    from universalchess.services.game_broadcast import get_broadcaster

    get_broadcaster().broadcast_event(event_type, payload)


def get_bluetooth_status_state() -> BluetoothStatusState:
    """Return the process-wide engine, wired to the game broadcaster on first use."""
    global _state
    if _state is None:
        _state = BluetoothStatusState(broadcast=_default_broadcast)
    return _state
