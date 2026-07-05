# BLE Manager
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""BLE Manager for GATT services and BLE communication.

This module provides BLE (Bluetooth Low Energy) communication via D-Bus/BlueZ.
It implements GATT services for Millennium, Pegasus (Nordic UART), and Chessnut
protocols, allowing chess apps to connect and communicate with the board.

The BleManager uses callbacks to notify the owner (ProtocolManager) of:
- Client connections/disconnections
- Received data
- Connection state changes

Usage:
    def on_data(data, client_type):
        # Process received bytes
        pass
    
    def on_connected(client_type):
        # Handle client connection
        pass
    
    def on_disconnected():
        # Handle client disconnection
        pass
    
    manager = BleManager(
        device_name="DGT PEGASUS",
        on_data_received=on_data,
        on_connected=on_connected,
        on_disconnected=on_disconnected
    )
    manager.start()
"""

import os
import signal
import subprocess  # nosec B404 - used only for fixed btmgmt management commands, never shell/untrusted input
import threading
import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib
from typing import Optional, Callable

from universalchess.board.logging import log
from universalchess.paths import BT_ADMIN

# ============================================================================
# BlueZ D-Bus Constants
# ============================================================================

BLUEZ_SERVICE_NAME = 'org.bluez'
GATT_MANAGER_IFACE = 'org.bluez.GattManager1'
LE_ADVERTISING_MANAGER_IFACE = 'org.bluez.LEAdvertisingManager1'
DBUS_OM_IFACE = 'org.freedesktop.DBus.ObjectManager'
DBUS_PROP_IFACE = 'org.freedesktop.DBus.Properties'
GATT_SERVICE_IFACE = 'org.bluez.GattService1'
GATT_CHRC_IFACE = 'org.bluez.GattCharacteristic1'
LE_ADVERTISEMENT_IFACE = 'org.bluez.LEAdvertisement1'
AGENT_IFACE = 'org.bluez.Agent1'
AGENT_MANAGER_IFACE = 'org.bluez.AgentManager1'

# ============================================================================
# BLE UUID Definitions
# ============================================================================

# Device Information Service UUIDs (standard BLE - 0x180A)
DEVICE_INFO_SERVICE_UUID = "0000180a-0000-1000-8000-00805f9b34fb"
MANUFACTURER_NAME_UUID = "00002a29-0000-1000-8000-00805f9b34fb"
MODEL_NUMBER_UUID = "00002a24-0000-1000-8000-00805f9b34fb"
SERIAL_NUMBER_UUID = "00002a25-0000-1000-8000-00805f9b34fb"
HARDWARE_REV_UUID = "00002a27-0000-1000-8000-00805f9b34fb"
FIRMWARE_REV_UUID = "00002a26-0000-1000-8000-00805f9b34fb"
SOFTWARE_REV_UUID = "00002a28-0000-1000-8000-00805f9b34fb"
SYSTEM_ID_UUID = "00002a23-0000-1000-8000-00805f9b34fb"
IEEE_REGULATORY_UUID = "00002a2a-0000-1000-8000-00805f9b34fb"
PNP_ID_UUID = "00002a50-0000-1000-8000-00805f9b34fb"

# Millennium ChessLink BLE UUIDs
MILLENNIUM_UUIDS = {
    "service": "49535343-fe7d-4ae5-8fa9-9fafd205e455",
    "config": "49535343-6daa-4d02-abf6-19569aca69fe",
    "notify1": "49535343-aca3-481c-91ec-d85e28a60318",
    "tx": "49535343-1e4d-4bd9-ba61-23c647249616",
    "rx": "49535343-8841-43f4-a8d4-ecbe34729bb3",
    "notify2": "49535343-026e-3a9b-954c-97daef17e26e",
}

# Nordic UART Service BLE UUIDs (used by Pegasus)
NORDIC_UUIDS = {
    "service": "6e400001-b5a3-f393-e0a9-e50e24dcca9e",
    "rx": "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
    "tx": "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
}

# Chessnut Air BLE UUIDs
CHESSNUT_UUIDS = {
    "fen_service": "1b7e8261-2877-41c3-b46e-cf057c562023",
    "fen_rx": "1b7e8262-2877-41c3-b46e-cf057c562023",
    "op_service": "1b7e8271-2877-41c3-b46e-cf057c562023",
    "op_tx": "1b7e8272-2877-41c3-b46e-cf057c562023",
    "op_rx": "1b7e8273-2877-41c3-b46e-cf057c562023",
    "unk_service": "1b7e8281-2877-41c3-b46e-cf057c562023",
    "unk_tx": "1b7e8282-2877-41c3-b46e-cf057c562023",
    "unk_rx": "1b7e8283-2877-41c3-b46e-cf057c562023",
    "ota_service": "9e5d1e47-5c13-43a0-8635-82ad38a1386f",
    "ota_char1": "e3dd50bf-f7a7-4e99-838e-570a086c666b",
    "ota_char2": "92e86c7a-d961-4091-b74f-2409e72efe36",
    "ota_char3": "347f7608-2e2d-47eb-913b-75d4edc4de3b",
}

# Chessnut manufacturer data for advertisement
CHESSNUT_MANUFACTURER_ID = 0x4450
CHESSNUT_MANUFACTURER_DATA = bytes.fromhex("4353b953056400003e9751101b00")


# ============================================================================
# BleManager Class
# ============================================================================

class BleManager:
    """Manager for BLE GATT services and communication.
    
    Handles D-Bus/BlueZ setup, GATT service registration, and BLE communication
    for Millennium, Pegasus, and Chessnut protocols.
    
    Attributes:
        connected: Whether a BLE client is connected
        client_type: Type of connected client ('millennium', 'pegasus', 'chessnut', or None)
    """
    
    # Client type constants
    CLIENT_MILLENNIUM = 'millennium'
    CLIENT_PEGASUS = 'pegasus'
    CLIENT_CHESSNUT = 'chessnut'
    
    def __init__(self, device_name: str = "DGT PEGASUS",
                 on_data_received: Callable[[bytes, str], None] = None,
                 on_connected: Callable[[str], None] = None,
                 on_disconnected: Callable[[], None] = None,
                 relay_mode: bool = False,
                 on_relay_data: Callable[[bytes], None] = None,
                 on_display_passkey: Callable[[Optional[str]], None] = None,
                 on_confirm_pairing: Callable[[Optional[str]], bool] = None,
                 status_state=None,
                 adapter_alias: Optional[str] = None):
        """Initialize the BLE manager.
        
        Args:
            device_name: Bluetooth device name to advertise
            adapter_alias: Friendly adapter Alias (BlueZ ``Adapter1.Alias``) to
                set for the board. This is the branded name a phone shows for the
                board and is independent of the per-advertisement ``LocalName``
                values apps discover by. When None, falls back to ``device_name``.
            on_data_received: Callback(data: bytes, client_type: str) for received data
            on_connected: Callback(client_type: str) when client connects
            on_disconnected: Callback() when client disconnects
            relay_mode: If True, forward received data via on_relay_data
            on_relay_data: Callback(data: bytes) for relay mode data forwarding
            status_state: Live :class:`BluetoothStatusState` fed with advertising
                results, client connect/disconnect (with the active emulator),
                adapter power, and OS-level device changes so the board menu and
                web card show an always-current status. Defaults to the
                process-wide singleton; injectable for tests.
            on_display_passkey: Callback(passkey: Optional[str]) invoked when the
                pairing agent must display a passkey (e.g. for a Bluetooth
                keyboard). Called with the 6-digit string to show, or None to
                clear the display when pairing finishes or is cancelled.
            on_confirm_pairing: Callback(passkey: Optional[str]) -> bool invoked
                when a phone/app pairs to the board (numeric comparison or
                just-works). Must show the code on the board and block until the
                user accepts (return True) or declines (return False). When None,
                such pairings are rejected so no device can pair unprompted.
        """
        self.device_name = device_name
        # Friendly adapter Alias; None means "use device_name" (see __init__ doc).
        self._adapter_alias = adapter_alias or device_name
        self._on_data_received = on_data_received
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._relay_mode = relay_mode
        self._on_relay_data = on_relay_data
        self._on_display_passkey = on_display_passkey
        self._on_confirm_pairing = on_confirm_pairing
        # Set while the board drives a user-initiated keyboard pairing. The
        # pairing agent reads it to auto-accept that pairing's numeric-comparison
        # /just-works confirmation (the user picked the keyboard and types the
        # passkey on it) instead of raising a Pair/Reject modal to race.
        self._keyboard_pairing_active = threading.Event()

        # Live Bluetooth status engine (advertising + link + devices). The board
        # menu reads it in-process and it broadcasts every change to the web.
        if status_state is None:
            from universalchess.managers.bluetooth_status_state import (
                get_bluetooth_status_state,
            )
            status_state = get_bluetooth_status_state()
        self._status_state = status_state
        
        # Connection state
        self.connected = False
        self.client_type = None
        
        # D-Bus objects
        self._bus = None
        self._mainloop = None
        self._adapter = None
        self._app = None
        self._agent = None
        self._advertisements = []

        # Characteristic instances for sending notifications
        self._millennium_tx = None
        self._nordic_tx = None
        self._chessnut_fen = None
        self._chessnut_op_rx = None
        
        # Shutdown flag
        self._stopping = False

        # Self-heal progress poll. The bluez self-heal runs as a separate root
        # process; the board learns it is active only by polling the progress
        # file it writes. While a heal runs (a several-minute on-board rebuild),
        # stock BlueZ rejects the adverts, so this lets the status show
        # "repairing advertising" instead of a bare failure. Stopped on shutdown.
        self._heal_monitor_stop = threading.Event()
        self._heal_monitor_thread = None
        self._heal_poll_interval_seconds = 5.0
    
    def _notify_connected(self, client_type: str):
        """Notify that a client connected."""
        # Reset other protocol states
        if client_type == self.CLIENT_MILLENNIUM:
            if self._nordic_tx:
                self._nordic_tx.notifying = False
        elif client_type == self.CLIENT_PEGASUS:
            if self._millennium_tx:
                self._millennium_tx.notifying = False
        elif client_type == self.CLIENT_CHESSNUT:
            if self._millennium_tx:
                self._millennium_tx.notifying = False
            if self._nordic_tx:
                self._nordic_tx.notifying = False
        
        self.connected = True
        self.client_type = client_type
        
        log.info(f"[BleManager] Client connected: {client_type}")

        # A connected BLE central pauses LE advertising; record the active
        # emulator so the board/web show "paused_connected" with what is in play
        # rather than a false advertising failure.
        from universalchess.managers.bluetooth_status_state import TRANSPORT_BLE
        self._status_state.client_connected(TRANSPORT_BLE, emulator=client_type)

        if self._on_connected:
            self._on_connected(client_type)
    
    def _notify_disconnected(self):
        """Notify that a client disconnected."""
        self.connected = False
        self.client_type = None
        
        log.info("[BleManager] Client disconnected")

        self._status_state.client_disconnected()

        if self._on_disconnected:
            self._on_disconnected()
    
    def _notify_data_received(self, data: bytes, client_type: str):
        """Notify that data was received."""
        if self._on_data_received:
            self._on_data_received(data, client_type)
        
        # Forward to relay if enabled
        if self._relay_mode and self._on_relay_data:
            self._on_relay_data(data)
    
    def send_notification(self, data: bytes):
        """Send data to the connected BLE client.
        
        Routes to the appropriate characteristic based on client_type.
        
        Args:
            data: Data bytes to send
        """
        if not self.connected:
            log.debug("[BleManager] send_notification: Not connected, skipping")
            return
        
        if self.client_type == self.CLIENT_MILLENNIUM:
            if self._millennium_tx and self._millennium_tx.notifying:
                self._millennium_tx.send_notification(data)
        
        elif self.client_type == self.CLIENT_PEGASUS:
            if self._nordic_tx and self._nordic_tx.notifying:
                self._nordic_tx.send_notification(data)
        
        elif self.client_type == self.CLIENT_CHESSNUT:
            # Route based on data type
            if len(data) > 0 and data[0] == 0x01:
                # FEN notification
                if self._chessnut_fen and self._chessnut_fen.notifying:
                    self._chessnut_fen.send_notification(data)
            else:
                # Other responses (battery, etc.)
                if self._chessnut_op_rx and self._chessnut_op_rx.notifying:
                    self._chessnut_op_rx.send_notification(data)
    
    def find_adapter(self):
        """Find the first Bluetooth adapter."""
        remote_om = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE_NAME, '/'),
            DBUS_OM_IFACE
        )
        objects = remote_om.GetManagedObjects()
        for o, props in objects.items():
            if GATT_MANAGER_IFACE in props:
                return o
        return None
    
    @staticmethod
    def _run_bluetooth_management_command(cmd, timeout_seconds: float = 5.0):
        """Run a btmgmt command and kill its whole process group on timeout.

        ``sudo btmgmt`` can leave the child ``btmgmt`` process running after
        ``subprocess.run(..., timeout=...)`` times out. Starting a new session
        lets timeout cleanup kill both sudo and btmgmt so BLE startup cannot
        leave management commands stuck behind the service.
        """
        process = subprocess.Popen(  # noqa: S603 # nosec B603 - cmd is a fixed btmgmt argv list, no shell, no untrusted input
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            return subprocess.CompletedProcess(
                cmd, process.returncode, stdout=stdout, stderr=stderr)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:  # noqa: S110 # nosec B110 - process already gone; nothing to clean up
                pass
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(
                cmd, timeout_seconds, output=stdout, stderr=stderr)

    def configure_adapter_security(self):
        """Configure the controller for connectable LE advertising.

        Runs the pinned ``bt-admin`` helper (``configure``), granted passwordless
        sudo by the package postinst, which sets ``bondable off`` / ``le on`` /
        ``connectable on`` via btmgmt. Without this the controller is not
        configured for LE advertising and phone chess apps cannot discover the
        board. Centralizing the three privileged btmgmt calls in one helper means
        the service needs a single NOPASSWD grant; if that grant is missing,
        ``sudo -n`` fails immediately with "a password is required" and this logs
        the failure (which then surfaces as the advertising failed-state) instead
        of hanging on a password prompt. The helper bounds each btmgmt call at 5s,
        so allow the whole sequence up to 20s before the outer guard fires.
        """
        cmd = ['sudo', '-n', BT_ADMIN, 'configure']
        try:
            result = self._run_bluetooth_management_command(cmd, timeout_seconds=20.0)
            if result.returncode == 0:
                log.info("[BleManager] Adapter configured for LE advertising")
            else:
                detail = (result.stderr or result.stdout or "").strip() or "unknown error"
                log.warning(f"[BleManager] bt-admin configure failed: {detail}")
        except FileNotFoundError:
            log.warning("[BleManager] bt-admin helper not found - skipping adapter configuration")
        except subprocess.TimeoutExpired:
            log.warning("[BleManager] bt-admin configure timed out")
        except Exception as e:
            log.warning(f"[BleManager] bt-admin configure error: {e}")
    
    def start(self, mainloop: GLib.MainLoop = None):
        """Start the BLE manager.
        
        Sets up D-Bus, registers GATT services, and starts advertising.
        
        Args:
            mainloop: Optional GLib mainloop. If not provided, creates one.
        """
        log.info("[BleManager] Starting...")

        # Record whether this board runs a patched (non-stock) bluetoothd so the
        # web card and device menu can warn about the deviation. Read once at
        # bring-up from the self-heal marker (written at package install, not per
        # boot); read_status never raises, so a missing/bad marker is harmless.
        from universalchess.managers.bluez_patch_status import read_status
        self._status_state.set_stack_status(read_status())

        # Begin mirroring bluez self-heal progress into the status so the UI
        # shows "repairing advertising" (not a bare failure) during a heal.
        self._start_heal_monitor()

        try:
            # Configure adapter security
            log.info("[BleManager] Configuring adapter security...")
            self.configure_adapter_security()
            log.info("[BleManager] Adapter security configured")
        except Exception as e:
            log.error(f"[BleManager] Failed to configure adapter security: {e}", exc_info=True)
            return False
        
        try:
            # Initialize D-Bus
            log.info("[BleManager] Initializing D-Bus...")
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            self._bus = dbus.SystemBus()
            log.info("[BleManager] D-Bus initialized")
        except Exception as e:
            log.error(f"[BleManager] Failed to initialize D-Bus: {e}", exc_info=True)
            return False
        
        if mainloop:
            self._mainloop = mainloop
        else:
            self._mainloop = GLib.MainLoop()
        
        try:
            # Find adapter
            log.info("[BleManager] Finding Bluetooth adapter...")
            self._adapter = self.find_adapter()
            if not self._adapter:
                log.error("[BleManager] No Bluetooth adapter found")
                return False
            log.info(f"[BleManager] Found adapter: {self._adapter}")
        except Exception as e:
            log.error(f"[BleManager] Failed to find adapter: {e}", exc_info=True)
            return False
        
        try:
            # Configure adapter properties
            log.info("[BleManager] Configuring adapter properties...")
            adapter_props = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE_NAME, self._adapter),
                DBUS_PROP_IFACE
            )
            
            try:
                adapter_props.Set("org.bluez.Adapter1", "Alias", dbus.String(self._adapter_alias))
                log.info(f"[BleManager] Adapter Alias set to '{self._adapter_alias}'")
            except dbus.exceptions.DBusException as e:
                log.warning(f"[BleManager] Could not set Alias: {e}")
            
            try:
                powered = adapter_props.Get("org.bluez.Adapter1", "Powered")
                if not powered:
                    adapter_props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
                    log.info("[BleManager] Adapter powered on")
                    powered = True
                else:
                    log.info("[BleManager] Adapter already powered on")
                self._status_state.set_powered(bool(powered))
            except dbus.exceptions.DBusException as e:
                log.warning(f"[BleManager] Could not check/set Powered: {e}")
        except Exception as e:
            log.error(f"[BleManager] Failed to configure adapter properties: {e}", exc_info=True)
            return False
        
        try:
            # Register agent
            log.info("[BleManager] Registering agent...")
            self._register_agent()
            log.info("[BleManager] Agent registered")
        except Exception as e:
            log.error(f"[BleManager] Failed to register agent: {e}", exc_info=True)
            return False
        
        try:
            # Create and register GATT application
            log.info("[BleManager] Creating GATT application...")
            self._create_gatt_application()
            log.info("[BleManager] GATT application created")
        except Exception as e:
            log.error(f"[BleManager] Failed to create GATT application: {e}", exc_info=True)
            return False
        
        try:
            # Register advertisements
            log.info("[BleManager] Registering advertisements...")
            self._register_advertisements()
            log.info("[BleManager] Advertisements registered")
        except Exception as e:
            log.error(f"[BleManager] Failed to register advertisements: {e}", exc_info=True)
            return False

        # Track live, externally-driven Bluetooth changes (console btmgmt/rfkill,
        # adapter power, device connect/disconnect). Best-effort: a missing
        # signal subscription must not fail BLE bring-up.
        try:
            self._register_status_signal_receivers()
        except Exception as e:  # noqa: BLE001
            log.warning(f"[BleManager] Could not register status signal receivers: {e}")

        log.info("[BleManager] Started successfully")
        return True

    def _register_status_signal_receivers(self):
        """Subscribe to BlueZ signals that change the live Bluetooth status.

        Feeds :class:`BluetoothStatusState` from changes that originate outside
        our own calls so the board/web stay accurate:

        * ``Adapter1.Powered`` / ``LEAdvertisingManager1.ActiveInstances`` via
          ``PropertiesChanged`` -- catches power-off, ``rfkill``, and adverts
          pausing/resuming (including console ``btmgmt``/``bluetoothctl``).
        * Device ``Connected`` via ``PropertiesChanged`` and object lifecycle via
          ``InterfacesAdded``/``InterfacesRemoved`` -- catches phones/keyboards
          connecting and disconnecting at the OS level.

        Runs on the existing GLib mainloop (the bus is a ``SystemBus`` driven by
        ``DBusGMainLoop``), so callbacks fire on the BLE thread.
        """
        def _on_properties_changed(interface, changed, invalidated, path=None):
            if interface == "org.bluez.Adapter1" and "Powered" in changed:
                self._status_state.set_powered(bool(changed["Powered"]))
            elif interface == LE_ADVERTISING_MANAGER_IFACE and "ActiveInstances" in changed:
                self._status_state.set_active_instances(int(changed["ActiveInstances"]))
            elif interface == "org.bluez.Device1" and "Connected" in changed:
                if bool(changed["Connected"]):
                    self._status_state.device_connected(
                        self._device_address_from_path(path),
                        self._device_name_safe(path),
                    )
                else:
                    self._status_state.device_disconnected(
                        self._device_address_from_path(path))

        def _on_interfaces_added(path, interfaces):
            if "org.bluez.Device1" in interfaces:
                props = interfaces["org.bluez.Device1"]
                if props.get("Connected"):
                    self._status_state.device_connected(
                        self._device_address_from_path(path),
                        str(props.get("Name") or props.get("Alias") or ""),
                    )

        def _on_interfaces_removed(path, interfaces):
            if "org.bluez.Device1" in interfaces:
                self._status_state.device_disconnected(
                    self._device_address_from_path(path))

        self._bus.add_signal_receiver(
            _on_properties_changed,
            dbus_interface=DBUS_PROP_IFACE,
            signal_name="PropertiesChanged",
            path_keyword="path",
        )
        self._bus.add_signal_receiver(
            _on_interfaces_added,
            dbus_interface=DBUS_OM_IFACE,
            signal_name="InterfacesAdded",
        )
        self._bus.add_signal_receiver(
            _on_interfaces_removed,
            dbus_interface=DBUS_OM_IFACE,
            signal_name="InterfacesRemoved",
        )
        log.info("[BleManager] Status signal receivers registered")

    @staticmethod
    def _device_address_from_path(path: Optional[str]) -> str:
        """Derive a device MAC from its BlueZ object path.

        ``/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF`` -> ``AA:BB:CC:DD:EE:FF``. Used
        as the stable key for the connected-device set; returns ``""`` for a
        non-device path so a stray signal is ignored.
        """
        if not path or "/dev_" not in path:
            return ""
        return path.rsplit("/dev_", 1)[1].replace("_", ":")

    def _device_name_safe(self, path: Optional[str]) -> str:
        """Best-effort friendly name for a connected device path.

        Falls back to the address when the name cannot be read (e.g. the device
        object is gone), so the device list always has a label.
        """
        try:
            props = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE_NAME, path), DBUS_PROP_IFACE)
            name = props.Get("org.bluez.Device1", "Name")
            return str(name)
        except Exception:  # noqa: BLE001
            return self._device_address_from_path(path)

    def start_async(self, mainloop: GLib.MainLoop = None) -> threading.Thread:
        """Run start() and the GLib mainloop on a background daemon thread.

        start() blocks for ~15s in configure_adapter_security() -- three
        `btmgmt` calls that stall while bluetoothd owns the management socket --
        before the (fast) D-Bus/agent/GATT/advertisement registration. Calling
        start() inline on the application startup path froze the splash for that
        whole period. This method performs the IDENTICAL work (same start() call
        with the same mainloop, then mainloop.run()) but on a dedicated thread,
        returning immediately so the caller can continue bringing up the UI.

        Behavior notes:
        - Adapter state, pairing, advertising and GATT registration are
          unchanged; only the thread on which the bring-up runs differs.
        - Running start() and mainloop.run() on the SAME thread is also more
          correct than the previous split (bus created on the main thread,
          mainloop run on another): the D-Bus async reply handlers now fire on
          the thread that owns the bus and mainloop.
        - If start() returns False or raises, the mainloop is NOT run and the
          error is logged. Unlike the previous inline path (which aborted the
          whole process on failure), the application keeps running with BLE
          unavailable, so a Bluetooth fault no longer prevents the device from
          reaching the menu.

        Args:
            mainloop: GLib mainloop to run after a successful start(). If None,
                start() creates one; either way the loop stored on the manager
                (self._mainloop) is the one run.

        Returns:
            The started daemon thread (exposed for diagnostics and tests).
        """
        def _bring_up() -> None:
            log.info("[BleManager] Async setup thread starting...")
            try:
                if not self.start(mainloop):
                    log.error("[BleManager] start() failed - BLE will be unavailable")
                    return
                run_loop = self._mainloop
                if run_loop is None:
                    log.error("[BleManager] No mainloop after start() - BLE will be unavailable")
                    return
                run_loop.run()
                log.info("[BleManager] Mainloop exited normally")
            except Exception as e:
                log.error(f"[BleManager] Error in async setup/mainloop: {e}", exc_info=True)

        thread = threading.Thread(target=_bring_up, name="BleSetup", daemon=True)
        thread.start()
        return thread

    def _start_heal_monitor(self):
        """Start the background poll that mirrors self-heal progress into status.

        The bluez self-heal is a separate root process, so the board learns it is
        running only by reading the progress file it writes (see
        ``managers/bluez_patch_status.read_progress``). Polls every few seconds
        and feeds the status engine, which dedupes -- an unchanged poll does not
        broadcast, so the live web view never churns. Idempotent; daemon thread;
        exits promptly on :meth:`stop`.
        """
        if self._heal_monitor_thread and self._heal_monitor_thread.is_alive():
            return
        from universalchess.managers.bluez_patch_status import read_progress

        def _loop():
            while not self._heal_monitor_stop.is_set():
                progress = read_progress()
                self._status_state.set_heal_status(
                    progress.get("running", False), progress.get("phase")
                )
                # wait() returns immediately when stop() sets the event.
                self._heal_monitor_stop.wait(self._heal_poll_interval_seconds)

        self._heal_monitor_stop.clear()
        self._heal_monitor_thread = threading.Thread(
            target=_loop, name="BleHealMonitor", daemon=True
        )
        self._heal_monitor_thread.start()

    def stop(self):
        """Stop the BLE manager."""
        log.info("[BleManager] Stopping...")
        self._stopping = True
        # Stop the self-heal progress poll so it does not outlive the manager.
        self._heal_monitor_stop.set()
        
        # Quit mainloop FIRST to stop processing events
        log.info("[BleManager] Quitting mainloop...")
        if self._mainloop:
            try:
                self._mainloop.quit()
                log.info("[BleManager] Mainloop quit requested")
            except Exception as e:
                log.error(f"[BleManager] Error quitting mainloop: {e}")
        else:
            log.info("[BleManager] No mainloop to quit")
        
        # Unregister advertisements (with timeout to avoid blocking)
        log.info("[BleManager] Unregistering advertisements...")
        try:
            if self._adapter:
                le_adv_manager = dbus.Interface(
                    self._bus.get_object(BLUEZ_SERVICE_NAME, self._adapter),
                    LE_ADVERTISING_MANAGER_IFACE
                )
                for i, adv in enumerate(self._advertisements):
                    try:
                        log.info(f"[BleManager] Unregistering advertisement {i+1}/{len(self._advertisements)}...")
                        le_adv_manager.UnregisterAdvertisement(
                            adv.get_path(),
                            timeout=1.0
                        )
                        log.info(f"[BleManager] Advertisement {i+1} unregistered")
                    except Exception as e:
                        log.error(f"[BleManager] Error unregistering advertisement {i+1}: {e}")
            else:
                log.info("[BleManager] No adapter, skipping advertisement unregister")
        except Exception as e:
            log.error(f"[BleManager] Error unregistering advertisements: {e}", exc_info=True)
        
        log.info("[BleManager] Stopped")
    
    def begin_keyboard_pairing(self):
        """Mark a user-initiated keyboard pairing as active.

        While active, the pairing agent auto-accepts the keyboard's
        numeric-comparison/just-works confirmation rather than prompting on the
        board. Call :meth:`end_keyboard_pairing` (in a finally) once the pairing
        attempt completes so incoming pairings regain the confirmation gate.
        """
        self._keyboard_pairing_active.set()

    def end_keyboard_pairing(self):
        """Clear the user-initiated keyboard pairing flag (see begin)."""
        self._keyboard_pairing_active.clear()

    def _register_agent(self):
        """Register the pairing agent (KeyboardDisplay capability).

        KeyboardDisplay is required so BlueZ will ask us to *display* a passkey
        when a Bluetooth keyboard pairs (the user then types it on the keyboard).
        Chess-app/phone pairings (numeric comparison / just-works) are gated by
        ``on_confirm_pairing``: the agent shows the code on the board and only
        completes the bond after the user accepts on the board. A board-initiated
        keyboard pairing (``_keyboard_pairing_active``) bypasses that prompt and
        auto-accepts, since the user already chose the keyboard.
        """
        self._agent = _PairingAgent(
            self._bus,
            on_display_passkey=self._on_display_passkey,
            on_confirm_pairing=self._on_confirm_pairing,
            is_keyboard_pairing_active=self._keyboard_pairing_active.is_set,
        )
        
        agent_manager = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE_NAME, '/org/bluez'),
            AGENT_MANAGER_IFACE
        )
        
        try:
            agent_manager.RegisterAgent(self._agent.AGENT_PATH, self._agent.CAPABILITY)
            agent_manager.RequestDefaultAgent(self._agent.AGENT_PATH)
            log.info("[BleManager] Agent registered")
        except dbus.exceptions.DBusException as e:
            log.warning(f"[BleManager] Could not register agent: {e}")
    
    def _create_gatt_application(self):
        """Create and register the GATT application with all services."""
        self._app = _Application(self._bus)
        
        # Create services (pass self for callbacks)
        service_index = 0
        
        # Device Information Service
        self._app.add_service(_DeviceInfoService(self._bus, service_index))
        service_index += 1
        
        # Millennium Service
        millennium_service = _MillenniumService(self._bus, service_index, self)
        self._app.add_service(millennium_service)
        self._millennium_tx = millennium_service.tx_char
        service_index += 1
        
        # Nordic UART Service (Pegasus)
        nordic_service = _NordicUARTService(self._bus, service_index, self)
        self._app.add_service(nordic_service)
        self._nordic_tx = nordic_service.tx_char
        service_index += 1
        
        # Chessnut Services
        fen_service = _ChessnutFENService(self._bus, service_index, self)
        self._app.add_service(fen_service)
        self._chessnut_fen = fen_service.fen_char
        service_index += 1
        
        op_service = _ChessnutOperationService(self._bus, service_index, self)
        self._app.add_service(op_service)
        self._chessnut_op_rx = op_service.op_rx_char
        service_index += 1
        
        self._app.add_service(_ChessnutUnknownService(self._bus, service_index))
        service_index += 1
        
        self._app.add_service(_ChessnutOTAService(self._bus, service_index))
        
        # Register with BlueZ
        gatt_manager = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE_NAME, self._adapter),
            GATT_MANAGER_IFACE
        )
        
        gatt_manager.RegisterApplication(
            self._app.get_path(), {},
            reply_handler=lambda: log.info("[BleManager] GATT application registered"),
            error_handler=lambda e: log.error(f"[BleManager] Failed to register GATT application: {e}")
        )
    
    def get_advertisement_status(self) -> dict:
        """Return the current BLE advertisement registration status.

        Reads the live status engine so the board Bluetooth menu (in-process)
        and the web Bluetooth card (over the broadcast/SSE channel) report the
        same advertising sub-block schema.
        """
        return self._status_state.to_dict()["advertising"]

    def _on_advertisement_registered(self, idx: int):
        """D-Bus reply handler: one advertisement was accepted by BlueZ."""
        log.info(f"[BleManager] Advertisement {idx} registered")
        self._status_state.advertisement_registered()

    def _on_advertisement_failed(self, idx: int, error):
        """D-Bus error handler: BlueZ rejected one advertisement.

        Records the error so the board/web can show *why* apps cannot discover
        the board (the common cause is the service user lacking passwordless
        ``btmgmt`` access, leaving the controller un-configured for LE adverts).
        """
        log.error(f"[BleManager] Failed to register advertisement {idx}: {error}")
        self._status_state.advertisement_failed(error)

    def _register_advertisements(self):
        """Register BLE advertisements for all protocols."""
        le_adv_manager = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE_NAME, self._adapter),
            LE_ADVERTISING_MANAGER_IFACE
        )
        
        # Advertisement 1: DGT PEGASUS with Nordic UUID
        adv1 = _Advertisement(
            self._bus, 0, "DGT PEGASUS",
            service_uuids=[NORDIC_UUIDS["service"]]
        )
        self._advertisements.append(adv1)
        
        # Advertisement 2: Chessnut Air with ManufacturerData
        adv2 = _Advertisement(
            self._bus, 1, "Chessnut Air",
            manufacturer_data={CHESSNUT_MANUFACTURER_ID: CHESSNUT_MANUFACTURER_DATA}
        )
        self._advertisements.append(adv2)
        
        # Advertisement 3: MILLENNIUM CHESS
        adv3 = _Advertisement(
            self._bus, 2, "MILLENNIUM CHESS"
        )
        self._advertisements.append(adv3)

        # Reset counters and publish a pending baseline (failed == 0 -> ok) so a
        # reader during the brief async window does not see a stale prior result.
        self._status_state.begin_advertising(
            len(self._advertisements),
            [adv.local_name for adv in self._advertisements],
        )

        # Register all advertisements
        for i, adv in enumerate(self._advertisements, 1):
            le_adv_manager.RegisterAdvertisement(
                adv.get_path(), {},
                reply_handler=lambda idx=i: self._on_advertisement_registered(idx),
                error_handler=lambda e, idx=i: self._on_advertisement_failed(idx, e)
            )


# ============================================================================
# Internal D-Bus Classes
# ============================================================================

class _Rejected(dbus.DBusException):
    """Raised/returned to BlueZ to refuse a pairing the user did not approve."""
    _dbus_error_name = "org.bluez.Error.Rejected"


class _PairingAgent(dbus.service.Object):
    """Bluetooth pairing agent for the board.

    Uses ``KeyboardDisplay`` capability so BlueZ will request a passkey to be
    *displayed* when a Bluetooth keyboard pairs; the user types the displayed
    passkey on the keyboard to complete pairing.

    Phone/app pairings that use numeric comparison (``RequestConfirmation``) or
    just-works (``RequestAuthorization``) are gated by ``on_confirm_pairing``:
    the board shows the code (if any) and the user must accept on the board
    before the bond completes. These two methods reply asynchronously so the
    GLib main loop stays responsive while the user decides; a refusal or any
    failure returns ``org.bluez.Error.Rejected`` so an unknown device can never
    pair unprompted.

    The displayed keyboard passkey is forwarded to ``on_display_passkey(text)``
    and cleared via ``on_display_passkey(None)`` when pairing ends or is
    cancelled.
    """

    AGENT_PATH = "/org/bluez/universal_agent"
    CAPABILITY = "KeyboardDisplay"

    def __init__(self, bus, on_display_passkey=None, on_confirm_pairing=None,
                 is_keyboard_pairing_active=None):
        self.bus = bus
        self._on_display_passkey = on_display_passkey
        self._on_confirm_pairing = on_confirm_pairing
        # Predicate returning True while the board is driving a keyboard pairing
        # the user explicitly started; such pairings auto-accept (see
        # _confirm_async). Defaults to "never" so incoming pairings always
        # require the on-board confirmation gate.
        self._is_keyboard_pairing_active = (
            is_keyboard_pairing_active or (lambda: False))
        dbus.service.Object.__init__(self, bus, self.AGENT_PATH)

    def _confirm_async(self, passkey, reply, error):
        """Resolve a pairing confirmation off the GLib loop, then reply.

        For an incoming phone/app pairing this runs the on-board Pair/Reject
        prompt, which blocks until the user decides (or a timeout elapses), so it
        must not run on the D-Bus dispatch thread. For a board-initiated keyboard
        pairing it auto-accepts: the user already chose the keyboard and is busy
        typing the passkey on it, so no on-board button is shown.
        ``run_pairing_confirmation`` translates the outcome into the async
        ``reply``/``error`` callbacks.
        """
        from universalchess.menus.pairing_confirm import run_pairing_confirmation

        auto_accept = bool(self._is_keyboard_pairing_active())

        def worker():
            run_pairing_confirmation(
                self._on_confirm_pairing, passkey,
                accept=reply,
                reject=lambda: error(_Rejected("Pairing rejected on board")),
                log=log,
                auto_accept=auto_accept,
            )

        threading.Thread(target=worker, daemon=True,
                         name="bt-pair-confirm").start()

    def _show_passkey(self, passkey) -> None:
        """Forward a passkey to the display callback, formatted for the user."""
        if self._on_display_passkey is None:
            return
        from universalchess.managers.bt_keyboard import format_passkey
        try:
            self._on_display_passkey(format_passkey(int(passkey)))
        except Exception as e:  # noqa: BLE001 - display must not break pairing
            log.error(f"[BleManager] Failed to display passkey: {e}")

    def _clear_passkey(self) -> None:
        """Clear any displayed passkey."""
        if self._on_display_passkey is None:
            return
        try:
            self._on_display_passkey(None)
        except Exception as e:  # noqa: BLE001
            log.error(f"[BleManager] Failed to clear passkey display: {e}")

    @dbus.service.method(AGENT_IFACE, in_signature='', out_signature='')
    def Release(self):
        log.info("[BleManager] Agent released")
        self._clear_passkey()

    @dbus.service.method(AGENT_IFACE, in_signature='os', out_signature='')
    def AuthorizeService(self, device, uuid):
        log.info(f"[BleManager] AuthorizeService: {device} -> {uuid}")

    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='s')
    def RequestPinCode(self, device):
        # Legacy pairing: no PIN required.
        return ""

    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='u')
    def RequestPasskey(self, device):
        # Only reached if the peer expects us to *input* a passkey, which does
        # not apply to a keyboard peer (it inputs, we display). Return 0.
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature='ouq', out_signature='')
    def DisplayPasskey(self, device, passkey, entered):
        log.info(f"[BleManager] DisplayPasskey for {device}: {int(passkey):06d} "
                 f"(entered={entered})")
        self._show_passkey(passkey)

    @dbus.service.method(AGENT_IFACE, in_signature='os', out_signature='')
    def DisplayPinCode(self, device, pincode):
        log.info(f"[BleManager] DisplayPinCode for {device}: {pincode}")
        if self._on_display_passkey is not None:
            try:
                self._on_display_passkey(str(pincode))
            except Exception as e:  # noqa: BLE001
                log.error(f"[BleManager] Failed to display PIN: {e}")

    @dbus.service.method(AGENT_IFACE, in_signature='ou', out_signature='',
                         async_callbacks=('reply', 'error'))
    def RequestConfirmation(self, device, passkey, reply, error):
        # Numeric comparison (e.g. iPhone): show the 6-digit code on the board
        # and require the user to accept before the bond completes.
        from universalchess.managers.bt_keyboard import format_passkey
        code = format_passkey(int(passkey))
        log.info(f"[BleManager] RequestConfirmation for {device}: {code} "
                 f"- awaiting on-board confirmation")
        self._confirm_async(code, reply, error)

    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='',
                         async_callbacks=('reply', 'error'))
    def RequestAuthorization(self, device, reply, error):
        # Just-works pairing (no code): still require explicit acceptance on the
        # board so an unknown device cannot pair unprompted.
        log.info(f"[BleManager] RequestAuthorization for {device} "
                 f"- awaiting on-board confirmation")
        self._confirm_async(None, reply, error)

    @dbus.service.method(AGENT_IFACE, in_signature='', out_signature='')
    def Cancel(self):
        log.info("[BleManager] Pairing cancelled")
        self._clear_passkey()


class _Advertisement(dbus.service.Object):
    """BLE Advertisement."""
    
    PATH_BASE = '/org/bluez/universal/advertisement'
    
    def __init__(self, bus, index, name, service_uuids=None, manufacturer_data=None):
        self.path = self.PATH_BASE + str(index)
        self.bus = bus
        self.ad_type = 'peripheral'
        self.local_name = name
        self.service_uuids = service_uuids or []
        self.manufacturer_data = manufacturer_data
        dbus.service.Object.__init__(self, bus, self.path)
    
    def get_properties(self):
        properties = {
            'Type': self.ad_type,
            'LocalName': dbus.String(self.local_name),
            'IncludeTxPower': dbus.Boolean(True),
        }
        
        if self.service_uuids:
            properties['ServiceUUIDs'] = dbus.Array(self.service_uuids, signature='s')
        
        if self.manufacturer_data:
            mfr_dict = {}
            for company_id, data in self.manufacturer_data.items():
                mfr_dict[dbus.UInt16(company_id)] = dbus.Array([dbus.Byte(b) for b in data], signature='y')
            properties['ManufacturerData'] = dbus.Dictionary(mfr_dict, signature='qv')
        
        return {LE_ADVERTISEMENT_IFACE: properties}
    
    def get_path(self):
        return dbus.ObjectPath(self.path)
    
    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != LE_ADVERTISEMENT_IFACE:
            raise dbus.exceptions.DBusException(
                'org.bluez.Error.InvalidArguments',
                'Invalid interface: ' + interface)
        return self.get_properties()[LE_ADVERTISEMENT_IFACE]
    
    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature='', out_signature='')
    def Release(self):
        log.info(f"[BleManager] Advertisement released: {self.path}")
        # BlueZ dropped this advert (e.g. controller reset); reflect it live so
        # the board/web leave the 'advertising' state without us re-registering.
        try:
            from universalchess.managers.bluetooth_status_state import (
                get_bluetooth_status_state,
            )
            get_bluetooth_status_state().advertisement_released()
        except Exception as e:  # noqa: BLE001 - status reporting must not break BLE
            log.debug(f"[BleManager] Failed to record advert release: {e}")


class _Application(dbus.service.Object):
    """GATT Application container."""
    
    def __init__(self, bus):
        self.path = '/org/bluez/universal'
        self.services = []
        dbus.service.Object.__init__(self, bus, self.path)
    
    def get_path(self):
        return dbus.ObjectPath(self.path)
    
    def add_service(self, service):
        self.services.append(service)
    
    @dbus.service.method(DBUS_OM_IFACE, out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        response = {}
        for service in self.services:
            response[service.get_path()] = service.get_properties()
            chrcs = service.get_characteristics()
            for chrc in chrcs:
                response[chrc.get_path()] = chrc.get_properties()
        return response


class _Service(dbus.service.Object):
    """GATT Service base class."""
    
    PATH_BASE = '/org/bluez/universal/service'
    
    def __init__(self, bus, index, uuid, primary):
        self.path = self.PATH_BASE + str(index)
        self.bus = bus
        self.uuid = uuid
        self.primary = primary
        self.characteristics = []
        dbus.service.Object.__init__(self, bus, self.path)
    
    def get_properties(self):
        return {
            GATT_SERVICE_IFACE: {
                'UUID': self.uuid,
                'Primary': self.primary,
                'Characteristics': dbus.Array(
                    self.get_characteristic_paths(),
                    signature='o')
            }
        }
    
    def get_path(self):
        return dbus.ObjectPath(self.path)
    
    def add_characteristic(self, characteristic):
        self.characteristics.append(characteristic)
    
    def get_characteristic_paths(self):
        return [chrc.get_path() for chrc in self.characteristics]
    
    def get_characteristics(self):
        return self.characteristics


class _Characteristic(dbus.service.Object):
    """GATT Characteristic base class."""
    
    def __init__(self, bus, index, uuid, flags, service):
        self.path = service.path + '/char' + str(index)
        self.bus = bus
        self.uuid = uuid
        self.service = service
        self.flags = flags
        self.notifying = False
        dbus.service.Object.__init__(self, bus, self.path)
    
    def get_properties(self):
        return {
            GATT_CHRC_IFACE: {
                'Service': self.service.get_path(),
                'UUID': self.uuid,
                'Flags': self.flags,
            }
        }
    
    def get_path(self):
        return dbus.ObjectPath(self.path)
    
    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != GATT_CHRC_IFACE:
            raise dbus.exceptions.DBusException(
                'org.bluez.Error.InvalidArguments',
                'Invalid interface: ' + interface)
        return self.get_properties()[GATT_CHRC_IFACE]
    
    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        return []
    
    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        pass
    
    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        self.notifying = True
    
    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        self.notifying = False
    
    @dbus.service.signal(DBUS_PROP_IFACE, signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated):
        pass
    
    def send_notification(self, data):
        """Send notification with data."""
        if not self.notifying:
            return
        value = dbus.Array([dbus.Byte(b) for b in data], signature='y')
        self.PropertiesChanged(GATT_CHRC_IFACE, {'Value': value}, [])


class _ReadOnlyCharacteristic(_Characteristic):
    """Simple read-only characteristic with static value."""
    
    def __init__(self, bus, index, uuid, service, value):
        _Characteristic.__init__(self, bus, index, uuid, ['read'], service)
        if isinstance(value, str):
            self.value = [dbus.Byte(ord(c)) for c in value]
        else:
            self.value = [dbus.Byte(b) for b in value]
    
    def ReadValue(self, options):
        return dbus.Array(self.value, signature='y')


# ============================================================================
# Device Information Service
# ============================================================================

class _DeviceInfoService(_Service):
    """Device Information Service (0x180A)."""
    
    def __init__(self, bus, index):
        _Service.__init__(self, bus, index, DEVICE_INFO_SERVICE_UUID, True)
        
        self.add_characteristic(_ReadOnlyCharacteristic(
            bus, 0, MANUFACTURER_NAME_UUID, self, "MCHP"))
        self.add_characteristic(_ReadOnlyCharacteristic(
            bus, 1, MODEL_NUMBER_UUID, self, "BT5056"))
        self.add_characteristic(_ReadOnlyCharacteristic(
            bus, 2, SERIAL_NUMBER_UUID, self, "3481F4ED7834"))
        self.add_characteristic(_ReadOnlyCharacteristic(
            bus, 3, HARDWARE_REV_UUID, self, "5056_SPP     "))
        self.add_characteristic(_ReadOnlyCharacteristic(
            bus, 4, FIRMWARE_REV_UUID, self, "2220013"))
        self.add_characteristic(_ReadOnlyCharacteristic(
            bus, 5, SOFTWARE_REV_UUID, self, "0000"))
        self.add_characteristic(_ReadOnlyCharacteristic(
            bus, 6, SYSTEM_ID_UUID, self, bytes.fromhex("0000000000000000")))
        self.add_characteristic(_ReadOnlyCharacteristic(
            bus, 7, IEEE_REGULATORY_UUID, self, bytes.fromhex("0001000400000000")))
        self.add_characteristic(_ReadOnlyCharacteristic(
            bus, 8, PNP_ID_UUID, self, bytes([0x01, 0x0D, 0x00, 0x00, 0x00, 0x01, 0x00])))


# ============================================================================
# Millennium Service
# ============================================================================

class _MillenniumTXCharacteristic(_Characteristic):
    """Millennium TX characteristic - sends data to client."""
    
    def __init__(self, bus, index, service, manager: BleManager):
        _Characteristic.__init__(self, bus, index, MILLENNIUM_UUIDS["tx"],
                                 ['read', 'write', 'write-without-response', 'notify'], service)
        self._manager = manager
        self._cached_value = bytearray([0])
    
    def ReadValue(self, options):
        if not self._manager.connected:
            self._manager._notify_connected(BleManager.CLIENT_MILLENNIUM)
        return dbus.Array([dbus.Byte(b) for b in self._cached_value], signature='y')
    
    def WriteValue(self, value, options):
        pass
    
    def StartNotify(self):
        log.info("[BleManager] Millennium TX StartNotify - client subscribing")
        self.notifying = True
        self._manager._notify_connected(BleManager.CLIENT_MILLENNIUM)
    
    def StopNotify(self):
        if not self.notifying:
            return
        log.info("[BleManager] Millennium client disconnected")
        self.notifying = False
        self._manager._notify_disconnected()
    
    def send_notification(self, data):
        if not self.notifying:
            return
        self._cached_value = bytearray(data)
        value = dbus.Array([dbus.Byte(b) for b in data], signature='y')
        self.PropertiesChanged(GATT_CHRC_IFACE, {'Value': value}, [])


class _MillenniumRXCharacteristic(_Characteristic):
    """Millennium RX characteristic - receives data from client."""
    
    def __init__(self, bus, index, service, manager: BleManager):
        _Characteristic.__init__(self, bus, index, MILLENNIUM_UUIDS["rx"],
                                 ['write', 'write-without-response'], service)
        self._manager = manager
    
    def WriteValue(self, value, options):
        try:
            bytes_data = bytes([int(b) for b in value])
            
            if not self._manager.connected:
                self._manager._notify_connected(BleManager.CLIENT_MILLENNIUM)
            
            self._manager._notify_data_received(bytes_data, BleManager.CLIENT_MILLENNIUM)
        except Exception as e:
            log.error(f"[BleManager] Error in Millennium RX: {e}")


class _MillenniumService(_Service):
    """Millennium ChessLink service."""
    
    def __init__(self, bus, index, manager: BleManager):
        _Service.__init__(self, bus, index, MILLENNIUM_UUIDS["service"], True)
        
        # Config characteristic
        self.add_characteristic(_ReadOnlyCharacteristic(
            bus, 0, MILLENNIUM_UUIDS["config"], self, bytes.fromhex("00240024000000F401")))
        
        # Notify1 characteristic
        self.add_characteristic(_Characteristic(
            bus, 1, MILLENNIUM_UUIDS["notify1"], ['write', 'notify'], self))
        
        # TX characteristic (main)
        self.tx_char = _MillenniumTXCharacteristic(bus, 2, self, manager)
        self.add_characteristic(self.tx_char)
        
        # RX characteristic
        self.add_characteristic(_MillenniumRXCharacteristic(bus, 3, self, manager))
        
        # Notify2 characteristic
        self.add_characteristic(_Characteristic(
            bus, 4, MILLENNIUM_UUIDS["notify2"], ['write', 'notify'], self))


# ============================================================================
# Nordic UART Service (Pegasus)
# ============================================================================

class _NordicTXCharacteristic(_Characteristic):
    """Nordic TX characteristic - sends data to Pegasus client."""
    
    def __init__(self, bus, index, service, manager: BleManager):
        _Characteristic.__init__(self, bus, index, NORDIC_UUIDS["tx"], ['notify'], service)
        self._manager = manager
    
    def StartNotify(self):
        log.info("[BleManager] Nordic TX StartNotify - Pegasus client subscribing")
        self.notifying = True
        self._manager._notify_connected(BleManager.CLIENT_PEGASUS)
    
    def StopNotify(self):
        if not self.notifying:
            return
        log.info("[BleManager] Pegasus client disconnected")
        self.notifying = False
        self._manager._notify_disconnected()


class _NordicRXCharacteristic(_Characteristic):
    """Nordic RX characteristic - receives data from Pegasus client."""
    
    def __init__(self, bus, index, service, manager: BleManager):
        _Characteristic.__init__(self, bus, index, NORDIC_UUIDS["rx"],
                                 ['write', 'write-without-response'], service)
        self._manager = manager
    
    def WriteValue(self, value, options):
        try:
            bytes_data = bytes([int(b) for b in value])
            
            if not self._manager.connected:
                self._manager._notify_connected(BleManager.CLIENT_PEGASUS)
            
            self._manager._notify_data_received(bytes_data, BleManager.CLIENT_PEGASUS)
        except Exception as e:
            log.error(f"[BleManager] Error in Nordic RX: {e}")


class _NordicUARTService(_Service):
    """Nordic UART Service for Pegasus."""
    
    def __init__(self, bus, index, manager: BleManager):
        _Service.__init__(self, bus, index, NORDIC_UUIDS["service"], True)
        
        self.tx_char = _NordicTXCharacteristic(bus, 0, self, manager)
        self.add_characteristic(self.tx_char)
        self.add_characteristic(_NordicRXCharacteristic(bus, 1, self, manager))


# ============================================================================
# Chessnut Services
# ============================================================================

class _ChessnutFENCharacteristic(_Characteristic):
    """Chessnut FEN RX Characteristic - sends FEN notifications."""
    
    def __init__(self, bus, index, service, manager: BleManager):
        _Characteristic.__init__(self, bus, index, CHESSNUT_UUIDS["fen_rx"], ['notify'], service)
        self._manager = manager


class _ChessnutOperationTXCharacteristic(_Characteristic):
    """Chessnut Operation TX Characteristic - receives commands."""
    
    def __init__(self, bus, index, service, manager: BleManager):
        _Characteristic.__init__(self, bus, index, CHESSNUT_UUIDS["op_tx"],
                                 ['write', 'write-without-response'], service)
        self._manager = manager
    
    def WriteValue(self, value, options):
        try:
            bytes_data = bytes([int(b) for b in value])
            
            if not self._manager.connected:
                self._manager._notify_connected(BleManager.CLIENT_CHESSNUT)
            
            self._manager._notify_data_received(bytes_data, BleManager.CLIENT_CHESSNUT)
        except Exception as e:
            log.error(f"[BleManager] Error in Chessnut OP TX: {e}")


class _ChessnutOperationRXCharacteristic(_Characteristic):
    """Chessnut Operation RX Characteristic - sends responses."""
    
    def __init__(self, bus, index, service, manager: BleManager):
        _Characteristic.__init__(self, bus, index, CHESSNUT_UUIDS["op_rx"], ['notify'], service)
        self._manager = manager
    
    def StartNotify(self):
        log.info("[BleManager] Chessnut OP RX StartNotify - client subscribing")
        self.notifying = True
        self._manager._notify_connected(BleManager.CLIENT_CHESSNUT)
    
    def StopNotify(self):
        if not self.notifying:
            return
        log.info("[BleManager] Chessnut client disconnected")
        self.notifying = False
        self._manager._notify_disconnected()


class _ChessnutFENService(_Service):
    """Chessnut FEN Service."""
    
    def __init__(self, bus, index, manager: BleManager):
        _Service.__init__(self, bus, index, CHESSNUT_UUIDS["fen_service"], True)
        self.fen_char = _ChessnutFENCharacteristic(bus, 0, self, manager)
        self.add_characteristic(self.fen_char)


class _ChessnutOperationService(_Service):
    """Chessnut Operation Service."""
    
    def __init__(self, bus, index, manager: BleManager):
        _Service.__init__(self, bus, index, CHESSNUT_UUIDS["op_service"], True)
        self.add_characteristic(_ChessnutOperationTXCharacteristic(bus, 0, self, manager))
        self.op_rx_char = _ChessnutOperationRXCharacteristic(bus, 1, self, manager)
        self.add_characteristic(self.op_rx_char)


class _ChessnutUnknownService(_Service):
    """Chessnut Unknown Service."""
    
    def __init__(self, bus, index):
        _Service.__init__(self, bus, index, CHESSNUT_UUIDS["unk_service"], True)
        self.add_characteristic(_Characteristic(
            bus, 0, CHESSNUT_UUIDS["unk_tx"], ['write', 'write-without-response'], self))
        self.add_characteristic(_Characteristic(
            bus, 1, CHESSNUT_UUIDS["unk_rx"], ['notify'], self))


class _ChessnutOTAService(_Service):
    """Chessnut OTA Service."""
    
    def __init__(self, bus, index):
        _Service.__init__(self, bus, index, CHESSNUT_UUIDS["ota_service"], True)
        self.add_characteristic(_Characteristic(
            bus, 0, CHESSNUT_UUIDS["ota_char1"], ['write', 'notify', 'indicate'], self))
        self.add_characteristic(_Characteristic(
            bus, 1, CHESSNUT_UUIDS["ota_char2"], ['write'], self))
        self.add_characteristic(_ReadOnlyCharacteristic(
            bus, 2, CHESSNUT_UUIDS["ota_char3"], self, bytes([0x00])))
