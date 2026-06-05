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
            # Draw the bolt as a single bold polygon spanning the battery body,
            # then XOR it onto the sprite so it reads as black over the (white)
            # empty area and white over the (black) level bars - staying visible
            # at any fill level. Points are fractions of the inner body box.
            inner_height = inner_bottom - inner_top
            
            def _bolt_point(fx: float, fy: float) -> tuple:
                return (int(round(inner_left + fx * inner_width)),
                        int(round(inner_top + fy * inner_height)))
            
            bolt_points = [
                _bolt_point(0.70, 0.00),
                _bolt_point(0.15, 0.60),
                _bolt_point(0.45, 0.60),
                _bolt_point(0.30, 1.00),
                _bolt_point(0.85, 0.42),
                _bolt_point(0.55, 0.42),
            ]
            
            bolt_mask = Image.new("1", (w, h), 0)
            ImageDraw.Draw(bolt_mask).polygon(bolt_points, fill=1)
            
            # XOR the bolt onto the sprite. Test the mask pixel as truthy (it is
            # nonzero where drawn) to be independent of mode-"1" pixel-value
            # conventions (0/1 vs 0/255) across Pillow versions.
            sprite_pixels = sprite.load()
            bolt_pixels = bolt_mask.load()
            for y in range(h):
                for x in range(w):
                    if bolt_pixels[x, y]:
                        sprite_pixels[x, y] = 255 if sprite_pixels[x, y] == 0 else 0
