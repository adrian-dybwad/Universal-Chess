"""Coach statement widget shown in the chess-board area.

Occupies the same 128x128 region as the chess board (x=0, y=16). While a move is
selected in the analysis widget the board is hidden and this widget shows the
AI coach statement for that move; on the analysis view the board is restored and
this widget is hidden. Hidden by default so it never draws until a move is
selected.

Wrapping/centering is delegated to a child :class:`TextWidget` that fills the whole
panel; no title, underline, or outer border is drawn, so the statement gets the
maximum room in the board area below the status bar. The header label is still
tracked (tip vs. review) for the manager/tests but is no longer rendered.
"""

from PIL import Image

from .framework.widget import Widget
from .text import TextWidget, Justify

try:
    from universalchess.board.logging import log
except ImportError:  # pragma: no cover - logging shim for non-board contexts
    import logging
    log = logging.getLogger(__name__)


class CoachTextWidget(Widget):
    """Board-area panel that renders a wrapped AI coach statement."""

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
        # Tip" for a hint). Retained as state (read by the manager and tests) but no
        # longer drawn, so the body gets the full panel.
        self._header = self.DEFAULT_HEADER
        # Child text widget draws the wrapped, centered statement body across the
        # full panel width (2px top/bottom inset only). Its update callback is a
        # no-op: set_text()
        # is only called from this widget's set_text(), which already invalidates and
        # requests one refresh.
        self._body = TextWidget(
            0,
            2,
            self.width,
            self.height - 4,
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
        """Render the wrapped statement body filling the panel (no title/border)."""
        self.draw_background_on_sprite(sprite)
        # Full-width body (no left/right padding); small top inset only.
        self._body.draw_on(sprite, 0, 2)
