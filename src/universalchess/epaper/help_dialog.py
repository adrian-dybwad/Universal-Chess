"""Help dialog widget for the e-paper board.

Shows the help tip for the focused menu entry when the user presses the HELP
("?") key in a menu. The tip text is sourced from the shared menu catalog, so
the board and web UI present the same guidance. This is a full-screen modal that
blocks until dismissed by any key (mirroring AboutWidget's dismiss model).
"""

import threading
from typing import Optional

from PIL import Image, ImageDraw

from .framework.widget import Widget

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


class HelpDialogWidget(Widget):
    """Full-screen modal showing a focused menu entry's help tip.

    Args:
        update_callback: Callback to trigger display updates. Must not be None.
        title: Short heading (the focused entry's label, newlines flattened).
        body: Help tip text; word-wrapped to the display width.
        background_shade: Background shade 0-16 (0=white).
    """

    is_modal = True

    TITLE_Y = 24
    BODY_TOP_Y = 60
    INSTRUCTION_Y = 276
    LINE_SPACING = 4
    SIDE_MARGIN = 8

    def __init__(self, update_callback, title: str, body: str,
                 background_shade: int = 0):
        super().__init__(0, 0, 128, 296, update_callback, background_shade=background_shade)
        # Flatten newlines so a multi-line menu label reads as one title line.
        self._title = " ".join((title or "").split())
        self._body = body or ""
        self._dismissed = threading.Event()
        self._font_loader = None

    def _get_font_loader(self):
        if self._font_loader is None:
            from universalchess.resources import ResourceLoader
            self._font_loader = ResourceLoader(
                "/opt/universalchess/resources", "/home/pi/resources")
        return self._font_loader

    def _wrap(self, draw: ImageDraw.ImageDraw, text: str, font, max_width: int):
        """Word-wrap ``text`` to ``max_width`` pixels, returning a list of lines.

        Falls back to character-level breaking for any single word wider than
        the available width so a long token (e.g. a command) cannot overflow.
        """
        lines = []
        for paragraph in text.split("\n"):
            words = paragraph.split()
            if not words:
                lines.append("")
                continue
            current = words[0]
            for word in words[1:]:
                candidate = f"{current} {word}"
                if draw.textlength(candidate, font=font) <= max_width:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
            lines.append(current)
        return lines

    def render(self, sprite: Image.Image) -> None:
        """Render the help dialog onto the sprite."""
        self.draw_background_on_sprite(sprite)
        draw = ImageDraw.Draw(sprite)
        loader = self._get_font_loader()

        title_font = loader.get_font(14)
        body_font = loader.get_font(12)
        instr_font = loader.get_font(10)

        if self._title:
            draw.text((64, self.TITLE_Y), self._title,
                      font=title_font, fill=0, anchor="mm")

        max_width = self.width - 2 * self.SIDE_MARGIN
        lines = self._wrap(draw, self._body, body_font, max_width)
        y = self.BODY_TOP_Y
        line_height = 12 + self.LINE_SPACING
        for line in lines:
            draw.text((self.SIDE_MARGIN, y), line, font=body_font, fill=0)
            y += line_height

        draw.text((64, self.INSTRUCTION_Y), "Press any button",
                  font=instr_font, fill=0, anchor="mm")

    def dismiss(self) -> None:
        """Dismiss the dialog (called when the user presses any button)."""
        self._dismissed.set()

    def wait_for_dismiss(self, timeout: float = 30.0) -> bool:
        """Block until dismissed or the timeout expires.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            True if dismissed by the user, False on timeout.
        """
        return self._dismissed.wait(timeout=timeout)

    def stop(self) -> None:
        """Stop the widget and release any waiting threads."""
        self._dismissed.set()
        super().stop()
