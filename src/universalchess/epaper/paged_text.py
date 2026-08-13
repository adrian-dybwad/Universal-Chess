"""Wrapped text split into pages that fit the panel, with a page footer.

A :class:`~universalchess.epaper.text.TextWidget` draws as many wrapped lines as
its height allows and discards the rest without complaint, so any text that can
be long needs paging rather than one tall block. This view owns that: it wraps
through the same TextWidget that will render the text, splits the lines into
pages sized to the panel, tracks which page is showing, and draws a "Page N of X"
footer with the button that turns it.

Two panels page text -- the coach statement in the board area (OK advances,
cycling) and the menu help dialog (UP/DOWN, stopping at the ends) -- so the
cursor reports whether it moved and takes wrapping as an argument instead of
deciding for both.

The body area is the panel minus :attr:`PagedTextWidget.FOOTER_HEIGHT`, and pages
are measured against that, which is what keeps a page off the footer. Line height
is TextWidget's own ``font_size + 2``: the pages are measured for the widget that
renders them, so any other spacing would leave a page a line short or a line
half-drawn.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable

from PIL import Image, ImageDraw

from universalchess.resources import get_font

from .framework.widget import Widget
from .text import Justify, TextWidget


def ignore_updates(*_args: object, **_kwargs: object) -> None:
    """Update callback for a paged view whose owner requests its own refresh.

    A paged view is not autonomous: its text and page only change through the
    widget that owns it, which invalidates itself in the same call. An owner that
    needs no more than that passes this, rather than a callback that would ask
    the display manager for a second refresh of the same change.
    """


class NavigationHint(Enum):
    """Which button the footer tells the reader to press to turn the page.

    ``OK_NEXT`` draws "Next" and a checkmark, mirroring the physical OK button.
    ``UP_DOWN`` draws arrows for the two keys that page everywhere else on the
    board (the menu selection, the analysis pages). ``NONE`` leaves the right of
    the footer empty, for an owner that explains paging elsewhere.
    """

    NONE = "none"
    OK_NEXT = "ok_next"
    UP_DOWN = "up_down"


class PagedTextWidget(Widget):
    """Panel showing one page of wrapped text at a time."""

    # Height (px) reserved at the bottom for the footer. The body area is the
    # panel minus this, so text is measured for the room it actually has.
    FOOTER_HEIGHT = 14

    # Fixed, not scaled with the body: the indicator stays a consistent size
    # whatever the text size is set to.
    FOOTER_FONT_SIZE = 10

    # Room the hint takes on the right of the footer, so the page label can be
    # measured against what is left.
    HINT_WIDTH = 34

    DEFAULT_FONT_SIZE = 12

    def __init__(self, x: int, y: int, width: int, height: int,  # noqa: PLR0913 - a widget's geometry (4) plus its refresh callback is the framework's own constructor shape; the rest are keyword-only options
                 update_callback: Callable[..., object],
                 *,
                 text: str = "", font_size: int = DEFAULT_FONT_SIZE,
                 justify: Justify = Justify.LEFT,
                 hint: NavigationHint = NavigationHint.NONE,
                 footer_on_single_page: bool = True) -> None:
        """Initialize the paged view.

        Args:
            x: X position on the display.
            y: Y position on the display.
            width: Panel width in pixels.
            height: Panel height in pixels, footer included.
            update_callback: Callback to trigger display updates. Must not be
                None; pass :func:`ignore_updates` when the owner refreshes itself.
            text: Initial text; may be empty, which is no pages rather than one.
            font_size: Body font size in points. Also sets the line height, and
                so how many lines a page holds.
            justify: Body justification, per TextWidget.
            hint: Which button the footer names as the one that pages.
            footer_on_single_page: Whether to draw the footer for text that fits
                on one page. False for an owner whose own footer already says how
                to get out of the panel; the footer is drawn regardless once there
                is more than one page, since that is the reader's only sign that
                the text continues.

        """
        super().__init__(x, y, width, height, update_callback)
        self._hint = hint
        self._footer_on_single_page = footer_on_single_page
        self._footer_font = get_font(self.FOOTER_FONT_SIZE)
        self._pages: list[str] = []
        self._page = 0
        # The child both wraps and draws, so pagination uses the exact line
        # breaks that will appear. It never asks for a refresh of its own: its
        # text only changes from here, which requests the one refresh needed.
        self._body = TextWidget(
            0, 0, width, height - self.FOOTER_HEIGHT,
            ignore_updates,
            text="",
            font_size=font_size,
            wrapText=True,
            justify=justify,
            transparent=True,
        )
        self._text = ""
        if text:
            self.set_text(text)

    # ------------------------------------------------------------------
    # Text and pagination
    # ------------------------------------------------------------------

    @property
    def text(self) -> str:
        """The full text, across all pages."""
        return self._text

    @property
    def lines_per_page(self) -> int:
        """How many wrapped lines fit in the body area."""
        line_height = self._body.font_size + 2
        return max(1, self._body.height // line_height)

    @property
    def page_count(self) -> int:
        """Number of pages the text spans (0 when there is no text)."""
        return len(self._pages)

    @property
    def current_page(self) -> int:
        """1-based index of the page showing (0 when there is no text)."""
        return self._page + 1 if self._pages else 0

    @property
    def page_text(self) -> str:
        """The text of the page showing ("" when there is no text)."""
        return self._pages[self._page] if self._pages else ""

    @property
    def footer_label(self) -> str:
        """The footer's page indicator ("" when there is no text)."""
        if not self._pages:
            return ""
        return f"Page {self.current_page} of {self.page_count}"

    def set_text(self, text: str) -> None:
        """Show ``text`` from its first page, re-paginating if it changed."""
        if text == self._text:
            return
        self._text = text
        self._pages = self._paginate(text)
        self._page = 0
        self._body.set_text(self.page_text)
        self.invalidate_and_update()

    def _paginate(self, text: str) -> list[str]:
        """Split ``text`` into pages of at most :attr:`lines_per_page` lines."""
        if not text:
            return []
        lines = self._body.wrap_lines(text)
        if not lines:
            return []
        per_page = self.lines_per_page
        return [
            "\n".join(lines[start:start + per_page])
            for start in range(0, len(lines), per_page)
        ]

    # ------------------------------------------------------------------
    # Cursor
    # ------------------------------------------------------------------

    def next_page(self, *, wrap: bool = False) -> bool:
        """Show the next page. Returns whether the view moved."""
        return self._move(1, wrap=wrap)

    def previous_page(self, *, wrap: bool = False) -> bool:
        """Show the previous page. Returns whether the view moved."""
        return self._move(-1, wrap=wrap)

    def _move(self, step: int, *, wrap: bool) -> bool:
        """Move the cursor by ``step``, wrapping only when asked to."""
        if not self._pages:
            return False
        target = self._page + step
        if not 0 <= target < len(self._pages):
            if not wrap:
                return False
            target %= len(self._pages)
        if target == self._page:
            return False
        self._page = target
        self._body.set_text(self.page_text)
        self.invalidate_and_update()
        return True

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self, sprite: Image.Image) -> None:
        """Draw the page showing, and the footer when there is one to draw."""
        self.draw_background_on_sprite(sprite)
        self._body.draw_on(sprite, 0, 0)
        if self._shows_footer():
            self._render_footer(sprite)

    def _shows_footer(self) -> bool:
        """Whether the footer is drawn for the text currently held."""
        if not self._pages:
            return False
        return self._footer_on_single_page or len(self._pages) > 1

    def _render_footer(self, sprite: Image.Image) -> None:
        """Draw the page indicator, and the paging hint on the right."""
        draw = ImageDraw.Draw(sprite)
        footer_top = self.height - self.FOOTER_HEIGHT
        text_y = footer_top + (self.FOOTER_HEIGHT - self.FOOTER_FONT_SIZE) // 2 - 1

        draw.text((0, text_y), self.footer_label, font=self._footer_font, fill=0)

        center_y = footer_top + self.FOOTER_HEIGHT // 2
        if self._hint is NavigationHint.OK_NEXT:
            self._draw_ok_next(draw, center_y, text_y)
        elif self._hint is NavigationHint.UP_DOWN:
            self._draw_up_down(draw, center_y)

    def _draw_ok_next(self, draw: ImageDraw.ImageDraw, center_y: int, text_y: int) -> None:
        """Draw "Next" and a checkmark glyph, mirroring the physical OK button."""
        check_right = self.width - 2
        check_left = check_right - 8
        vertex = (check_left + 3, center_y + 3)
        draw.line([(check_left, center_y), vertex], fill=0, width=2)
        draw.line([vertex, (check_right, center_y - 4)], fill=0, width=2)

        label = "Next"
        label_width = int(draw.textlength(label, font=self._footer_font))
        draw.text((check_left - 4 - label_width, text_y), label, font=self._footer_font, fill=0)

    def _draw_up_down(self, draw: ImageDraw.ImageDraw, center_y: int) -> None:
        """Draw both arrows for the keys that page everywhere else on the board.

        Both are always drawn, because the owners of this hint page cyclically as
        the menu and the analysis view do, so neither key is ever a dead end.
        """
        right = self.width - 2
        half = 4
        self._draw_triangle(draw, right - 3 * half - 2, center_y, up=True)
        self._draw_triangle(draw, right - half, center_y, up=False)

    @staticmethod
    def _draw_triangle(draw: ImageDraw.ImageDraw, center_x: int, center_y: int, *,
                       up: bool) -> None:
        """Draw a small filled triangle pointing up or down at ``center_x``."""
        half = 4
        tip_y = center_y - half if up else center_y + half
        base_y = center_y + half if up else center_y - half
        draw.polygon(
            [(center_x - half, base_y), (center_x + half, base_y), (center_x, tip_y)],
            fill=0,
        )

    @property
    def font_size(self) -> int:
        """Body font size, which is also what sets the lines per page."""
        return self._body.font_size
