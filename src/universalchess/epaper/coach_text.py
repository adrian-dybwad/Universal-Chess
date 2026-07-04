"""Coach statement widget shown in the chess-board area.

Occupies the same 128x128 region as the chess board (x=0, y=16). While a move is
selected in the analysis widget the board is hidden and this widget shows the
AI coach statement for that move; on the analysis view the board is restored and
this widget is hidden. Hidden by default so it never draws until a move is
selected.

Wrapping/centering is delegated to a child :class:`TextWidget` that fills the body
area; no title, underline, or outer border is drawn, so the statement gets the
maximum room in the board area below the status bar. The header label is still
tracked (tip vs. review) for the manager/tests but is no longer rendered.

Long statements are paginated: the wrapped lines are split into fixed-height
pages and only the current page's lines are shown. A footer at the bottom of the
panel reads "Page N of X" on the left and "Next" with a checkmark glyph on the
right, mirroring the physical OK (checkmark) button. Pressing OK while a coach
statement or hint tip is on screen calls :meth:`next_page`, which advances one
page and wraps back to the first page after the last one, instead of forcing a
full e-paper refresh.
"""

from PIL import Image, ImageDraw

from .framework.widget import Widget
from .text import TextWidget, Justify
from universalchess.resources import get_font

try:
    from universalchess.board.logging import log
except ImportError:  # pragma: no cover - logging shim for non-board contexts
    import logging
    log = logging.getLogger(__name__)


class CoachTextWidget(Widget):
    """Board-area panel that renders a wrapped, paged AI coach statement."""

    DEFAULT_HEADER = "Coach"

    # Height (px) reserved at the bottom of the panel for the paging footer
    # ("Page N of X" left, "Next" + checkmark right). The body area is shrunk by
    # this amount so the footer never overlaps the statement text.
    FOOTER_HEIGHT = 14
    # Footer text size; smaller than the body so the indicator stays unobtrusive.
    FOOTER_FONT_SIZE = 10

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
        # Paging state: the wrapped statement split into per-page text blocks, and
        # the zero-based index of the page currently shown.
        self._pages: list[str] = []
        self._page = 0
        self._footer_font = get_font(self.FOOTER_FONT_SIZE)
        # Child text widget draws the wrapped, centered statement body across the
        # full panel width (2px top inset), leaving FOOTER_HEIGHT at the bottom for
        # the paging footer. Its update callback is a no-op: set_text() is only
        # called from this widget, which already invalidates and requests one
        # refresh.
        self._body = TextWidget(
            0,
            2,
            self.width,
            self.height - 2 - self.FOOTER_HEIGHT,
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

    def _lines_per_page(self) -> int:
        """Number of wrapped body lines that fit on one page."""
        line_height = self._body.font_size + 2
        return max(1, self._body.height // line_height)

    def _paginate(self, text: str) -> None:
        """Split ``text`` into per-page blocks sized to the body area.

        Wrapping is delegated to the body :class:`TextWidget` so pagination uses
        the exact same line breaks the body will render. Each page is the newline
        join of a fixed number of consecutive wrapped lines; empty text yields no
        pages so the footer is suppressed.
        """
        if not text:
            self._pages = []
            return
        lines = self._body.wrap_lines(text)
        if not lines:
            self._pages = []
            return
        per_page = self._lines_per_page()
        self._pages = [
            "\n".join(lines[i:i + per_page])
            for i in range(0, len(lines), per_page)
        ]

    def _show_current_page(self) -> None:
        """Push the current page's text to the body widget."""
        page_text = self._pages[self._page] if self._pages else ""
        self._body.set_text(page_text)

    def set_text(self, text: str) -> None:
        """Set the coach statement, re-paginating and re-rendering on change.

        Resets to the first page so a new statement always starts at page 1.
        """
        if text == self._text:
            return
        self._text = text
        self._paginate(text)
        self._page = 0
        self._show_current_page()
        self.invalidate_and_update()

    def next_page(self) -> bool:
        """Advance to the next page, wrapping to the first page after the last.

        Returns True when a statement is present (so the OK/checkmark button
        paged instead of forcing a full refresh); False when there is nothing to
        page. A single-page statement stays on page 1 but still re-renders, so
        the button consistently performs a partial page refresh rather than a
        full-screen refresh while a coach statement or tip is shown.
        """
        if not self._pages:
            return False
        self._page = (self._page + 1) % len(self._pages)
        self._show_current_page()
        self.invalidate_and_update()
        return True

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

    @property
    def page_count(self) -> int:
        """Number of pages the current statement spans (0 when empty)."""
        return len(self._pages)

    @property
    def current_page(self) -> int:
        """The 1-based index of the page currently shown (0 when empty)."""
        return self._page + 1 if self._pages else 0

    def render(self, sprite: Image.Image) -> None:
        """Render the current page body plus the paging footer (no title/border)."""
        self.draw_background_on_sprite(sprite)
        # Full-width body (no left/right padding); small top inset only.
        self._body.draw_on(sprite, 0, 2)
        if self._pages:
            self._render_footer(sprite)

    def _render_footer(self, sprite: Image.Image) -> None:
        """Draw "Page N of X" (left) and "Next" + a checkmark glyph (right).

        The checkmark mirrors the physical OK button so the reader knows OK pages
        the statement. Drawn only when at least one page exists.
        """
        draw = ImageDraw.Draw(sprite)
        footer_top = self.height - self.FOOTER_HEIGHT
        text_y = footer_top + (self.FOOTER_HEIGHT - self.FOOTER_FONT_SIZE) // 2 - 1

        label = f"Page {self.current_page} of {self.page_count}"
        draw.text((0, text_y), label, font=self._footer_font, fill=0)

        # Checkmark glyph anchored at the right edge, "Next" to its left.
        center_y = footer_top + self.FOOTER_HEIGHT // 2
        check_right = self.width - 2
        check_left = check_right - 8
        vertex = (check_left + 3, center_y + 3)
        draw.line([(check_left, center_y), vertex], fill=0, width=2)
        draw.line([vertex, (check_right, center_y - 4)], fill=0, width=2)

        next_text = "Next"
        next_width = int(draw.textlength(next_text, font=self._footer_font))
        next_x = check_left - 4 - next_width
        draw.text((next_x, text_y), next_text, font=self._footer_font, fill=0)
