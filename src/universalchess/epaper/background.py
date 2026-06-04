"""
Background widget with dithered grayscale patterns.

On a 1-bit e-paper display, grayscale is simulated using dithering patterns.
This widget provides a full-screen background with configurable shade.
"""

from PIL import Image
from .framework.widget import Widget, DITHER_PATTERNS

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


class BackgroundWidget(Widget):
    """Full-screen background widget with dithered grayscale.
    
    Uses ordered dithering to simulate grayscale on 1-bit display.
    Shade levels range from 0 (white) to 16 (black).
    """
    
    def __init__(self, width: int, height: int, update_callback, shade: int = 0):
        """Create a background widget.
        
        Args:
            width: Display width
            height: Display height
            update_callback: Callback to trigger display updates. Must not be None.
            shade: Grayscale level 0-16 (0=white, 8=50% gray, 16=black)
        """
        super().__init__(0, 0, width, height, update_callback, background_shade=shade)
    
    def set_shade(self, shade: int) -> None:
        """Set the background shade level.
        
        Args:
            shade: Grayscale level 0-16 (0=white, 8=50% gray, 16=black)
        """
        shade = max(0, min(16, shade))
        if shade != self._background_shade:
            self._background_shade = shade
            self.invalidate_and_update()
    
    def render(self, sprite: Image.Image) -> None:
        """Render the dithered background pattern onto the sprite image."""
        self.draw_background_on_sprite(sprite)
