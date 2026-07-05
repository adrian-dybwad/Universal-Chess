"""
Bluetooth status module.

Provides:
- Functions to query Bluetooth adapter status and format information for menus
- Widget for displaying Bluetooth connection state in the status bar
"""

from PIL import Image, ImageDraw
from .framework.widget import Widget
import subprocess  # nosec B404 - fixed, trusted argv lists (rfkill/hciconfig/bt-admin); no shell, no user input
from typing import Optional

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
        result = subprocess.run(['rfkill', 'list', 'bluetooth'],  # noqa: S607  # nosec B603 B607
                               capture_output=True, text=True, timeout=5)
        # If "Soft blocked: no" is in output, Bluetooth is enabled
        status['enabled'] = 'Soft blocked: no' in result.stdout
    except Exception as e:
        log.warning(f"[Bluetooth] Failed to check rfkill status: {e}")
    
    # Get adapter address via hciconfig
    try:
        result = subprocess.run(['hciconfig', 'hci0'],  # noqa: S607  # nosec B603 B607
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


def _bt_admin(action: str) -> bool:
    """Run the pinned ``bt-admin`` helper for a radio ``action`` (enable/disable).

    Routes the radio toggle through the same passwordless helper the board's BLE
    bring-up uses, so there is one privileged path and one NOPASSWD grant. Uses
    ``sudo -n`` so a missing grant fails fast and is logged rather than hanging
    on a password prompt, and checks the return code (the previous direct
    ``sudo rfkill`` swallowed failures by returning True on any non-exception).
    """
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603 B607
            ['sudo', '-n', BT_ADMIN, action],  # noqa: S607
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
        """Draw the Bluetooth rune (ᛒ) onto the sprite, matching the menu glyph.

        The stem's top and bottom connect to two distinct right-hand vertices,
        and two long diagonals run from each right vertex to the opposite left
        tip, crossing the stem at its centre. Scales to fit within width x height
        with a 1px margin. (An earlier version collapsed both right arrows onto a
        single mid-right point, pinching the right side.)

        Args:
            draw: ImageDraw object for the sprite
            connected: If True, draw with thicker lines (connected state)
        """
        margin = 1
        icon_h = self._height - 2 * margin
        line_width = 2 if connected else 1

        # Centre the glyph in the sprite. The rune is narrower than it is tall,
        # so the horizontal reach is a fraction of the width; the vertices sit
        # ~0.43 of the stem half-height above/below centre (Material proportion).
        cx = self._width // 2
        cy = margin + icon_h // 2
        half_h = icon_h // 2
        horiz = max(2, int(self._width * 0.32))
        vy = max(1, int(half_h * 0.43))

        top = (cx, cy - half_h)
        bottom = (cx, cy + half_h)
        upper_right = (cx + horiz, cy - vy)
        lower_right = (cx + horiz, cy + vy)
        upper_left = (cx - horiz, cy - vy)
        lower_left = (cx - horiz, cy + vy)

        draw.line([top, bottom], fill=0, width=line_width)
        draw.line([top, upper_right], fill=0, width=line_width)
        draw.line([bottom, lower_right], fill=0, width=line_width)
        draw.line([upper_right, lower_left], fill=0, width=line_width)
        draw.line([lower_right, upper_left], fill=0, width=line_width)
    
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
