"""Coach statement widget shown in the chess-board area.

Occupies the same 128x128 region as the chess board (x=0, y=16). While a move is
selected in the move-list widget the board is hidden and this widget shows the
AI coach statement for that move; on the analysis view the board is restored and
this widget is hidden. Hidden by default so it never draws until a move is
selected.

The statement is rendered through
:class:`~universalchess.epaper.paged_text.PagedTextWidget`, which wraps it,
splits it into pages that fit the panel and draws the "Page N of X" footer with
"Next" and a checkmark glyph -- the checkmark mirroring the physical OK button.
Pressing OK while a coach statement or hint tip is on screen calls
:meth:`next_page`, which advances one page and wraps back to the first after the
last, instead of forcing a full e-paper refresh.

No title, underline, or outer border is drawn, so the statement gets the maximum
room in the board area below the status bar. The header label is still tracked
(tip vs. review) for the manager/tests but is no longer rendered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .framework.widget import Widget
from .paged_text import NavigationHint, PagedTextWidget, ignore_updates
from .text import Justify
from .text_scale import DEFAULT_TEXT_SIZE, scale_font

if TYPE_CHECKING:
    from PIL import Image


class CoachTextWidget(Widget):
    """Board-area panel that renders a wrapped, paged AI coach statement."""

    DEFAULT_HEADER = "Coach"

    # Base (medium) body font size; scaled by the Display > Text Size setting.
    BODY_FONT_SIZE = 12

    # Top inset above the statement; the panel has no title, so this is only
    # breathing room under the status bar.
    BODY_TOP_INSET = 2

    # The paging footer's height, which is the paged view's own. Named here too
    # because the panel's layout is described in terms of it.
    FOOTER_HEIGHT = PagedTextWidget.FOOTER_HEIGHT

    def __init__(self, x: int, y: int, width: int, height: int,  # noqa: PLR0913 - the framework's widget constructor shape (geometry plus refresh callback), with one option
                 update_callback: Callable[..., object],
                 text_size: str = DEFAULT_TEXT_SIZE) -> None:
        """Initialize the coach-text widget (hidden until a move is selected).

        Args:
            x: X position on the display.
            y: Y position on the display.
            width: Panel width in pixels.
            height: Panel height in pixels.
            update_callback: Callback to trigger display updates. Must not be None.
            text_size: Display text-size name (small/medium/large) that scales the
                statement body font. The paging footer stays a fixed size.

        """
        super().__init__(x, y, width, height, update_callback)
        # Hidden by default: it only appears while a move is selected. Set the
        # flag directly (not via hide()) so no refresh is requested before the
        # widget is even added to the manager.
        self.visible = False
        # The header labels the panel's purpose ("Coach" for move review, "Coach's
        # Tip" for a hint). Retained as state (read by the manager and tests) but no
        # longer drawn, so the body gets the full panel.
        self._header = self.DEFAULT_HEADER
        # Full-width paged body. It asks for no refresh of its own: set_text and
        # the paging are only ever driven from this widget, which requests the one
        # refresh needed.
        self._pages = PagedTextWidget(
            0,
            self.BODY_TOP_INSET,
            self.width,
            self.height - self.BODY_TOP_INSET,
            ignore_updates,
            font_size=scale_font(self.BODY_FONT_SIZE, text_size),
            justify=Justify.CENTER,
            hint=NavigationHint.OK_NEXT,
        )

    def set_text(self, text: str) -> None:
        """Set the coach statement, re-paginating and re-rendering on change.

        Resets to the first page so a new statement always starts at page 1.
        """
        if text == self.text:
            return
        self._pages.set_text(text)
        self.invalidate_and_update()

    def next_page(self) -> bool:
        """Advance to the next page, wrapping to the first page after the last.

        Returns True when a statement is present (so the OK/checkmark button
        paged instead of forcing a full refresh); False when there is nothing to
        page. A single-page statement stays on page 1 but still re-renders, so
        the button consistently performs a partial page refresh rather than a
        full-screen refresh while a coach statement or tip is shown.
        """
        if self.page_count == 0:
            return False
        self._pages.next_page(wrap=True)
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
        return self._pages.text

    @property
    def header(self) -> str:
        """The current panel header label."""
        return self._header

    @property
    def page_count(self) -> int:
        """Number of pages the current statement spans (0 when empty)."""
        return self._pages.page_count

    @property
    def current_page(self) -> int:
        """The 1-based index of the page currently shown (0 when empty)."""
        return self._pages.current_page

    @property
    def page_text(self) -> str:
        """The text of the page currently shown ("" when empty)."""
        return self._pages.page_text

    @property
    def lines_per_page(self) -> int:
        """How many wrapped statement lines fit on one page."""
        return self._pages.lines_per_page

    @property
    def font_size(self) -> int:
        """The scaled body font size in use."""
        return self._pages.font_size

    def render(self, sprite: Image.Image) -> None:
        """Render the current page and its paging footer (no title/border)."""
        self.draw_background_on_sprite(sprite)
        self._pages.draw_on(sprite, 0, self.BODY_TOP_INSET)
