"""
Bouncing ball widget for demo purposes.
"""

from PIL import Image, ImageDraw
from .framework.widget import Widget


class BallWidget(Widget):
    """Bouncing ball widget for display demos."""
    
    def __init__(self, x: int, y: int, update_callback, radius: int = 8):
        super().__init__(x, y, radius * 2, radius * 2, update_callback)
        self.radius = radius
    
    def set_position(self, x: int, y: int) -> None:
        """Set ball position."""
        if self.x != x or self.y != y:
            self.x = x
            self.y = y
            self.invalidate_and_update()
    
    def get_mask(self) -> Image.Image:
        """Get mask for transparent compositing."""
        mask = Image.new("1", (self.width, self.height), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse([0, 0, self.width - 1, self.height - 1], fill=255)
        return mask
    
    def render(self, sprite: Image.Image) -> None:
        """Render ball onto the sprite image."""
        draw = ImageDraw.Draw(sprite)
        draw.ellipse([0, 0, self.width - 1, self.height - 1], fill=0)
