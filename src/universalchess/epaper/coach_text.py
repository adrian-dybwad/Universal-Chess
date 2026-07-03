"""Coach statement widget shown in the chess-board area.

Occupies the same 128x128 region as the chess board (x=0, y=16). While a move is
selected in the analysis widget the board is hidden and this widget shows the
AI coach statement for that move; on the analysis view the board is restored and
this widget is hidden. Hidden by default so it never draws until a move is
selected.

Wrapping/centering is delegated to a child :class:`TextWidget`; this widget adds
the surrounding border and the "Coach" header so the statement reads as its own
panel rather than floating over an empty square.
"""

from PIL import Image, ImageDraw

from .framework.widget import Widget
from .text import TextWidget, Justify

try:
    from universalchess.board.logging import log
except ImportError:  # pragma: no cover - logging shim for non-board contexts
    import logging
    log = logging.getLogger(__name__)


class CoachTextWidget(Widget):
    """Board-area panel that renders a wrapped AI coach statement."""

    HEADER_HEIGHT = 16
    DEFAULT_HEADER = "Coach"

    def __init__(self, x: int, y: int, width: int, height: int, update_callback):
        """Initialize the coach-text widget (hidden until a move is selected)."""
        super().__init__(x, y, width, height, update_callback)
        # Hidden by default: it only appears while a move is selected. Set the
        # flag directly (not via hide()) so no refresh is requested before the
        # widget is even added to the manager.
        self.visible = False
        self._text = ""
        # The header labels the panel's purpose ("Coach" for move review, "Coach's
        # Tip" for a hint). Kept mutable so the same panel can distinguish the two
        # without a second widget.
        self._header = self.DEFAULT_HEADER
        # Child text widget draws the wrapped, centered statement body. Its
        # update callback is a no-op: set_text() is only called from this
        # widget's set_text(), which already invalidates and requests one refresh.
        self._body = TextWidget(
            2,
            self.HEADER_HEIGHT + 2,
            self.width - 4,
            self.height - self.HEADER_HEIGHT - 4,
            self._noop_update,
            text="",
            font_size=12,
            wrapText=True,
            justify=Justify.CENTER,
            transparent=True,
        )

    @staticmethod
    def _noop_update(full: bool = False, immediate: bool = False):
        """No-op update callback for the render-only body text widget."""
        return None

    def set_text(self, text: str) -> None:
        """Set the coach statement, re-rendering only when it changed."""
        if text == self._text:
            return
        self._text = text
        self._body.set_text(text)
        self.invalidate_and_update()

    def set_header(self, header: str) -> None:
        """Set the panel header label, re-rendering only when it changed."""
        if header == self._header:
            return
        self._header = header
        self.invalidate_and_update()

    @property
    def text(self) -> str:
        """The currently displayed coach statement."""
        return self._text

    @property
    def header(self) -> str:
        """The current panel header label."""
        return self._header

    def render(self, sprite: Image.Image) -> None:
        """Render the border, header, and wrapped statement body."""
        draw = ImageDraw.Draw(sprite)
        self.draw_background_on_sprite(sprite)
        draw.rectangle([(0, 0), (self.width - 1, self.height - 1)], fill=None, outline=0)

        from universalchess.resources import get_font

        font = get_font(12)
        draw.text((4, 2), self._header, font=font, fill=0)
        draw.line([(2, self.HEADER_HEIGHT), (self.width - 2, self.HEADER_HEIGHT)], fill=0, width=1)

        # Vertically center the wrapped body within the area below the header.
        self._body.draw_on(sprite, 2, self.HEADER_HEIGHT + 2)
