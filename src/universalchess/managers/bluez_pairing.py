# BlueZ Host Pairing Manager
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""Host-initiated Bluetooth keyboard discovery and pairing via the BlueZ D-Bus API.

This implements the standard BlueZ flow for pairing a Classic (BR/EDR) HID
keyboard to the board acting as host, with no ``bluetoothctl``/``btmgmt``/
``hcitool`` subprocesses:

    Adapter1.SetDiscoveryFilter(Transport="auto")   # inquiry + LE scan
    Adapter1.StartDiscovery()                        # BlueZ creates Device1 objects
    <enumerate via ObjectManager, keep keyboard-class devices>
    Adapter1.StopDiscovery()
    Device1.Pair()                                   # serviced by the default agent
    Device1.Trusted = True
    Device1.Connect()                                # BlueZ exposes the HID input

Why the discovery filter matters: BlueZ's discovery transport must be set
explicitly, otherwise discovery can be left in whatever mode a prior caller
configured. ``Transport="auto"`` runs both a BR/EDR inquiry and an LE scan, so
it finds Classic *and* Bluetooth Low Energy keyboards. (Verified on the Pi
controller: ``auto`` finds a Classic keyboard as fast as an explicit ``bredr``
filter, while additionally covering BLE keyboards.) This filter was the root
cause that earlier controller-level (btmgmt/hcitool) workarounds papered over.

Why discovery runs continuously: real keyboards answer a BR/EDR inquiry on their
own schedule and some advertise only intermittently, so a fixed-length scan can
miss one that simply had not responded yet. :meth:`discover_keyboards_stream`
keeps discovery running for the lifetime of the pairing screen and reports each
keyboard the moment it is seen, so an intermittently-discoverable keyboard still
surfaces instead of being missed by a one-shot window.

Pairing relies on the system default pairing agent (``KeyboardDisplay``)
registered by :class:`BleManager`, so a passkey is displayed on the board when a
keyboard requires one.

Asymmetric bonds: if the keyboard still holds a link key the board has cleared,
``Pair()`` fails with ``AuthenticationFailed`` -- this is inherent BR/EDR
behaviour, not recoverable by re-issuing pairing alone. A well-behaved peer
forgets the stale key when that failed attempt connects then drops, so a single
retry then pairs cleanly.

