"""
Battery level indicator widget.

Observes SystemState for battery level and charger status.
"""

from PIL import Image, ImageDraw
from .framework.widget import Widget

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from universalchess.state import get_system


class BatteryWidget(Widget):
    """Battery level indicator using drawn graphics.
    
    Args:
        x: X position
        y: Y position
        width: Widget width in pixels
        height: Widget height in pixels
        update_callback: Callback to trigger display updates. Must not be None.
    """
    
    def __init__(self, x: int, y: int, width: int, height: int, update_callback):
        super().__init__(x, y, width, height, update_callback)
        self._state = get_system()
        self._state.on_battery_change(self._on_battery_change)
    
    def _on_battery_change(self) -> None:
        """Called when battery state changes."""
        self.invalidate_and_update()
    
    def start(self) -> None:
        """No-op. Polling is handled by SystemPollingService."""
        pass
    
    def stop(self) -> None:
        """Unregister from state."""
        self._state.remove_observer(self._on_battery_change)
    
    @property
    def level(self) -> int:
        """Battery level (0-20) from state."""
        level = self._state.battery_level
        return level if level is not None else 10  # Default to ~50%
    
    @property
    def charger_connected(self) -> bool:
        """Charger connection status from state."""
        return self._state.charger_connected
    
    def render(self, sprite: Image.Image) -> None:
        """Render battery indicator with level bars and charging flash icon.
        
        Scales to fit the configured widget width and height.
        Battery body uses most of the width with a terminal nub on the right.
        
        When charging, displays a lightning bolt overlay with XOR effect -
        white over black level bars and black over white background.
        """
        draw = ImageDraw.Draw(sprite)
        
        batterylevel = self.level
        
        # Scale factor based on width (20 is the new base size)
        w = self.width
        h = self.height
        
        # Battery dimensions - scale to fit widget
        # Terminal nub is ~3px wide, body takes the rest
        term_width = max(2, int(w * 0.15))
        body_width = w - term_width - 1
        
        body_left = 0
        body_top = 1
        body_right = body_width
        body_bottom = h - 2
        
        # Battery terminal (nub on right)
        term_left = body_right
        term_top = max(2, h // 4)
        term_right = w - 1
        term_bottom = h - max(2, h // 4) - 1
        
        # Sprite is pre-filled white
        
        # Draw battery outline
        draw.rectangle([body_left, body_top, body_right, body_bottom], outline=0, width=1)
        draw.rectangle([term_left, term_top, term_right, term_bottom], fill=0)
        
        # Calculate fill width based on level (0-20)
        inner_left = body_left + 2
        inner_top = body_top + 2
        inner_right = body_right - 2
        inner_bottom = body_bottom - 2
        inner_width = inner_right - inner_left
        
        fill_width = int((batterylevel / 20.0) * inner_width)
        fill_right = inner_left + fill_width
        
        # Draw level bars
        if fill_width > 0:
            draw.rectangle([inner_left, inner_top, fill_right, inner_bottom], fill=0)
        
        # Draw charging lightning bolt if connected
        if self.charger_connected:
            # Lightning bolt - scaled to fit battery body
            cx = (inner_left + inner_right) // 2
            cy = (inner_top + inner_bottom) // 2
            
            # Scale bolt size based on inner dimensions
            bolt_h = inner_bottom - inner_top
            bolt_w = min(inner_width // 2, bolt_h)
            
            # Create bolt mask (temporary image for XOR operation)
            bolt_mask = Image.new("1", (w, h), 0)
            bolt_draw = ImageDraw.Draw(bolt_mask)
            
            # Scale bolt points
            sx = bolt_w / 10.0  # horizontal scale
            sy = bolt_h / 8.0   # vertical scale
            
            top_triangle = [
                (cx + int(4 * sx), inner_top),
                (cx - int(2 * sx), cy),
                (cx + int(1 * sx), cy),
            ]
            bolt_draw.polygon(top_triangle, fill=1)
            
            bottom_triangle = [
                (cx + int(2 * sx), cy - 1),
                (cx - int(4 * sx), inner_bottom),
                (cx - int(1 * sx), cy - 1),
            ]
            bolt_draw.polygon(bottom_triangle, fill=1)
            
            # XOR the bolt onto the sprite
            sprite_pixels = sprite.load()
            bolt_pixels = bolt_mask.load()
            for y in range(h):
                for x in range(w):
                    if bolt_pixels[x, y] == 1:
                        current = sprite_pixels[x, y]
                        sprite_pixels[x, y] = 255 if current == 0 else 0
