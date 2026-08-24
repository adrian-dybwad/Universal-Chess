"""
System polling service.

Polls battery, WiFi, and Bluetooth status and updates SystemState.
Battery is polled every 5 seconds, WiFi and Bluetooth every 10 seconds.

Radios the board does not have are never polled. A plain Raspberry Pi Zero has
no wireless die, and ``rfkill list wifi`` prints nothing for a radio that does
not exist -- which the enabled-check reads as "nothing blocked, so it is on",
putting permanently-disconnected Wi-Fi and Bluetooth glyphs in the status bar.
Such a board is reported once as WIFI_ABSENT/BT_ABSENT (which hides the glyphs)
and the 10-second network loop is not started at all, so a single ARMv6 core is
not woken to run four subprocesses against missing hardware.
"""

import os
import subprocess  # nosec B404 - subprocess is only ever invoked with fixed argv lists, never shell=True
import threading
from typing import Optional

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from universalchess.board.wireless_capability import (
    WirelessCapability, get_wireless_capability,
)
from universalchess.connectivity import radio as wifi_radio
from universalchess.state import get_system
from universalchess.state.system import (
    WIFI_ABSENT, WIFI_DISABLED, WIFI_DISCONNECTED, WIFI_CONNECTED,
    BT_ABSENT, BT_DISABLED, BT_DISCONNECTED, BT_CONNECTED
)


# Polling intervals in seconds
BATTERY_POLL_INTERVAL = 5
NETWORK_POLL_INTERVAL = 10


