"""
System state - battery, WiFi, and Bluetooth status.

Holds observable system status that widgets can display. The actual polling
and hardware interaction is handled elsewhere (board module, network utils).

Widgets observe this state to display status indicators in the status bar.
"""

import logging
from typing import Optional, Callable, List

log = logging.getLogger(__name__)


# WiFi state constants
WIFI_DISABLED = 0
WIFI_DISCONNECTED = 1
WIFI_CONNECTED = 2

# Bluetooth state constants
BT_DISABLED = 0
BT_DISCONNECTED = 1
BT_CONNECTED = 2


class SystemState:
    """Observable system status.
    
    Holds:
    - Battery level and charging state
    - WiFi connection state and signal strength
    - Bluetooth state and connection info
    
    Observers are notified when any status changes.
    """
    
    def __init__(self):
        """Initialize with unknown/default states."""
        # Battery state
        self._battery_level: Optional[int] = None  # 0-20 scale, None = unknown
        self._charger_connected: bool = False
        
        # WiFi state
        self._wifi_state: int = WIFI_DISCONNECTED  # WIFI_DISABLED, WIFI_DISCONNECTED, WIFI_CONNECTED
        self._wifi_signal_strength: int = 0  # 0-3 (0 = no signal, 1-3 = weak/medium/strong)
        self._wifi_ssid: Optional[str] = None
        
        # Bluetooth state
        self._bt_state: int = BT_DISCONNECTED  # BT_DISABLED, BT_DISCONNECTED, BT_CONNECTED
        self._bt_device_name: Optional[str] = None  # Connected device name
        self._bt_client_type: Optional[str] = None  # 'millennium', 'pegasus', 'chessnut', etc.
        
        # Observer callbacks
        self._on_battery_change: List[Callable[[], None]] = []
        self._on_wifi_change: List[Callable[[], None]] = []
        self._on_bluetooth_change: List[Callable[[], None]] = []
    
    # -------------------------------------------------------------------------
    # Battery properties
    # -------------------------------------------------------------------------
    
    @property
    def battery_level(self) -> Optional[int]:
        """Battery level on 0-20 scale, or None if unknown."""
        return self._battery_level
    
    @property
    def charger_connected(self) -> bool:
        """Whether the charger is connected."""
        return self._charger_connected
    
    @property
    def battery_percent(self) -> Optional[int]:
        """Battery level as percentage (0-100), or None if unknown."""
        if self._battery_level is None:
            return None
        return self._battery_level * 5  # 0-20 scale to 0-100
    
    # -------------------------------------------------------------------------
    # WiFi properties
    # -------------------------------------------------------------------------
    
    @property
    def wifi_state(self) -> int:
        """WiFi state (WIFI_DISABLED, WIFI_DISCONNECTED, WIFI_CONNECTED)."""
        return self._wifi_state
    
    @property
    def wifi_signal_strength(self) -> int:
        """WiFi signal strength (0-3)."""
        return self._wifi_signal_strength
    
    @property
    def wifi_ssid(self) -> Optional[str]:
        """Connected WiFi network name, or None."""
        return self._wifi_ssid
    
    @property
    def wifi_connected(self) -> bool:
        """Whether WiFi is connected."""
        return self._wifi_state == WIFI_CONNECTED
    
    @property
    def wifi_enabled(self) -> bool:
        """Whether WiFi is enabled (not disabled)."""
        return self._wifi_state != WIFI_DISABLED
    
    # -------------------------------------------------------------------------
    # Bluetooth properties
    # -------------------------------------------------------------------------
    
    @property
    def bt_state(self) -> int:
        """Bluetooth state (BT_DISABLED, BT_DISCONNECTED, BT_CONNECTED)."""
        return self._bt_state
    
    @property
    def bt_device_name(self) -> Optional[str]:
        """Connected Bluetooth device name, or None."""
        return self._bt_device_name
    
    @property
    def bt_client_type(self) -> Optional[str]:
        """Type of connected Bluetooth client, or None."""
        return self._bt_client_type
    
    @property
    def bt_connected(self) -> bool:
        """Whether a Bluetooth device is connected."""
        return self._bt_state == BT_CONNECTED
    
    @property
    def bt_enabled(self) -> bool:
        """Whether Bluetooth is enabled (not disabled)."""
        return self._bt_state != BT_DISABLED
    
    # -------------------------------------------------------------------------
    # Observer management
    # -------------------------------------------------------------------------
    
    def on_battery_change(self, callback: Callable[[], None]) -> None:
        """Register callback for battery state changes.
        
        Args:
            callback: Function called when battery level or charger state changes.
        """
        if callback not in self._on_battery_change:
            self._on_battery_change.append(callback)
    
    def on_wifi_change(self, callback: Callable[[], None]) -> None:
        """Register callback for WiFi state changes.
        
        Args:
            callback: Function called when WiFi state or signal changes.
        """
        if callback not in self._on_wifi_change:
            self._on_wifi_change.append(callback)
    
    def on_bluetooth_change(self, callback: Callable[[], None]) -> None:
        """Register callback for Bluetooth state changes.
        
        Args:
            callback: Function called when Bluetooth state changes.
        """
        if callback not in self._on_bluetooth_change:
            self._on_bluetooth_change.append(callback)
    
    def remove_observer(self, callback: Callable) -> None:
        """Remove a previously registered callback.
        
        Args:
            callback: The callback to remove (from any observer list).
        """
        if callback in self._on_battery_change:
            self._on_battery_change.remove(callback)
        if callback in self._on_wifi_change:
            self._on_wifi_change.remove(callback)
        if callback in self._on_bluetooth_change:
            self._on_bluetooth_change.remove(callback)
    
    def _notify_battery(self) -> None:
        """Notify battery observers."""
        for callback in self._on_battery_change:
            try:
                callback()
            except Exception:
                log.exception("Battery state observer callback failed")
    
    def _notify_wifi(self) -> None:
        """Notify WiFi observers."""
        for callback in self._on_wifi_change:
            try:
                callback()
            except Exception:
                log.exception("WiFi state observer callback failed")
    
    def _notify_bluetooth(self) -> None:
        """Notify Bluetooth observers."""
        for callback in self._on_bluetooth_change:
            try:
                callback()
            except Exception:
                log.exception("Bluetooth state observer callback failed")
    
    # -------------------------------------------------------------------------
    # State mutations
    # -------------------------------------------------------------------------
    
    def set_battery(self, level: Optional[int], charger_connected: bool) -> None:
        """Update battery state.
        
        Args:
            level: Battery level (0-20 scale), or None if unknown.
            charger_connected: Whether charger is connected.
        """
        changed = (level != self._battery_level or 
                   charger_connected != self._charger_connected)
        self._battery_level = level
        self._charger_connected = charger_connected
        if changed:
            self._notify_battery()
    
    def set_wifi(self, state: int, signal_strength: int = 0, 
                 ssid: Optional[str] = None) -> None:
        """Update WiFi state.
        
        Args:
            state: WIFI_DISABLED, WIFI_DISCONNECTED, or WIFI_CONNECTED.
            signal_strength: Signal strength 0-3.
            ssid: Connected network name, or None.
        """
        changed = (state != self._wifi_state or 
                   signal_strength != self._wifi_signal_strength or
                   ssid != self._wifi_ssid)
        self._wifi_state = state
        self._wifi_signal_strength = signal_strength
        self._wifi_ssid = ssid
        if changed:
            self._notify_wifi()
    
    def set_bluetooth(self, state: int, device_name: Optional[str] = None,
                      client_type: Optional[str] = None) -> None:
        """Update Bluetooth state.
        
        Args:
            state: BT_DISABLED, BT_DISCONNECTED, or BT_CONNECTED.
            device_name: Connected device name, or None.
            client_type: Client type ('millennium', 'pegasus', etc.), or None.
        """
        changed = (state != self._bt_state or 
                   device_name != self._bt_device_name or
                   client_type != self._bt_client_type)
        self._bt_state = state
        self._bt_device_name = device_name
        self._bt_client_type = client_type
        if changed:
            self._notify_bluetooth()


# -----------------------------------------------------------------------------
# Singleton instance
# -----------------------------------------------------------------------------

_instance: Optional[SystemState] = None


def get_system() -> SystemState:
    """Get the singleton SystemState instance.
    
    Returns:
        The global SystemState instance.
    """
    global _instance
    if _instance is None:
        _instance = SystemState()
    return _instance


def reset_system() -> SystemState:
    """Reset the singleton to a fresh instance.
    
    Primarily for testing.
    
    Returns:
        The new SystemState instance.
    """
    global _instance
    _instance = SystemState()
    return _instance