The D-Bus calls are isolated in thin ``_dbus_*`` primitives so the discovery and
pairing orchestration can be unit-tested without a live bus or the ``dbus``
package.
"""

import re
import subprocess
import time
from typing import Callable, Dict, List, Optional

from universalchess.board.logging import log

BLUEZ_SERVICE = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

# Bluetooth Class of Device decoding for keyboards.
_COD_MAJOR_PERIPHERAL = 0x05          # major device class (bits 8-12) == peripheral
_COD_KEYBOARD_BIT = 0x40              # peripheral minor bit indicating a keyboard
# BLE GAP appearance value for a HID keyboard.
_APPEARANCE_KEYBOARD = 0x03C1

_MAC_ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

# Seconds to wait after a failed (auth) pair before retrying, giving a
# self-healing peer time to drop its stale link key after the dropped link.
_PEER_SELF_HEAL_DELAY_SECONDS = 2.0

# Seconds between ObjectManager polls while discovery is running. Sets the upper
# bound on both how quickly a newly-found keyboard is surfaced and how quickly a
# streaming scan reacts to its stop request.
_DISCOVERY_POLL_SECONDS = 0.5

CONNECT_OK = "ok"
CONNECT_AUTH_FAILED = "auth_failed"
CONNECT_FAILED = "failed"


class BluezPairingManager:
    """Discover and pair Bluetooth keyboards through the BlueZ D-Bus API."""

    def __init__(self, adapter_name: str = "hci0"):
        self._adapter_name = adapter_name
        self._adapter_path = f"/org/bluez/{adapter_name}"
        self._bus = None

    # ------------------------------------------------------------------
    # Pure helpers (no D-Bus) -- directly unit-testable
    # ------------------------------------------------------------------
    @staticmethod
    def is_keyboard(properties: Dict[str, object]) -> bool:
        """Classify a BlueZ ``Device1`` property map as a keyboard.

        Uses, in order, the most reliable signals BlueZ exposes: the ``Icon``
        hint, the BLE ``Appearance`` value, then the Class of Device peripheral
        keyboard bit. Any one match is sufficient.
        """
        icon = properties.get("Icon")
        if icon is not None and "keyboard" in str(icon).lower():
            return True

        appearance = properties.get("Appearance")
        if appearance is not None:
            try:
                if int(appearance) == _APPEARANCE_KEYBOARD:
                    return True
            except (TypeError, ValueError):
                pass

        cod = properties.get("Class")
        if cod is not None:
            try:
                cod_int = int(cod)
            except (TypeError, ValueError):
                return False
            major = (cod_int >> 8) & 0x1F
            if major == _COD_MAJOR_PERIPHERAL and (cod_int & _COD_KEYBOARD_BIT):
                return True
        return False

    @staticmethod
    def _validate_mac_address(address: str) -> bool:
        return _MAC_ADDRESS_RE.match(address) is not None

    @staticmethod
    def _device_path(adapter_path: str, address: str) -> str:
        """Build the BlueZ device object path for an address under an adapter."""
        return f"{adapter_path}/dev_" + address.replace(":", "_").upper()

    # ------------------------------------------------------------------
    # Thin D-Bus primitives (mocked in tests)
    # ------------------------------------------------------------------
    def _bus_connection(self):
        import dbus
        if self._bus is None:
            self._bus = dbus.SystemBus()
        return self._bus

    def _adapter(self):
        import dbus
        return dbus.Interface(
            self._bus_connection().get_object(BLUEZ_SERVICE, self._adapter_path),
            ADAPTER_IFACE,
        )

    def _device(self, address: str):
        import dbus
        path = self._device_path(self._adapter_path, address)
        return dbus.Interface(
            self._bus_connection().get_object(BLUEZ_SERVICE, path), DEVICE_IFACE)

    def _device_properties(self, address: str):
        import dbus
        path = self._device_path(self._adapter_path, address)
        return dbus.Interface(
            self._bus_connection().get_object(BLUEZ_SERVICE, path), PROPERTIES_IFACE)

    def _set_discovery_filter(self) -> None:
        # BlueZ rejects SetDiscoveryFilter while a discovery is in progress, and
        # any pre-existing discovery may have been left in a mode set by a prior
        # caller. Stop first so this filter actually takes effect; otherwise the
        # intended inquiry/scan may never run. "auto" runs both a BR/EDR inquiry
        # and an LE scan, covering Classic and BLE keyboards.
        import dbus
        self._stop_discovery()
        self._adapter().SetDiscoveryFilter({"Transport": dbus.String("auto")})

    def _start_discovery(self) -> None:
        import dbus
        try:
            self._adapter().StartDiscovery()
        except dbus.exceptions.DBusException as exc:
            # An already-running discovery is fine to piggyback on.
            if "InProgress" not in str(exc):
                raise

    def _stop_discovery(self) -> None:
        import dbus
        try:
            self._adapter().StopDiscovery()
        except dbus.exceptions.DBusException as exc:
            log.debug(f"[BluezPairing] StopDiscovery: {exc}")

    def _managed_objects(self) -> Dict[str, Dict[str, Dict[str, object]]]:
        import dbus
        manager = dbus.Interface(
            self._bus_connection().get_object(BLUEZ_SERVICE, "/"),
            OBJECT_MANAGER_IFACE,
        )
        return manager.GetManagedObjects()

    def _is_paired(self, address: str) -> bool:
        import dbus
        try:
            return bool(self._device_properties(address).Get(DEVICE_IFACE, "Paired"))
        except dbus.exceptions.DBusException:
            return False

    def _device_exists(self, address: str) -> bool:
        """Return True when BlueZ currently has a Device1 object for ``address``."""
        path = self._device_path(self._adapter_path, address)
        return path in self._managed_objects()

    def _remove_device(self, address: str) -> bool:
        """Remove a device's BlueZ object and bond. Returns success.

        Used both by the pairing flow (which clears a stale bond before a fresh
        pair and ignores the result) and by :meth:`forget_device` (which reports
        the result to the UI).
        """
        import dbus
        path = self._device_path(self._adapter_path, address)
        try:
            self._adapter().RemoveDevice(path)
            return True
        except dbus.exceptions.DBusException as exc:
            log.debug(f"[BluezPairing] RemoveDevice {address}: {exc}")
            return False

    def _pair(self, address: str, timeout_seconds: float) -> str:
        """Pair a device. Returns "ok", "auth_failed", or "failed"."""
        import dbus
        try:
            # Synchronous call; the D-Bus reply timeout must exceed the pairing
            # time so a slow passkey exchange is not cut short.
            self._device(address).Pair(timeout=timeout_seconds)
            return "ok"
        except dbus.exceptions.DBusException as exc:
            name = exc.get_dbus_name() or ""
            if name.endswith("AuthenticationFailed"):
                return "auth_failed"
            if name.endswith("NoReply"):
                # BlueZ can complete the bond but fail to reply before the D-Bus
                # timeout expires. Trust the cached Paired flag in that case so
                # the caller still runs trust/connect and the UI reports the real
                # outcome rather than a false "Pairing failed".
                try:
                    if self._is_paired(address):
                        log.info(
                            f"[BluezPairing] Pair {address} timed out but "
                            "BlueZ reports Paired: yes"
                        )
                        return "ok"
                except Exception as state_exc:  # noqa: BLE001 - fall through to failed
                    log.debug(
                        f"[BluezPairing] Could not verify post-timeout pair "
                        f"state for {address}: {state_exc}"
                    )
            log.info(f"[BluezPairing] Pair {address} failed: {name or exc}")
            return "failed"

    def _set_trusted(self, address: str) -> None:
        import dbus
        try:
            self._device_properties(address).Set(
                DEVICE_IFACE, "Trusted", dbus.Boolean(True))
        except dbus.exceptions.DBusException as exc:
            log.warning(f"[BluezPairing] Could not trust {address}: {exc}")

    def _recent_connect_auth_failure(self, address: str, since_timestamp: float) -> bool:
        """Return True if bluetoothd logged an auth failure for this connect.

        BlueZ does not always surface the exact controller/profile error through
        the D-Bus ``Connect()`` exception. On the board/WiFi Key stale-bond case
        authoritative evidence is emitted by bluetoothd for the target address
        during the connect attempt:

        * ``Authentication Failed (0x05)`` when the controller reports the
          baseband authentication failure directly.
        * ``control_connect_cb() ... Invalid exchange (52)`` when HID control
          setup fails after the peer removed its bond but the board still has
          one saved.

        The markers are intentionally narrow so generic failures (out of range,
        NoReply, already connected elsewhere) do not trigger a stale-pairing
        confirmation.
        """
        since = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(max(0, since_timestamp - 1)))
        try:
            result = subprocess.run(
                ["journalctl", "-u", "bluetooth", "--since", since, "--no-pager"],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            log.debug(f"[BluezPairing] Could not inspect bluetoothd log: {exc}")
            return False
        output = ((result.stdout or "") + (result.stderr or "")).lower()
        return address.lower() in output and (
            "authentication failed (0x05)" in output
            or "control_connect_cb()" in output
            and "invalid exchange (52)" in output
        )

    def _connect_status(self, address: str, timeout_seconds: float) -> str:
        """Connect a device via BlueZ and return ok/auth_failed/failed.

        Thin dbus primitive shared by the best-effort post-pair connect and the
        user-initiated :meth:`connect_device`; isolating the dbus call keeps the
        orchestration unit-testable without a live bus.
        """
        import dbus
        started_at = time.time()
        try:
            self._device(address).Connect(timeout=timeout_seconds)
            return CONNECT_OK
        except dbus.exceptions.DBusException as exc:
            name = exc.get_dbus_name() or ""
            if name.endswith("AuthenticationFailed") or self._recent_connect_auth_failure(
                    address, started_at):
                log.info(f"[BluezPairing] Connect {address}: authentication failed")
                return CONNECT_AUTH_FAILED
            log.info(f"[BluezPairing] Connect {address}: {name or exc}")
            return CONNECT_FAILED

    def _do_connect(self, address: str, timeout_seconds: float) -> bool:
        """Connect a device via BlueZ. Returns True on success, False on error."""
        return self._connect_status(address, timeout_seconds) == CONNECT_OK

    def _do_disconnect(self, address: str, timeout_seconds: float) -> bool:
        """Disconnect a device via BlueZ. Returns True on success, False on error."""
        import dbus
        try:
            self._device(address).Disconnect(timeout=timeout_seconds)
            return True
        except dbus.exceptions.DBusException as exc:
            log.info(
                f"[BluezPairing] Disconnect {address}: {exc.get_dbus_name() or exc}")
            return False

    def _connect(self, address: str, timeout_seconds: float) -> None:
        # Best-effort connect after pairing: pairing + trust already succeeded
        # and a trusted HID device reconnects autonomously, so a transient
        # failure here is not fatal. The boolean result is intentionally ignored.
        self._do_connect(address, timeout_seconds)

    # ------------------------------------------------------------------
    # Orchestration (unit-testable via the primitives above)
    # ------------------------------------------------------------------
    def _scan(self, should_continue: "Callable[[], bool]",
              on_found: "Callable[[Dict[str, object]], None]") -> None:
        """Run a discovery, reporting each keyboard as it appears.

        Sets the discovery filter, starts discovery, then polls the object
        manager while ``should_continue()`` is true. ``on_found`` is called with
        ``{"address", "name"}`` the first time a keyboard is seen and again
        whenever its resolved name changes (a keyboard often appears with an
        address-only name before BlueZ resolves the friendly name). Discovery is
        always stopped on exit so the controller does not stay in inquiry mode.
        """
        try:
            self._set_discovery_filter()
        except Exception as exc:  # noqa: BLE001 - filter is best-effort
            log.warning(f"[BluezPairing] SetDiscoveryFilter failed: {exc}")
        self._start_discovery()

        emitted: Dict[str, str] = {}
        try:
            while should_continue():
                for _path, interfaces in self._managed_objects().items():
                    props = interfaces.get(DEVICE_IFACE)
                    if not props or not self.is_keyboard(props):
                        continue
                    address = str(props.get("Address", ""))
                    if not address:
                        continue
                    name = str(props.get("Name") or props.get("Alias") or address)
                    if emitted.get(address) == name:
                        continue
                    emitted[address] = name
                    on_found({"address": address, "name": name})
                time.sleep(_DISCOVERY_POLL_SECONDS)
        finally:
            self._stop_discovery()

    def discover_keyboards(self, timeout: int = 12) -> List[Dict[str, object]]:
        """Discover keyboard-class devices for a bounded window.

        Returns a list of ``{"address", "name"}`` dicts. Discovery runs for up to
        ``timeout`` seconds; results accumulate as BlueZ creates ``Device1``
        objects, so a slow-to-respond keyboard is still captured. Prefer
        :meth:`discover_keyboards_stream` for an interactive screen where a
        keyboard may appear at any time.
        """
        found: Dict[str, Dict[str, object]] = {}
        deadline = time.time() + timeout
        self._scan(
            lambda: time.time() < deadline,
            lambda device: found.__setitem__(str(device["address"]), device),
        )
        return list(found.values())

    def discover_keyboards_stream(
        self,
        on_found: "Callable[[Dict[str, object]], None]",
        stop_event,
    ) -> None:
        """Continuously discover keyboards until ``stop_event`` is set.

        ``on_found`` is called with ``{"address", "name"}`` for each keyboard the
        first time it is seen and again whenever its resolved name changes.
        Discovery runs for the lifetime of the pairing screen so an
        intermittently-discoverable keyboard is surfaced as soon as it responds,
        instead of being missed by a fixed-length scan.
        """
        self._scan(lambda: not stop_event.is_set(), on_found)

    def _ensure_device_present(self, address: str, timeout: int) -> bool:
        """Re-run a short discovery until the device object exists.

        After ``RemoveDevice`` the cached object is gone, so a fresh pairing must
        rediscover the device before ``Pair()`` has anything to act on.
        """
        path = self._device_path(self._adapter_path, address)
        try:
            self._set_discovery_filter()
        except Exception as exc:  # noqa: BLE001 - filter is best-effort
            log.warning(f"[BluezPairing] SetDiscoveryFilter failed: {exc}")
        self._start_discovery()
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if path in self._managed_objects():
                    return True
                time.sleep(0.5)
        finally:
            self._stop_discovery()
        return path in self._managed_objects()

    def _pair_trust_connect(self, address: str, pair_timeout: float = 30.0,
                            connect_timeout: float = 30.0) -> str:
        """One pair/trust/connect cycle. Returns the ``_pair`` status string."""
        status = self._pair(address, pair_timeout)
        if status != "ok":
            return status
        self._set_trusted(address)
        self._connect(address, connect_timeout)
        return "ok"

    def pair_keyboard(self, address: str) -> bool:
        """Pair, trust, and connect a keyboard, recovering from stale bonds.

        A user-initiated pairing is always treated as fresh: any existing local
        bond is cleared first so a stale/asymmetric link key cannot make BlueZ
        no-op the pair or attempt a key-based reconnect that fails
        authentication. Clearing a bond removes the device object, so the device
        is rediscovered before pairing -- pairing a missing object would fail
        with ``UnknownObject``.

        If the first pair fails authentication, the peer still holds a stale key;
        a well-behaved peer drops it when that failed attempt's link drops, so
        the bond is cleared and a single retry is made after a short delay.
        Exactly one retry; a second failure returns False so the UI does not
        loop.
        """
        if not self._validate_mac_address(address):
            raise ValueError(f"Invalid MAC address format: {address}")

        # Clearing a stale local bond destroys the Device1 object, so it must be
        # rediscovered. A device with no local bond keeps the object the caller
        # just discovered, so it can be paired directly.
        if self._is_paired(address):
            self._remove_device(address)
            if not self._prepare_fresh_device(address):
                return False
        elif not self._device_exists(address):
            if not self._ensure_device_present(address, timeout=12):
                log.warning(f"[BluezPairing] {address} not present to pair")
                return False

        status = self._pair_trust_connect(address)
        if status == "ok":
            return True
        if status != "auth_failed":
            return False

        log.info(
            f"[BluezPairing] Pair authentication failed for {address}; clearing "
            "and retrying once after peer self-heals"
        )
        self._remove_device(address)
        time.sleep(_PEER_SELF_HEAL_DELAY_SECONDS)
        if not self._prepare_fresh_device(address):
            return False
        return self._pair_trust_connect(address) == "ok"

    def _prepare_fresh_device(self, address: str) -> bool:
        """Rediscover a device after its object was removed for a fresh pairing.

        Returns False (with a warning) when the device cannot be rediscovered, so
        the caller never pairs a missing object (which raises ``UnknownObject``).
        """
        if self._ensure_device_present(address, timeout=12):
            return True
        log.warning(f"[BluezPairing] {address} not rediscovered after bond clear")
        return False

    # ------------------------------------------------------------------
    # Paired-device management (list / connect / disconnect / forget)
    # ------------------------------------------------------------------
    def list_paired_devices(self) -> List[Dict[str, object]]:
        """List paired devices belonging to this adapter for the management UI.

        Each entry is ``{"address", "name", "connected"}``. Reads the cached
        BlueZ object tree directly -- paired devices persist there, so no inquiry
        is needed. Filters to ``Device1`` objects under this adapter whose
        ``Paired`` flag is set, resolves a display name (Name, then Alias, then
        the address for a nameless device), and sorts by name so the row order is
        stable between polls (the raw ObjectManager order is volatile and would
        shift rows under the user's selection).
        """
        prefix = self._adapter_path + "/dev_"
        devices: List[Dict[str, object]] = []
        for path, interfaces in self._managed_objects().items():
            if not path.startswith(prefix):
                continue
            props = interfaces.get(DEVICE_IFACE)
            if not props or not props.get("Paired"):
                continue
            address = str(props.get("Address", ""))
            if not address:
                continue
            name = str(props.get("Name") or props.get("Alias") or address)
            devices.append({
                "address": address,
                "name": name,
                "connected": bool(props.get("Connected", False)),
            })
        devices.sort(key=lambda d: (str(d["name"]).lower(), str(d["address"])))
        return devices

    def connect_device(self, address: str, timeout_seconds: float = 20.0) -> bool:
        """Connect an already-paired device. Returns success for the UI to toast."""
        if not self._validate_mac_address(address):
            raise ValueError(f"Invalid MAC address format: {address}")
        return self.connect_device_status(address, timeout_seconds) == CONNECT_OK

    def connect_device_status(self, address: str,
                              timeout_seconds: float = 20.0) -> str:
        """Connect a paired device and return ok/auth_failed/failed.

        UI callers use ``auth_failed`` to offer a stale-pairing removal prompt.
        Other failures stay generic because they do not prove the saved bond is
        bad.
        """
        if not self._validate_mac_address(address):
            raise ValueError(f"Invalid MAC address format: {address}")
        return self._connect_status(address, timeout_seconds)

    def disconnect_device(self, address: str, timeout_seconds: float = 20.0) -> bool:
        """Disconnect a connected device. Returns success for the UI to toast."""
        if not self._validate_mac_address(address):
            raise ValueError(f"Invalid MAC address format: {address}")
        return self._do_disconnect(address, timeout_seconds)

    def forget_device(self, address: str) -> bool:
        """Remove a device's bond and BlueZ object ('forget'). Returns success.

        After this the device no longer appears in :meth:`list_paired_devices`
        and must be paired again to reconnect.
        """
        if not self._validate_mac_address(address):
            raise ValueError(f"Invalid MAC address format: {address}")
        return self._remove_device(address)
