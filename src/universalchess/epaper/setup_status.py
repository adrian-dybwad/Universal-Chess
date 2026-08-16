"""Setup-mode status widget.

Shown in place of the turn indicator while the Chessnut puzzle setup mode is
active. Communicates that move interpretation is suspended and the operator is
arranging pieces; the board widget above shows the position as it evolves.
"""

from PIL import Image, ImageDraw

from universalchess.i18n import t
from .framework.widget import Widget
from .text import TextWidget, Justify, Overflow
from .text_scale import DEFAULT_TEXT_SIZE, normalize_text_size, scale_font


class SetupStatusWidget(Widget):
    """Widget announcing that puzzle setup mode is active.

    Occupies the clock/turn-indicator region (y=144) so the turn indicator is
    hidden while setup is in progress, signalling that normal play is paused.
    """

    def __init__(self, x: int = 0, y: int = 144, width: int = 128, height: int = 72,
                 update_callback=None, text_size: str = DEFAULT_TEXT_SIZE):
        """Initialize the setup status widget.

        Args:
            x: X position (default 0).
            y: Y position (default 144, the turn-indicator region).
            width: Widget width (default 128).
            height: Widget height (default 72, matching the game-over panel).
            update_callback: Callback to trigger display updates.
            text_size: Display > Text Size name (small/medium/large).
        """
        super().__init__(x=x, y=y, width=width, height=height, update_callback=update_callback)
        text_size = normalize_text_size(text_size)
        title_font = scale_font(20, text_size)
        subtitle_font = scale_font(14, text_size)
        self._title_widget = TextWidget(
            x=0, y=8, width=width, height=title_font + 8,
            update_callback=self._handle_child_update,
            text=t("setup.mode_title"), font_size=title_font,
            justify=Justify.CENTER, transparent=True, bold=True,
            overflow=Overflow.FIT, min_font_size=12
        )
        self._subtitle_widget = TextWidget(
            x=0, y=8 + title_font + 10, width=width, height=subtitle_font + 6,
            update_callback=self._handle_child_update,
            text=t("setup.arrange_pieces"), font_size=subtitle_font,
            justify=Justify.CENTER, transparent=True,
            overflow=Overflow.FIT, min_font_size=8
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
        subtitle_y = 8 + self._title_widget.used_height() + 6
        self._subtitle_widget.draw_on(sprite, 0, subtitle_y)
