"""
Bluetooth status module.

Provides:
- Functions to query Bluetooth adapter status and format information for menus
- Widget for displaying Bluetooth connection state in the status bar
"""

from PIL import Image, ImageDraw
from .framework.widget import Widget
import subprocess
from typing import Optional, List

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from universalchess.state import get_system
from universalchess.state.system import BT_DISABLED, BT_DISCONNECTED, BT_CONNECTED
from universalchess.paths import BT_ADMIN


# Advertised service names for different protocols
ADVERTISED_NAMES = {
    'pegasus': 'DGT PEGASUS',
    'millennium': 'MILLENNIUM CHESS',
    'chessnut': 'Chessnut Air',
}


def get_bluetooth_status(device_name: Optional[str] = None,
                         ble_manager=None,
                         rfcomm_connected: bool = False) -> dict:
    """Get current Bluetooth adapter status and information.
    
    Args:
        device_name: The primary advertised device name
        ble_manager: Optional BleManager instance to check BLE connection status
        rfcomm_connected: Whether an RFCOMM client is connected
    
    Returns:
        Dictionary with keys:
        - enabled: bool, whether Bluetooth is enabled (not blocked by rfkill)
        - powered: bool, whether adapter is powered on
        - device_name: str, the primary advertised device name
        - address: str, the Bluetooth MAC address
        - ble_connected: bool, whether a BLE client is connected
        - ble_client_type: str or None, type of connected BLE client
        - rfcomm_connected: bool, whether an RFCOMM client is connected
        - advertised_names: list of str, all names being advertised
        - advertising: dict, BLE advertisement registration status (see
          ble_advertising_status); 'ok' is False when BlueZ rejected the
          advertisements, which hides the board from BLE scans
    """
    status = {
        'enabled': False,
        'powered': False,
        'device_name': device_name,
        'address': '',
        'ble_connected': False,
        'ble_client_type': None,
        'rfcomm_connected': rfcomm_connected,
        'advertised_names': list(ADVERTISED_NAMES.values()),
        'advertising': None,
    }
    
    # Check rfkill status
    try:
        result = subprocess.run(['rfkill', 'list', 'bluetooth'],
                               capture_output=True, text=True, timeout=5)
        # If "Soft blocked: no" is in output, Bluetooth is enabled
        status['enabled'] = 'Soft blocked: no' in result.stdout
    except Exception as e:
        log.warning(f"[Bluetooth] Failed to check rfkill status: {e}")
    
    # Get adapter address via hciconfig
    try:
        result = subprocess.run(['hciconfig', 'hci0'],
                               capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            # Parse output for BD Address
            for line in result.stdout.split('\n'):
                if 'BD Address:' in line:
                    parts = line.split('BD Address:')
                    if len(parts) > 1:
                        addr = parts[1].strip().split()[0]
                        status['address'] = addr
                if 'UP RUNNING' in line:
                    status['powered'] = True
    except Exception as e:
        log.warning(f"[Bluetooth] Failed to get adapter info: {e}")
    
    # Check BLE connection status
    if ble_manager is not None:
        status['ble_connected'] = ble_manager.connected
        status['ble_client_type'] = getattr(ble_manager, 'client_type', None)

    # BLE advertisement registration status from the live status engine (the
    # board's in-process source of truth). Read it directly so the field is
    # always populated with the same schema the web sees, even if no ble_manager
    # was passed.
    from universalchess.managers.bluetooth_status_state import (
        get_bluetooth_status_state,
    )
    snapshot = get_bluetooth_status_state().to_dict()
    status['advertising'] = snapshot['advertising']
    status['adv_state'] = snapshot['adv_state']

    # Fall back to the engine's link info for the connection readout when the
    # manager was not supplied (the engine tracks BLE and RFCOMM alike).
    if ble_manager is None and snapshot['connected']:
        status['ble_connected'] = snapshot['transport'] == 'ble'
        status['ble_client_type'] = snapshot['emulator']
        if snapshot['transport'] == 'rfcomm':
            status['rfcomm_connected'] = True

    return status


def format_status_label(status: dict) -> str:
    """Format Bluetooth status into a multi-line label for display.
    
    Shows device name, MAC address, connection status, and connected client type.
    
    Args:
        status: Dictionary from get_bluetooth_status()
        
    Returns:
        Multi-line string for display
    """
    lines = []
    
    # Device name
    lines.append(status['device_name'])
    
    # MAC address
    if status['address']:
        lines.append(status['address'])
    
    # Connection status
    if status['ble_connected']:
        client_type = status.get('ble_client_type', 'BLE')
        if client_type:
            lines.append(f"BLE: {client_type}")
        else:
            lines.append("BLE: Connected")
    elif status['rfcomm_connected']:
        lines.append("RFCOMM: Connected")
    elif status['enabled'] and status['powered']:
        lines.append("Ready")
    elif status['enabled']:
        lines.append("Enabled")
    else:
        lines.append("Disabled")
    
    return '\n'.join(lines)


def get_advertised_names_label() -> str:
    """Get a formatted label showing all advertised names.
    
    Returns:
        Multi-line string with all advertised names
    """
    return '\n'.join(ADVERTISED_NAMES.values())


def _bt_admin(action: str) -> bool:
    """Run the pinned ``bt-admin`` helper for a radio ``action`` (enable/disable).

    Routes the radio toggle through the same passwordless helper the board's BLE
    bring-up uses, so there is one privileged path and one NOPASSWD grant. Uses
    ``sudo -n`` so a missing grant fails fast and is logged rather than hanging
    on a password prompt, and checks the return code (the previous direct
    ``sudo rfkill`` swallowed failures by returning True on any non-exception).
    """
    try:
        result = subprocess.run(
            ['sudo', '-n', BT_ADMIN, action],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip() or "unknown error"
            log.error(f"[Bluetooth] bt-admin {action} failed: {err}")
            return False
        log.info(f"[Bluetooth] Radio {action}d via bt-admin")
        return True
    except Exception as e:
        log.error(f"[Bluetooth] Failed to {action}: {e}")
        return False


def enable_bluetooth() -> bool:
    """Enable the Bluetooth radio. Returns command success."""
    return _bt_admin("enable")


def disable_bluetooth() -> bool:
    """Disable the Bluetooth radio. Returns command success."""
    return _bt_admin("disable")


class BluetoothStatusWidget(Widget):
    """Bluetooth status indicator widget showing connection state.
    
    Displays a Bluetooth icon with different states:
    - Solid icon when connected
    - Outline icon when enabled but not connected
    - Icon with X overlay when disabled
    
    Args:
        x: X position
        y: Y position
        width: Widget width in pixels
        height: Widget height in pixels
        update_callback: Callback to trigger display updates. Must not be None.
    """
    
    def __init__(self, x: int, y: int, width: int, height: int, update_callback):
        super().__init__(x, y, width, height, update_callback)
        self._width = width
        self._height = height
        self._state = get_system()
        self._state.on_bluetooth_change(self._on_bluetooth_change)
        
        # Set initial visibility based on state
        self.visible = self._state.bt_enabled
    
    def _on_bluetooth_change(self) -> None:
        """Called when Bluetooth state changes."""
        self.visible = self._state.bt_enabled
        self.invalidate_and_update()
    
    def stop(self) -> None:
        """Unregister from state."""
        self._state.remove_observer(self._on_bluetooth_change)
    
    def _draw_bluetooth_icon(self, draw: ImageDraw.Draw, connected: bool = False) -> None:
        """Draw a Bluetooth icon onto sprite.
        
        The icon is a stylized "B" shape with angular notches.
        Scales to fit within width x height, with 1px margin on all sides.
        
        Args:
            draw: ImageDraw object for the sprite
            connected: If True, draw with thicker lines (connected state)
        """
        # 1px margin around the icon
        margin = 1
        icon_x = margin
        icon_y = margin
        icon_w = self._width - 2 * margin
        icon_h = self._height - 2 * margin
        
        # Scale factors based on icon dimensions (10x14 base for 12x16 widget)
        sx = icon_w / 10.0
        sy = icon_h / 14.0
        
        # Vertical center of icon area
        cy = icon_y + icon_h // 2
        
        # The vertical line position: shift left to balance visual weight
        # since arrows only extend to the right. Place at ~1/3 of icon width.
        cx = icon_x + icon_w // 3
        
        # Bluetooth runic "B" shape
        top_y = icon_y
        bottom_y = icon_y + icon_h - 1
        
        line_width = 2 if connected else 1
        
        # Main vertical line
        draw.line([(cx, top_y), (cx, bottom_y)], fill=0, width=line_width)
        
        # Arrow points to the right
        arrow_right = cx + int(4 * sx)
        
        # Top arrow: center-top -> right corner -> center-middle
        draw.line([(cx, top_y), (arrow_right, cy - int(3 * sy))], fill=0, width=line_width)
        draw.line([(arrow_right, cy - int(3 * sy)), (cx, cy)], fill=0, width=line_width)
        
        # Bottom arrow: center-bottom -> right corner -> center-middle
        draw.line([(cx, bottom_y), (arrow_right, cy + int(3 * sy))], fill=0, width=line_width)
        draw.line([(arrow_right, cy + int(3 * sy)), (cx, cy)], fill=0, width=line_width)
    
    def _draw_disabled_cross(self, draw: ImageDraw.Draw) -> None:
        """Draw a cross overlay to indicate Bluetooth is disabled.
        
        Uses 1px margin to match the icon margin.
        """
        margin = 1
        x1 = margin
        y1 = margin
        x2 = self._width - margin - 1
        y2 = self._height - margin - 1
        
        draw.line([(x1, y1), (x2, y2)], fill=0, width=1)
        draw.line([(x2, y1), (x1, y2)], fill=0, width=1)
    
    def render(self, sprite: Image.Image) -> None:
        """Render Bluetooth status icon onto sprite."""
        draw = ImageDraw.Draw(sprite)
        
        # Read state
        bt_state = self._state.bt_state
        
        # Sprite is pre-filled white
        
        if bt_state == BT_DISABLED:
            # Draw icon with cross overlay
            self._draw_bluetooth_icon(draw, connected=False)
            self._draw_disabled_cross(draw)
        elif bt_state == BT_CONNECTED:
            # Draw solid icon
            self._draw_bluetooth_icon(draw, connected=True)
        else:
            # Disconnected - draw outline icon
            self._draw_bluetooth_icon(draw, connected=False)
