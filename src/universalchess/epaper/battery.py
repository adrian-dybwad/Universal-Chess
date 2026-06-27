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


def render_battery(sprite: Image.Image, x: int, y: int, width: int, height: int,
                   level: int, charger_connected: bool) -> None:
    """Draw a battery indicator into ``sprite`` at the given box.

    Pure drawing helper shared by :class:`BatteryWidget` (status bar) and the
    shutdown splash so the battery glyph is defined once. Takes the level and
    charger state as plain arguments rather than reading global state, keeping it
    decoupled and reusable from any render context.

    Args:
        sprite: Target image (mode "1") to draw onto.
        x: Left edge of the battery box within ``sprite``.
        y: Top edge of the battery box within ``sprite``.
        width: Battery box width in pixels.
        height: Battery box height in pixels.
        level: Battery level on a 0-20 scale.
        charger_connected: When True, overlays the charging lightning bolt.
    """
    draw = ImageDraw.Draw(sprite)

    # Terminal nub is ~15% of the width; the body takes the rest.
    term_width = max(2, int(width * 0.15))
    body_width = width - term_width - 1

    body_left = x
    body_top = y + 1
    body_right = x + body_width
    body_bottom = y + height - 2

    term_left = body_right
    term_top = y + max(2, height // 4)
    term_right = x + width - 1
    term_bottom = y + height - max(2, height // 4) - 1

    draw.rectangle([body_left, body_top, body_right, body_bottom], outline=0, width=1)
    draw.rectangle([term_left, term_top, term_right, term_bottom], fill=0)

    inner_left = body_left + 2
    inner_top = body_top + 2
    inner_right = body_right - 2
    inner_bottom = body_bottom - 2
    inner_width = inner_right - inner_left

    fill_width = int((level / 20.0) * inner_width)
    fill_right = inner_left + fill_width

    if fill_width > 0:
        draw.rectangle([inner_left, inner_top, fill_right, inner_bottom], fill=0)

    if charger_connected:
        # Draw the bolt as a single bold polygon spanning the battery body, then
        # XOR it onto the sprite so it reads as black over the (white) empty area
        # and white over the (black) level bars - staying visible at any fill
        # level. Points are fractions of the inner body box.
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

        bolt_mask = Image.new("1", (width, height), 0)
        ImageDraw.Draw(bolt_mask).polygon(bolt_points, fill=1)

        # XOR the bolt onto the sprite. Test the mask pixel as truthy (it is
        # nonzero where drawn) to be independent of mode-"1" pixel-value
        # conventions (0/1 vs 0/255) across Pillow versions.
        sprite_pixels = sprite.load()
        bolt_pixels = bolt_mask.load()
        for by in range(height):
            for bx in range(width):
                if bolt_pixels[bx, by]:
                    px, py = x + bx, y + by
                    sprite_pixels[px, py] = 255 if sprite_pixels[px, py] == 0 else 0


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
        render_battery(sprite, 0, 0, self.width, self.height,
                       self.level, self.charger_connected)
