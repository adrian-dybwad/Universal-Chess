"""Full-screen modal showing a focused menu entry's help tip.

Shown when HELP is pressed in a menu, over the menu itself, and consumes the next
key: UP/DOWN turn the page when the tip needs more than one, and any other button
closes it. Paging is the same keys, and the same cycling, as the menu selection,
the keyboard layouts and the analysis pages.

The tip is rendered through :class:`~universalchess.epaper.paged_text.PagedTextWidget`
rather than as one tall block, because a block draws as many lines as the panel
holds and drops the rest without a mark: the USB Gadget Shared and Auto
descriptions are 25 and 23 wrapped lines against a panel that holds 13, so most
of each was invisible and nothing said so.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable

from PIL import Image, ImageDraw

from universalchess.resources import get_font

from .framework.widget import Widget
from .paged_text import NavigationHint, PagedTextWidget
from .text import Justify

if TYPE_CHECKING:
    from types import ModuleType

_board_module: ModuleType | None = None


def _get_board() -> ModuleType:
    """Lazily import and return the board module (for its Key constants).

    Deferred and cached exactly as ``icon_menu`` does it: importing the board at
    module import would pull in the serial stack, which this widget does not need
    to be constructed or rendered.
    """
    global _board_module  # noqa: PLW0603 - one-time cache of the lazily imported module
    if _board_module is None:
        from universalchess.board import board  # noqa: PLC0415 - deferred, see above
        _board_module = board
    return _board_module


class HelpDialogWidget(Widget):
    """Modal help tip, paged when it does not fit on one panel."""

    is_modal = True

    TITLE_Y = 24
    BODY_TOP_Y = 60
    INSTRUCTION_Y = 276
    SIDE_MARGIN = 8

    # Clearance between the bottom of the paged body and the instruction line.
    BODY_BOTTOM_GAP = 8

    TITLE_FONT_SIZE = 14
    BODY_FONT_SIZE = 12
    INSTRUCTION_FONT_SIZE = 10

    SINGLE_PAGE_INSTRUCTION = "Press any button"
    MULTI_PAGE_INSTRUCTION = "Any other button closes"

    # How long the dialog waits with no input before closing itself, so a board
    # left on a help screen returns to the menu. Measured from the last key, not
    # from when the dialog opened, or a long tip would close mid-read.
    IDLE_TIMEOUT_SECONDS = 30.0

    def __init__(self, update_callback: Callable[..., object], title: str, body: str,
                 background_shade: int = 0) -> None:
        """Initialize the dialog.

        Args:
            update_callback: Callback to trigger display updates. Must not be None.
            title: Short heading (the focused entry's label, newlines flattened).
            body: Help tip text; wrapped and paged to the panel.
            background_shade: Background shade 0-16 (0=white).

        """
        super().__init__(0, 0, 128, 296, update_callback, background_shade=background_shade)
        # Flatten newlines so a multi-line menu label reads as one title line.
        self._title = " ".join((title or "").split())
        self._dismissed = threading.Event()
        self._last_input = time.monotonic()
        self._pages = PagedTextWidget(
            self.SIDE_MARGIN,
            self.BODY_TOP_Y,
            self.width - 2 * self.SIDE_MARGIN,
            self.INSTRUCTION_Y - self.BODY_BOTTOM_GAP - self.BODY_TOP_Y,
            self._handle_child_update,
            text=body or "",
            font_size=self.BODY_FONT_SIZE,
            justify=Justify.LEFT,
            hint=NavigationHint.UP_DOWN,
            # The dialog's own instruction line already says how to get out, so a
            # "Page 1 of 1" under a tip that cannot be paged would only be noise.
            footer_on_single_page=False,
        )

    def _handle_child_update(self, *_args: object, **_kwargs: object) -> None:
        """Forward the paged body's refresh request as this widget's own.

        The child is not autonomous -- its text only changes from this widget's
        key handling -- but the page it draws has changed, so the panel does need
        a refresh, and the child's own cache invalidation does not reach this
        widget's cached sprite.
        """
        self.invalidate_and_update(immediate=True)

    # ------------------------------------------------------------------
    # Paging
    # ------------------------------------------------------------------

    @property
    def page_count(self) -> int:
        """Number of pages the tip spans."""
        return self._pages.page_count

    @property
    def current_page(self) -> int:
        """1-based index of the page showing."""
        return self._pages.current_page

    @property
    def page_text(self) -> str:
        """The text of the page showing."""
        return self._pages.page_text

    @property
    def lines_per_page(self) -> int:
        """How many wrapped lines the panel can draw at once."""
        return self._pages.lines_per_page

    @property
    def instruction(self) -> str:
        """The line at the foot of the panel, which depends on whether it pages."""
        if self.page_count > 1:
            return self.MULTI_PAGE_INSTRUCTION
        return self.SINGLE_PAGE_INSTRUCTION

    def next_page(self) -> bool:
        """Show the next page, cycling to the first after the last."""
        return self._pages.next_page(wrap=True)

    def previous_page(self) -> bool:
        """Show the previous page, cycling to the last from the first."""
        return self._pages.previous_page(wrap=True)

    def handle_key(self, key_id: object) -> bool:
        """Page on UP/DOWN, dismiss on anything else. Always consumes the key.

        Returns True in both cases: the dialog is modal, so the key must not
        reach the menu underneath whichever it was.
        """
        board = _get_board()
        if self.page_count > 1:
            if key_id == board.Key.UP:
                self.previous_page()
                self._last_input = time.monotonic()
                return True
            if key_id == board.Key.DOWN:
                self.next_page()
                self._last_input = time.monotonic()
                return True
        self.dismiss()
        return True

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self, sprite: Image.Image) -> None:
        """Render the title, the page showing, and the instruction line."""
        self.draw_background_on_sprite(sprite)
        draw = ImageDraw.Draw(sprite)

        if self._title:
            draw.text((64, self.TITLE_Y), self._title,
                      font=get_font(self.TITLE_FONT_SIZE), fill=0, anchor="mm")

        self._pages.draw_on(sprite, self.SIDE_MARGIN, self.BODY_TOP_Y)

        draw.text((64, self.INSTRUCTION_Y), self.instruction,
                  font=get_font(self.INSTRUCTION_FONT_SIZE), fill=0, anchor="mm")

    # ------------------------------------------------------------------
    # Dismissal
    # ------------------------------------------------------------------

    def dismiss(self) -> None:
        """Dismiss the dialog (called when the user presses a non-paging button)."""
        self._dismissed.set()

    def wait_for_dismiss(self, timeout: float = IDLE_TIMEOUT_SECONDS) -> bool:
        """Block until dismissed, or until ``timeout`` seconds without a key.

        Args:
            timeout: Idle seconds to allow. Each page turn restarts the window,
                so the limit is on the reader stopping, not on how long the tip
                takes to read.

        Returns:
            True if dismissed by the user, False once the idle window expires.

        """
        while True:
            if self._dismissed.is_set():
                return True
            remaining = timeout - (time.monotonic() - self._last_input)
            if remaining <= 0:
                return False
            if self._dismissed.wait(timeout=remaining):
                return True

    def stop(self) -> None:
        """Stop the widget and release any waiting threads."""
        self._dismissed.set()
        super().stop()
