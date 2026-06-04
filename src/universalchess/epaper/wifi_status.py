"""
WiFi status indicator widget.

Displays WiFi signal strength in the status bar:
- Signal strength (1-3 bars) when connected
- Cross overlay when WiFi is disabled
- No signal bars when not connected but enabled
"""

from PIL import Image, ImageDraw
from .framework.widget import Widget

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from universalchess.state import get_system
from universalchess.state.system import WIFI_DISABLED, WIFI_DISCONNECTED, WIFI_CONNECTED


class WiFiStatusWidget(Widget):
    """WiFi status indicator widget showing connection state and signal strength.
    
    Args:
        x: X position
        y: Y position
        size: Widget size in pixels (default 16 for status bar)
        update_callback: Callback to trigger display updates. Must not be None.
    """
    
    def __init__(self, x: int, y: int, size: int, update_callback):
        super().__init__(x, y, size, size, update_callback)
        self._size = size
        self._state = get_system()
        self._state.on_wifi_change(self._on_wifi_change)
        self.visible = self._state.wifi_enabled
    
    def _on_wifi_change(self) -> None:
        """Called when WiFi state changes."""
        self.visible = self._state.wifi_enabled
        self.invalidate_and_update()
    
    def stop(self) -> None:
        """Unregister from state."""
        self._state.remove_observer(self._on_wifi_change)
    
    def _draw_wifi_signal_icon(self, draw: ImageDraw.Draw, strength: int = 3):
        """Draw a WiFi signal icon with variable strength onto sprite.
        
        Scales automatically based on widget size.
        
        Args:
            draw: ImageDraw object for the sprite
            strength: Signal strength 0-3 (0=disconnected, 1=weak, 2=medium, 3=strong)
        """
        # Scale factor based on size (16 is the base size)
        s = self._size / 16.0
        
        # Center point and base position (bottom center of icon)
        cx = self._size // 2
        base_y = int(13 * s)
        
        # Arc radii scaled to size
        radii = [int(3 * s), int(6 * s), int(9 * s)]
        
        for i, radius in enumerate(radii):
            # Determine line width based on active/inactive
            if i < strength:
                # Active arc - thicker line
                width = max(2, int(2 * s))
            else:
                # Inactive arc - thin line
                width = max(1, int(1 * s))
            
            draw.arc([cx - radius, base_y - radius, cx + radius, base_y + radius],
                    start=225, end=315, fill=0, width=width)
        
        # Small dot at the bottom center (always drawn)
        dot_r = max(1, int(1 * s))
        draw.ellipse([cx - dot_r, base_y - dot_r, cx + dot_r, base_y + dot_r], fill=0)
    
    def _draw_disabled_cross(self, draw: ImageDraw.Draw):
        """Draw a cross overlay to indicate WiFi is disabled."""
        # Scale factor based on size
        s = self._size / 16.0
        margin = int(2 * s)
        width = max(2, int(2 * s))
        
        # Draw diagonal cross over the icon
        draw.line([margin, margin, 
                   self._size - margin, self._size - margin], fill=0, width=width)
        draw.line([self._size - margin, margin, 
                   margin, self._size - margin], fill=0, width=width)
    
    def render(self, sprite: Image.Image) -> None:
        """Render WiFi status icon with signal strength or disabled indicator."""
        draw = ImageDraw.Draw(sprite)
        
        # Read state
        wifi_state = self._state.wifi_state
        signal_strength = self._state.wifi_signal_strength
        
        # Sprite is pre-filled white
        
        if wifi_state == WIFI_DISABLED:
            # Draw WiFi icon with cross overlay
            self._draw_wifi_signal_icon(draw, strength=0)
            self._draw_disabled_cross(draw)
        elif wifi_state == WIFI_CONNECTED:
            # Draw WiFi icon with signal strength
            self._draw_wifi_signal_icon(draw, strength=signal_strength)
        else:
            # Disconnected but enabled - show empty arcs
            self._draw_wifi_signal_icon(draw, strength=0)