class SystemPollingService:
    """Service that polls system status and updates SystemState."""
    
    def __init__(self, capability: Optional[WirelessCapability] = None):
        """Initialize the system polling service.

        Args:
            capability: Which radios this board has. Injected for tests; read
                from the OS when omitted.
        """
        self._state = get_system()
        self._capability = capability if capability is not None else get_wireless_capability()
        
        # Thread control
        self._running = False
        self._stop_event = threading.Event()
        self._battery_thread: Optional[threading.Thread] = None
        self._network_thread: Optional[threading.Thread] = None
        
        # WiFi hook notification file (dhcpcd writes here on state change)
        self._hook_notification_file = "/var/run/dgtcm-wifi-hook-notify"
        self._last_hook_mtime = 0.0
        
        # Last raw battery payload logged, so the packet is recorded on change
        # only (not every 5s poll). None until the first successful read.
        self._last_battery_raw: Optional[bytes] = None
    
    def start(self) -> None:
        """Start the polling threads."""
        if self._running:
            return
        
        self._running = True
        self._stop_event.clear()
        
        self._battery_thread = threading.Thread(
            target=self._battery_poll_loop,
            name="system-battery-poll",
            daemon=True
        )
        self._battery_thread.start()
        
        # A board with neither radio has nothing for the network loop to observe,
        # so publish the absent states once (the status-bar glyphs hide on them)
        # and skip the thread entirely rather than wake up forever doing nothing.
        if self._capability.has_wifi or self._capability.has_bluetooth:
            self._network_thread = threading.Thread(
                target=self._network_poll_loop,
                name="system-network-poll",
                daemon=True
            )
            self._network_thread.start()
        else:
            self._publish_absent_radios()
            log.info(
                "[SystemPollingService] No Wi-Fi or Bluetooth hardware on this board "
                "(%s); network polling disabled",
                self._capability.pi_model or "model unknown",
            )
        
        log.info("[SystemPollingService] Started polling threads")
    
    def stop(self) -> None:
        """Stop the polling threads."""
        if not self._running:
            return
        
        self._running = False
        self._stop_event.set()
        
        if self._battery_thread:
            self._battery_thread.join(timeout=2.0)
            self._battery_thread = None
        
        if self._network_thread:
            self._network_thread.join(timeout=2.0)
            self._network_thread = None
        
        log.info("[SystemPollingService] Stopped polling threads")
    
    # -------------------------------------------------------------------------
    # Battery polling
    # -------------------------------------------------------------------------
    
    def _battery_poll_loop(self) -> None:
        """Background thread that polls battery status every 5 seconds."""
        while self._running and not self._stop_event.is_set():
            try:
                self._poll_battery()
            except Exception as e:
                log.debug(f"[SystemPollingService] Error polling battery: {e}")
            
            # Interruptible sleep
            for _ in range(BATTERY_POLL_INTERVAL * 10):
                if self._stop_event.is_set():
                    return
                self._stop_event.wait(timeout=0.1)
    
    def _poll_battery(self) -> None:
        """Poll battery status from board controller."""
        try:
            from universalchess.board.sync_centaur import command
            from universalchess.board import board
            
            if board.controller is None:
                return
            
            resp = board.controller.request_response(command.DGT_SEND_BATTERY_INFO)
            if resp is None or len(resp) == 0:
                return
            
            val = resp[0]
            level = val & 0x1F
            charger_state = (val >> 5) & 0x07
            charger_connected = charger_state in (1, 2)
            
            # Record the raw battery packet whenever it changes. The charger
            # decode above treats only states 1 and 2 as "plugged in"; capturing
            # charger_state here confirms what the controller actually reports at
            # full charge (a suspected charge-complete state that is misread as
            # unplugged, which lets the inactivity auto-shutdown fire on mains
            # power). Logged on change only to stay readable in the persistent,
            # rotated log rather than emitting a line every 5s poll.
            if bytes(resp) != self._last_battery_raw:
                self._last_battery_raw = bytes(resp)
                log.info(
                    "[SystemPollingService] Battery packet=%s level=%d "
                    "charger_state=%d charger_connected=%s",
                    " ".join(f"{b:02x}" for b in resp),
                    level, charger_state, charger_connected,
                )
            
            self._state.set_battery(level, charger_connected)
            
        except Exception as e:
            log.debug(f"[SystemPollingService] Error fetching battery: {e}")
    
    # -------------------------------------------------------------------------
    # Network polling (WiFi + Bluetooth)
    # -------------------------------------------------------------------------
    
    def _network_poll_loop(self) -> None:
        """Background thread that polls WiFi and Bluetooth every 10 seconds."""
        while self._running and not self._stop_event.is_set():
            try:
                # Check for dhcpcd hook notification (immediate WiFi update)
                self._check_wifi_hook()
                
                # Poll WiFi
                self._poll_wifi()
                
                # Poll Bluetooth
                self._poll_bluetooth()
                
            except Exception as e:
                log.debug(f"[SystemPollingService] Error in network poll: {e}")
            
            # Interruptible sleep
            for _ in range(NETWORK_POLL_INTERVAL):
                if self._stop_event.is_set():
                    return
                self._stop_event.wait(timeout=1.0)
    
    def _check_wifi_hook(self) -> None:
        """Check for dhcpcd hook notification file changes."""
        if os.path.exists(self._hook_notification_file):
            try:
                current_mtime = os.path.getmtime(self._hook_notification_file)
                if current_mtime > self._last_hook_mtime:
                    self._last_hook_mtime = current_mtime
                    log.debug("[SystemPollingService] dhcpcd hook notification detected")
            except Exception:  # noqa: S110  # nosec B110 - best-effort hook check; failure is non-fatal and intentionally ignored
                pass
    
    def _publish_absent_radios(self) -> None:
        """Report every radio this board lacks, so its indicator hides."""
        if not self._capability.has_wifi:
            self._state.set_wifi(WIFI_ABSENT, 0, None)
        if not self._capability.has_bluetooth:
            self._state.set_bluetooth(BT_ABSENT, None, None)
    
    def _poll_wifi(self) -> None:
        """Poll WiFi status, or report the radio as absent on a board without one."""
        if not self._capability.has_wifi:
            self._state.set_wifi(WIFI_ABSENT, 0, None)
            return
        
        # Check if WiFi is enabled
        if not self._is_wifi_enabled():
            self._state.set_wifi(WIFI_DISABLED, 0, None)
            return
        
        # Check if connected and get signal
        connected, signal_pct, ssid = self._get_wifi_connection()
        
        if not connected:
            self._state.set_wifi(WIFI_DISCONNECTED, 0, None)
            return
        
        # Convert signal percentage to 0-3 strength
        if signal_pct >= 70:
            signal_strength = 3
        elif signal_pct >= 40:
            signal_strength = 2
        elif signal_pct > 0:
            signal_strength = 1
        else:
            signal_strength = 0
        
        self._state.set_wifi(WIFI_CONNECTED, signal_strength, ssid)
    
    def _is_wifi_enabled(self) -> bool:
        """Check if WiFi is enabled (not blocked by rfkill)."""
        return wifi_radio.wifi_enabled()
    
    def _get_wifi_connection(self) -> tuple:
        """Get WiFi connection status.
        
        Uses the shared radio probe rather than a single tool, so the header
        indicator stays correct on both images: Armbian has only ``iw``,
        Raspberry Pi OS additionally has wireless-tools, and neither is a
        declared dependency. Probing one of them alone shows an associated
        board as disconnected wherever that one is missing.
        
        Returns:
            Tuple of (connected: bool, signal_pct: int, ssid: Optional[str])
        """
        parsed = wifi_radio.link_status()
        if not parsed["connected"] or not parsed["ssid"]:
            return (False, 0, None)
        signal_pct = 0
        if parsed["signal_dbm"] is not None:
            signal_pct = wifi_radio.dbm_to_percent(parsed["signal_dbm"])
        return (True, signal_pct, parsed["ssid"])
    
    def _poll_bluetooth(self) -> None:
        """Poll Bluetooth status, or report the controller as absent on a board without one."""
        if not self._capability.has_bluetooth:
            self._state.set_bluetooth(BT_ABSENT, None, None)
            return
        
        # Check if Bluetooth is enabled
        if not self._is_bluetooth_enabled():
            self._state.set_bluetooth(BT_DISABLED, None, None)
            return
        
        # Check if connected
        connected, device_name = self._get_bluetooth_connection()
        
        if connected:
            self._state.set_bluetooth(BT_CONNECTED, device_name, None)
        else:
            self._state.set_bluetooth(BT_DISCONNECTED, None, None)
    
    def _is_bluetooth_enabled(self) -> bool:
        """Check if Bluetooth is enabled (not blocked by rfkill)."""
        try:
            result = subprocess.run(  # noqa: S603  # nosec B603 B607 - argv list (no shell); fixed system utility
                ['rfkill', 'list', 'bluetooth'],  # noqa: S607 - argv list (no shell); fixed system utility
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return 'blocked: yes' not in result.stdout.lower()
        except Exception as e:
            log.debug(f"[SystemPollingService] Error checking Bluetooth enabled: {e}")
        return True  # Assume enabled if check fails
    
    def _get_bluetooth_connection(self) -> tuple:
        """Get Bluetooth connection status.
        
        Returns:
            Tuple of (connected: bool, device_name: Optional[str])
        """
        try:
            result = subprocess.run(  # noqa: S603  # nosec B603 B607 - argv list (no shell); fixed system utility
                ['bluetoothctl', 'devices', 'Connected'],  # noqa: S607 - argv list (no shell); fixed system utility
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                output = result.stdout.strip()
                if output:
                    # Parse "Device XX:XX:XX:XX:XX:XX DeviceName"
                    parts = output.split(' ', 2)
                    if len(parts) >= 3:
                        return (True, parts[2])
                    return (True, None)
        except Exception as e:
            log.debug(f"[SystemPollingService] Error getting Bluetooth connection: {e}")
        return (False, None)


# -----------------------------------------------------------------------------
# Singleton instance
# -----------------------------------------------------------------------------

_instance: Optional[SystemPollingService] = None
_lock = threading.Lock()


def get_system_service() -> SystemPollingService:
    """Get the singleton SystemPollingService instance.
    
    Returns:
        The global SystemPollingService instance.
    """
    global _instance
    
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = SystemPollingService()
    
    return _instance
