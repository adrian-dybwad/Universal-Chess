"""Setup-mode status widget.

Shown in place of the turn indicator while the Chessnut puzzle setup mode is
active. Communicates that move interpretation is suspended and the operator is
arranging pieces; the board widget above shows the position as it evolves.
"""

from PIL import Image, ImageDraw

from .framework.widget import Widget
from .text import TextWidget, Justify


class SetupStatusWidget(Widget):
    """Widget announcing that puzzle setup mode is active.

    Occupies the clock/turn-indicator region (y=144) so the turn indicator is
    hidden while setup is in progress, signalling that normal play is paused.
    """

    def __init__(self, x: int = 0, y: int = 144, width: int = 128, height: int = 72,
                 update_callback=None):
        """Initialize the setup status widget.

        Args:
            x: X position (default 0).
            y: Y position (default 144, the turn-indicator region).
            width: Widget width (default 128).
            height: Widget height (default 72, matching the game-over panel).
            update_callback: Callback to trigger display updates.
        """
        super().__init__(x=x, y=y, width=width, height=height, update_callback=update_callback)
        self._title_widget = TextWidget(
            x=0, y=8, width=width, height=24, update_callback=self._handle_child_update,
            text="SETUP MODE", font_size=20,
            justify=Justify.CENTER, transparent=True, bold=True
        )
        self._subtitle_widget = TextWidget(
            x=0, y=40, width=width, height=20, update_callback=self._handle_child_update,
            text="Arrange pieces", font_size=14,
            justify=Justify.CENTER, transparent=True
        )

    def _handle_child_update(self, full: bool = False, immediate: bool = False):
        """Forward child widget update requests to this widget's callback."""
        return self._update_callback(full, immediate) if self._update_callback else None

    def render(self, sprite: Image.Image) -> None:
        """Render the setup status onto the sprite image.

        Args:
            sprite: Sprite-sized PIL image (0,0 is the widget's top-left).
        """
        self.draw_background_on_sprite(sprite)

        draw = ImageDraw.Draw(sprite)
        draw.rectangle([0, 0, self.width - 1, self.height - 1], outline=0)

        self._title_widget.draw_on(sprite, 0, 8)
        self._subtitle_widget.draw_on(sprite, 0, 40)
