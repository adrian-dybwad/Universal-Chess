"""
Icon button widget for e-paper display.

A single button with an icon and label, designed for large touch-friendly
button menus on the small e-paper display.
"""

from PIL import Image, ImageDraw, ImageFont
from .framework.widget import Widget, DITHER_PATTERNS
from .text import TextWidget, Justify
from typing import Optional, Tuple, Dict
import math

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


# Module-level knight logo cache by size, set by application at startup
_knight_logos: Dict[int, Tuple[Image.Image, Image.Image]] = {}


def set_knight_logo(size: int, logo: Image.Image, mask: Image.Image) -> None:
    """Set a knight logo at a specific size.
    
    Called at application startup to provide pre-sized logos for icons.
    
    Args:
        size: The size (width/height) of this logo
        logo: PIL Image of the knight logo at this size
        mask: PIL Image mask for transparency
    """
    _knight_logos[size] = (logo, mask)


class IconButtonWidget(Widget):
    """Widget displaying a single button with icon and label.
    
    The button can be in selected (inverted colors) or unselected state.
    Icons are drawn using PIL primitives for e-paper compatibility.
    
    Supports two layout modes:
    - horizontal: Icon on left, text on right (default, compact)
    - vertical: Icon centered on top, text centered below (for large buttons)
    
    Attributes:
        key: Unique identifier for this button
        label: Display text
        icon_name: Icon identifier for rendering
        selected: Whether button is currently selected (inverted)
        layout: Layout mode - 'horizontal' or 'vertical'
    """
    
    def __init__(self, x: int, y: int, width: int, height: int, update_callback,
                 key: str, label: str, icon_name: str,
                 selected: bool = False,
                 icon_size: int = 36,
                 label_height: int = 18,
                 margin: int = 4,
                 padding: int = 2,
                 icon_margin: int = 2,
                 border_width: int = 2,
                 selected_shade: int = 12,
                 background_shade: int = 0,
                 layout: str = "horizontal",
                 font_size: int = 16,
                 bold: bool = False,
                 description: str = None,
                 description_font_size: int = 11,
                 icon_image: Image.Image = None,
                 icon_mask: Image.Image = None,
                 trailing_icon_name: str = None):
        """Initialize icon button widget.
        
        Args:
            x: X position of widget
            y: Y position of widget
            width: Widget width
            height: Widget height
            update_callback: Callback to trigger display updates. Must not be None.
            key: Unique identifier returned on selection
            label: Display text
            icon_name: Icon identifier for rendering
            selected: Initial selection state
            icon_size: Icon size in pixels (default 36)
            label_height: Height reserved for label text (default 18)
            margin: Space outside the button border (default 4)
            padding: Space inside the button border (default 2)
            icon_margin: Space around the icon on all sides (default 2)
            border_width: Width of the button border in pixels (default 2)
            selected_shade: Dithered shade for selected state 0-16 (default 12 = ~75% black)
            background_shade: Dithered background shade 0-16 (default 0 = white)
            layout: Layout mode - 'horizontal' (icon left, text right) or 
                   'vertical' (icon top centered, text bottom centered)
            font_size: Font size in pixels (default 16)
            bold: Whether to render text in bold (simulated via multi-draw)
            description: Optional long description text rendered below icon+label
            description_font_size: Font size for description text (default 11)
            icon_image: Optional pre-rendered image used as the main icon instead
                       of a drawn icon_name (composited via icon_mask)
            icon_mask: Optional transparency mask for icon_image (opaque where the
                       image should show)
            trailing_icon_name: Optional icon drawn at the right edge (e.g. a
                       radio indicator) in addition to the main icon
        """
        super().__init__(x, y, width, height, update_callback, background_shade=background_shade)
        self.key = key
        self.label = label
        self.icon_name = icon_name
        self.icon_image = icon_image
        self.icon_mask = icon_mask
        self.trailing_icon_name = trailing_icon_name
        self.selected = selected
        self.icon_size = icon_size
        self.label_height = label_height
        self.margin = margin
        self.padding = padding
        self.icon_margin = icon_margin
        self.border_width = border_width
        self.selected_shade = max(0, min(16, selected_shade))
        self.layout = layout
        self.font_size = font_size
        self.bold = bold
        self.description = description
        self.description_font_size = description_font_size
        
        # TextWidgets for label rendering - cached by (width, centered, bold, font_size) key
        # Text content is set dynamically via set_text()
        self._text_widgets_cache = {}
    
    def _handle_child_update(self, full: bool = False, immediate: bool = False):
        """No-op update callback for the render-only cached text widgets.

        The cached TextWidgets are not autonomous: their set_text() is called only
        from _render_text_line() during this button's own render(), which then
        draws the text. TextWidget.set_text() calls request_update() on a change;
        forwarding that to the Manager fired a re-entrant second display refresh
        (Manager defers and replays it) on every button repaint -- so a single
        menu redraw could double its refreshes. Returning None keeps set_text()'s
        cache invalidation while suppressing the redundant refresh; selection and
        other real changes drive refreshes via the owning menu, not these helpers.
        """
        return None
    
    def _get_cached_text_widget(self, width: int, centered: bool, font_size: int = None, bold: bool = None) -> TextWidget:
        """Get or create a cached TextWidget for the given parameters.
        
        Args:
            width: Width for text widget
            centered: If True, center-justify the text
            font_size: Font size to use (defaults to self.font_size)
            bold: Whether to use bold (defaults to self.bold)
            
        Returns:
            Cached TextWidget instance
        """
        if font_size is None:
            font_size = self.font_size
        if bold is None:
            bold = self.bold
        
        cache_key = (width, centered, bold, font_size)
        
        if cache_key not in self._text_widgets_cache:
            line_height = font_size + 4
            widget = TextWidget(
                0, 0, width, line_height, self._handle_child_update,
                text="", font_size=font_size,
                justify=Justify.CENTER if centered else Justify.LEFT,
                transparent=True, bold=bold
            )
            self._text_widgets_cache[cache_key] = widget
        
        return self._text_widgets_cache[cache_key]
    
    def _render_text_line(self, img: Image.Image, text: str, x: int, y: int, 
                          width: int, text_color: int, centered: bool = False,
                          font_size: int = None, bold: bool = None):
        """Render a line of text directly onto the target image.
        
        Uses TextWidget.draw_on() to avoid creating intermediate images.
        
        Args:
            img: Target image to draw text onto
            text: Text to render
            x: X position for text
            y: Y position for text
            width: Width for text widget (used for justification)
            text_color: Text color (0=black, 255=white)
            centered: If True, center-justify the text
            font_size: Font size to use (defaults to self.font_size)
            bold: Whether to use bold (defaults to self.bold)
        """
        widget = self._get_cached_text_widget(width, centered, font_size, bold)
        widget.set_text(text)
        widget.draw_on(img, x, y, text_color=text_color)
    
    def set_selected(self, selected: bool) -> None:
        """Set the selection state.

        Only invalidates cache - does NOT request update. The parent widget
        (e.g., IconMenuWidget) is responsible for calling request_update()
        after all selection state changes are complete. This prevents multiple
        redundant display refreshes when selection moves between buttons.

        Args:
            selected: New selection state
        """
        if selected != self.selected:
            self.selected = selected
            self.invalidate_cache()
    
    def _apply_dither_pattern(self, img: Image.Image, shade: int) -> None:
        """Apply a dither pattern to an image.
        
        Uses an 8x8 Bayer matrix for ordered dithering.
        
        Args:
            img: Image to modify in place
            shade: Shade level 0-16 (0=white, 16=black)
        """
        pattern = DITHER_PATTERNS.get(shade, DITHER_PATTERNS[0])
        pattern_size = len(pattern)  # 8 for Bayer
        pixels = img.load()
        for y in range(img.height):
            pattern_row = pattern[y % pattern_size]
            for x in range(img.width):
                if pattern_row[x % pattern_size] == 1:
                    pixels[x, y] = 0  # Black pixel
    
    def _wrap_description(self, text: str, max_chars_per_line: int = 24) -> list:
        """Word-wrap description text.
        
        Args:
            text: Text to wrap
            max_chars_per_line: Maximum characters per line
            
        Returns:
            List of wrapped lines
        """
        if not text:
            return []
        
        words = text.split()
        lines = []
        current_line = ""
        
        for word in words:
            if len(current_line) + len(word) + 1 <= max_chars_per_line:
                current_line = f"{current_line} {word}".strip()
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        
        if current_line:
            lines.append(current_line)
        
        return lines
    
    def render(self, sprite: Image.Image) -> None:
        """Render the button with icon and label onto the sprite.
        
        Supports two layout modes:
        - horizontal: Icon on left, text on right (default)
        - vertical: Icon centered on top, text centered below
        
        Multi-line labels are supported in both modes.
        
        If a description is provided, it renders as word-wrapped text below
        the icon+label area, spanning the full content width.
        
        Layout:
            - margin: transparent space outside the border
            - border: 2px black line
            - padding: space inside the border (between border and content)
        """
        draw = ImageDraw.Draw(sprite)
        
        # Calculate border rectangle (inset by margin)
        border_left = self.margin
        border_top = self.margin
        border_right = self.width - 1 - self.margin
        border_bottom = self.height - 1 - self.margin
        
        # Inside edge of border (where content can start)
        inside_left = self.margin + self.border_width
        inside_top = self.margin + self.border_width
        inside_right = self.width - self.margin - self.border_width
        inside_bottom = self.height - self.margin - self.border_width
        
        # Content area bounds (inside border and padding)
        content_left = inside_left + self.padding
        content_top = inside_top + self.padding
        content_right = inside_right - self.padding
        content_bottom = inside_bottom - self.padding
        content_width = content_right - content_left
        content_height = content_bottom - content_top
        
        # Draw button background (only inside border area, not margin)
        if self.selected:
            # Selected: dark grey dithered background inside border only
            pattern = DITHER_PATTERNS.get(self.selected_shade, DITHER_PATTERNS[0])
            pattern_size = len(pattern)  # 8 for Bayer
            for y in range(border_top, border_bottom + 1):
                for x in range(border_left, border_right + 1):
                    if pattern[y % pattern_size][x % pattern_size] == 1:
                        sprite.putpixel((x, y), 0)
                    else:
                        sprite.putpixel((x, y), 255)
            # Draw border outline
            draw.rectangle([border_left, border_top, border_right, border_bottom], fill=None, outline=0)
        else:
            # Unselected: white fill with black border
            draw.rectangle([border_left, border_top, border_right, border_bottom], fill=255, outline=0, width=self.border_width)
        
        text_color = 255 if self.selected else 0
        lines = self.label.split('\n')
        line_height = self.font_size + 2  # Add small spacing between lines
        
        # Calculate description area if present
        description_lines = []
        description_height = 0
        description_line_height = self.description_font_size + 2

        # A vertical button carrying both a trailing icon and a description
        # renders an enable-state footer instead of the word-wrapped block: a
        # centered "[checkbox] Enabled/Disabled" line at the bottom that signals
        # the row is a toggle (the WiFi/Bluetooth merged status button). Every
        # other description keeps the existing multi-line readout.
        state_footer = (self.layout == "vertical"
                        and self.trailing_icon_name is not None
                        and self.description is not None)
        footer_height = 0

        if state_footer:
            footer_height = max(self.description_font_size + 6, description_line_height) + 4
        elif self.description:
            # Word-wrap description to fit content width
            # At font size 10-11, average character width is ~5-6 pixels
            # Use font_size * 0.5 as approximate char width for proportional fonts
            avg_char_width = max(4, self.description_font_size * 0.5)
            chars_per_line = max(20, int(content_width / avg_char_width))
            description_lines = self._wrap_description(self.description, chars_per_line)
            description_height = len(description_lines) * description_line_height + 4  # +4 for gap
        
        # Reduce content height for icon+label if a description/footer is present
        icon_label_height = content_height - description_height - footer_height
        
        if self.layout == "vertical":
            # Vertical layout: icon centered on top, text centered below
            self._render_vertical_layout(
                sprite, draw, content_left, content_top, content_width, icon_label_height,
                lines, line_height, text_color
            )
        else:
            # Horizontal layout: icon on left, text on right (default)
            self._render_horizontal_layout(
                sprite, draw, inside_left, inside_top, content_top, icon_label_height,
                lines, line_height, text_color
            )
        
        if state_footer:
            self._render_state_footer(
                sprite, draw, content_left, content_top + icon_label_height,
                content_width, footer_height, text_color
            )
        elif description_lines:
            # Render description below icon+label area
            desc_start_y = content_top + icon_label_height + 4  # 4px gap
            desc_left_margin = 4  # Small left margin to prevent cutoff
            for i, desc_line in enumerate(description_lines):
                desc_y = desc_start_y + i * description_line_height
                self._render_text_line(
                    sprite, desc_line, content_left + desc_left_margin, desc_y, 
                    content_width - desc_left_margin, text_color, centered=False,
                    font_size=self.description_font_size, bold=False
                )
    
    def _render_vertical_layout(self, sprite: Image.Image, draw: ImageDraw.Draw, 
                                 content_left: int, content_top: int,
                                 content_width: int, content_height: int,
                                 lines: list, line_height: int, text_color: int):
        """Render button with icon on top, text below (both centered).
        
        Uses TextWidget for text rendering.
        
        Args:
            sprite: Sprite image to draw onto
            draw: ImageDraw object
            content_left: Left edge of content area
            content_top: Top edge of content area
            content_width: Width of content area
            content_height: Height of content area
            lines: Text lines to render
            line_height: Height of each text line
            text_color: Color for text (0 or 255)
        """
        # Calculate total text height
        text_total_height = len(lines) * line_height
        
        # Calculate vertical distribution: icon on top, text below
        # Leave some spacing between icon and text
        icon_text_gap = 4
        total_content = self.icon_size + icon_text_gap + text_total_height
        
        # Center the combined icon+text vertically
        start_y = content_top + (content_height - total_content) // 2
        
        # Draw icon centered horizontally
        icon_x = content_left + content_width // 2
        icon_y = start_y + self.icon_size // 2
        self._draw_main_icon(draw, icon_x, icon_y, self.icon_size, self.selected)
        
        # Draw text centered below icon using TextWidget
        text_start_y = start_y + self.icon_size + icon_text_gap
        for i, line in enumerate(lines):
            text_y = text_start_y + i * line_height
            self._render_text_line(sprite, line, content_left, text_y, 
                                   content_width, text_color, centered=True)
    
    def _render_horizontal_layout(self, sprite: Image.Image, draw: ImageDraw.Draw,
                                   inside_left: int, inside_top: int,
                                   content_top: int, content_height: int,
                                   lines: list, line_height: int, text_color: int):
        """Render button with icon on left, text on right.
        
        Uses TextWidget for text rendering.
        Both icon and text are centered vertically in the content area.
        
        Args:
            sprite: Sprite image to draw onto
            draw: ImageDraw object
            inside_left: Left edge inside border
            inside_top: Top edge inside border
            content_top: Top of content area (with padding)
            content_height: Height of content area
            lines: Text lines to render
            line_height: Height of each text line
            text_color: Color for text (0 or 255)
        """
        # Draw icon on the left, centered vertically in content area
        icon_left = inside_left + self.icon_margin
        icon_x = icon_left + self.icon_size // 2
        # Center icon vertically in content area
        icon_y = content_top + content_height // 2
        self._draw_main_icon(draw, icon_x, icon_y, self.icon_size, self.selected)
        
        # Icon right edge (including icon_margin on right side)
        icon_right = icon_left + self.icon_size + self.icon_margin
        
        # Draw an optional trailing icon (e.g. radio indicator) at the right edge
        # and reserve space for it so text does not overlap.
        trailing_reserved = 0
        if self.trailing_icon_name:
            trailing_size = min(self.icon_size, content_height)
            trailing_cx = (self.width - self.margin - self.border_width
                           - self.padding - self.icon_margin - trailing_size // 2)
            trailing_cy = content_top + content_height // 2
            self._draw_icon(draw, self.trailing_icon_name, trailing_cx, trailing_cy,
                            trailing_size, self.selected)
            trailing_reserved = trailing_size + 2 * self.icon_margin
        
        # Calculate text width (remaining space after icon and trailing marker)
        text_x = icon_right
        text_width = (self.width - text_x - self.margin - self.border_width
                      - self.padding - trailing_reserved)
        
        if len(lines) > 1:
            # Multi-line: center text block vertically
            total_text_height = len(lines) * line_height
            text_y = content_top + (content_height - total_text_height) // 2
            for line in lines:
                self._render_text_line(sprite, line, text_x, text_y, text_width, text_color)
                text_y += line_height
        else:
            # Single line: center vertically in content area
            text_y = content_top + (content_height - self.label_height) // 2
            self._render_text_line(sprite, self.label, text_x, text_y, text_width, text_color)

    def _render_state_footer(self, sprite: Image.Image, draw: ImageDraw.Draw,
                             content_left: int, footer_top: int,
                             content_width: int, footer_height: int, text_color: int):
        """Render a centered "[checkbox] state" footer line for a toggle button.

        Draws the trailing icon (a checkbox reflecting the on/off state) directly
        left of the description text, the pair centered horizontally on one line
        at the bottom of the button. Used by the WiFi/Bluetooth merged status
        button so it reads as an enable/disable toggle.
        """
        from universalchess.resources import get_font

        text = self.description
        icon_size = min(self.description_font_size + 4, max(1, footer_height - 2))
        gap = 3
        text_width = int(draw.textlength(text, font=get_font(self.description_font_size)))

        group_width = icon_size + gap + text_width
        start_x = content_left + max(0, (content_width - group_width) // 2)
        center_y = footer_top + footer_height // 2

        self._draw_icon(draw, self.trailing_icon_name,
                        start_x + icon_size // 2, center_y, icon_size, self.selected)

        text_x = start_x + icon_size + gap
        text_y = center_y - (self.description_font_size + 2) // 2
        self._render_text_line(sprite, text, text_x, text_y, text_width + 4,
                               text_color, centered=False,
                               font_size=self.description_font_size, bold=False)

    def get_mask(self):
        """Get mask for transparent margin.
        
        Returns a mask where the margin area is transparent (black in mask)
        and the button area is opaque (white in mask).
        """
        mask = Image.new("1", (self.width, self.height), 0)  # Start all transparent
        draw = ImageDraw.Draw(mask)
        
        # Make the button area (inside margin) opaque
        border_left = self.margin
        border_top = self.margin
        border_right = self.width - 1 - self.margin
        border_bottom = self.height - 1 - self.margin
        draw.rectangle([border_left, border_top, border_right, border_bottom], fill=255)
        
        return mask
    
    def _draw_main_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                        size: int, selected: bool):
        """Draw the button's main icon: a preview image if set, else a named icon.

        When icon_image is provided it is composited (through icon_mask) so it
        sits on the menu background; otherwise the drawn icon_name is used.

        Args:
            draw: ImageDraw object whose backing image receives the paste
            x: X center position
            y: Y center position
            size: Icon size in pixels
            selected: Whether the button is selected (inverts the preview)
        """
        if self.icon_image is not None:
            self._draw_image_icon(draw, x, y, size, selected)
        else:
            self._draw_icon(draw, self.icon_name, x, y, size, selected)

    def _draw_image_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                         size: int, selected: bool):
        """Composite a preview image (icon_image) centered at (x, y).

        Scales the image and its mask to ``size`` with nearest-neighbour to keep
        pixel art crisp, inverts the image when the button is selected so it
        reads on the dark selected background, and pastes through the mask so the
        menu background shows around the piece.
        """
        img = self.icon_image
        mask = self.icon_mask
        if img.size != (size, size):
            img = img.resize((size, size), Image.NEAREST)
            if mask is not None:
                mask = mask.resize((size, size), Image.NEAREST)
        if selected:
            img = Image.eval(img, lambda p: 255 - p)
        target_img = draw._image
        target_img.paste(img, (x - size // 2, y - size // 2), mask)

    def _draw_radio_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                         size: int, line_color: int, checked: bool):
        """Draw a radio indicator (ring, with a filled centre dot when checked).

        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
            checked: If True, fill the centre to mark the active selection
        """
        s = size / 36.0
        r = max(4, int(11 * s))
        line_width = max(2, int(2 * s))
        draw.ellipse([x - r, y - r, x + r, y + r], outline=line_color, width=line_width)
        if checked:
            ir = max(2, int(5 * s))
            draw.ellipse([x - ir, y - ir, x + ir, y + ir], fill=line_color, outline=line_color)

    def _draw_icon(self, draw: ImageDraw.Draw, icon_name: str,
                   x: int, y: int, size: int, selected: bool):
        """Draw an icon at the specified position.
        
        Icons are drawn using PIL drawing primitives for e-paper
        compatibility and file independence.
        
        Args:
            draw: ImageDraw object
            icon_name: Icon identifier
            x: X position (center of icon)
            y: Y position (center of icon)
            size: Icon size in pixels
            selected: Whether this icon is in a selected button
        """
        half = size // 2
        left = x - half
        top = y - half
        right = x + half
        bottom = y + half
        
        # Line color (white for selected/inverted, black for normal)
        line_color = 255 if selected else 0
        
        if icon_name == "centaur":
            self._draw_knight_icon(draw, x, y, size, line_color, selected, face_right=True)
        elif icon_name == "universal":
            self._draw_universal_icon(draw, x, y, size, line_color, selected)
        elif icon_name == "universal_logo":
            self._draw_universal_logo(draw, x, y, size, line_color, selected)
        elif icon_name == "settings":
            self._draw_gear_icon(draw, x, y, size, line_color)
        elif icon_name == "resign":
            self._draw_resign_icon(draw, x, y, size, line_color)
        elif icon_name == "resign_white":
            self._draw_resign_flag_icon(draw, x, y, size, line_color, selected, is_white_flag=True)
        elif icon_name == "resign_black":
            self._draw_resign_flag_icon(draw, x, y, size, line_color, selected, is_white_flag=False)
        elif icon_name == "draw":
            self._draw_draw_icon(draw, x, y, size, line_color)
        elif icon_name == "cancel":
            self._draw_cancel_icon(draw, x, y, size, line_color)
        elif icon_name == "exit":
            self._draw_exit_icon(draw, x, y, size, line_color)
        elif icon_name == "sound":
            self._draw_sound_icon(draw, x, y, size, line_color)
        elif icon_name == "shutdown":
            self._draw_power_icon(draw, x, y, size, line_color)
        elif icon_name == "reboot":
            self._draw_reboot_icon(draw, x, y, size, line_color)
        elif icon_name in ("Qw", "Qb"):
            self._draw_queen_icon(draw, x, y, size, line_color, selected, icon_name == "Qw")
        elif icon_name in ("Rw", "Rb"):
            self._draw_rook_icon(draw, x, y, size, line_color, selected, icon_name == "Rw")
        elif icon_name in ("Bw", "Bb"):
            self._draw_bishop_icon(draw, x, y, size, line_color, selected, icon_name == "Bw")
        elif icon_name in ("Nw", "Nb"):
            self._draw_knight_piece_icon(draw, x, y, size, line_color, selected, icon_name == "Nw")
        elif icon_name == "engine":
            self._draw_engine_icon(draw, x, y, size, line_color)
        elif icon_name == "elo":
            self._draw_elo_icon(draw, x, y, size, line_color)
        elif icon_name == "color":
            self._draw_color_icon(draw, x, y, size, line_color)
        elif icon_name == "white_piece":
            self._draw_king_icon(draw, x, y, size, line_color, is_white=True)
        elif icon_name == "black_piece":
            self._draw_king_icon(draw, x, y, size, line_color, is_white=False)
        elif icon_name == "random":
            self._draw_random_icon(draw, x, y, size, line_color)
        elif icon_name == "wifi":
            self._draw_wifi_icon(draw, x, y, size, line_color)
        elif icon_name == "wifi_strong":
            self._draw_wifi_signal_icon(draw, x, y, size, line_color, strength=3)
        elif icon_name == "wifi_medium":
            self._draw_wifi_signal_icon(draw, x, y, size, line_color, strength=2)
        elif icon_name == "wifi_weak":
            self._draw_wifi_signal_icon(draw, x, y, size, line_color, strength=1)
        elif icon_name == "wifi_disabled":
            self._draw_wifi_disabled_icon(draw, x, y, size, line_color)
        elif icon_name == "wifi_disconnected":
            self._draw_wifi_signal_icon(draw, x, y, size, line_color, strength=0)
        elif icon_name == "system":
            self._draw_system_icon(draw, x, y, size, line_color)
        elif icon_name == "positions":
            self._draw_positions_icon(draw, x, y, size, line_color)
        elif icon_name == "positions_test":
            self._draw_positions_test_icon(draw, x, y, size, line_color)
        elif icon_name == "positions_puzzles":
            self._draw_positions_puzzles_icon(draw, x, y, size, line_color)
        elif icon_name == "positions_endgames":
            self._draw_positions_endgames_icon(draw, x, y, size, line_color)
        elif icon_name == "positions_custom":
            self._draw_positions_custom_icon(draw, x, y, size, line_color)
        elif icon_name == "en_passant":
            self._draw_en_passant_icon(draw, x, y, size, line_color)
        elif icon_name == "castling":
            self._draw_castling_icon(draw, x, y, size, line_color)
        elif icon_name == "promotion":
            self._draw_promotion_icon(draw, x, y, size, line_color)
        elif icon_name == "timer":
            self._draw_timer_icon(draw, x, y, size, line_color, checked=False)
        elif icon_name == "timer_checked":
            self._draw_timer_icon(draw, x, y, size, line_color, checked=True)
        elif icon_name == "bluetooth":
            self._draw_bluetooth_icon(draw, x, y, size, line_color)
        elif icon_name == "account":
            self._draw_account_icon(draw, x, y, size, line_color)
        elif icon_name == "players":
            self._draw_players_icon(draw, x, y, size, line_color)
        elif icon_name == "lichess":
            self._draw_lichess_icon(draw, x, y, size, line_color)
        elif icon_name == "checkbox_checked":
            self._draw_checkbox_icon(draw, x, y, size, line_color, checked=True)
        elif icon_name == "checkbox_empty":
            self._draw_checkbox_icon(draw, x, y, size, line_color, checked=False)
        elif icon_name == "radio_checked":
            self._draw_radio_icon(draw, x, y, size, line_color, checked=True)
        elif icon_name == "radio_empty":
            self._draw_radio_icon(draw, x, y, size, line_color, checked=False)
        elif icon_name == "display":
            self._draw_display_icon(draw, x, y, size, line_color)
        elif icon_name == "cast":
            self._draw_cast_icon(draw, x, y, size, line_color)
        elif icon_name == "info":
            self._draw_info_icon(draw, x, y, size, line_color)
        elif icon_name == "download":
            self._draw_download_icon(draw, x, y, size, line_color)
        elif icon_name == "star":
            self._draw_star_icon(draw, x, y, size, line_color)
        elif icon_name == "play":
            self._draw_play_icon(draw, x, y, size, line_color)
        elif icon_name == "update":
            self._draw_update_icon(draw, x, y, size, line_color)
        elif icon_name == "undo":
            self._draw_undo_icon(draw, x, y, size, line_color)
        elif icon_name == "agents":
            self._draw_agents_icon(draw, x, y, size, line_color)
        else:
            # Default: simple square placeholder
            draw.rectangle([left + 4, top + 4, right - 4, bottom - 4],
                          outline=line_color, width=2)

    def _draw_agents_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                          size: int, line_color: int) -> None:
        """Draw three interconnected circles (a small node graph).

        Icon for the Agents settings section. Three nodes arranged in a triangle
        and joined by edges read as a network of agents even at the small e-paper
        icon sizes. Edges are drawn first so the filled nodes sit cleanly on top.

        Args:
            draw: ImageDraw object.
            x: X center position.
            y: Y center position.
            size: Icon size in pixels.
            line_color: Line color (0 black, 255 white when selected/inverted).
        """
        r_ring = max(3, int(size * 0.32))  # circumradius to the node centers
        r_node = max(2, int(size * 0.15))  # radius of each node
        edge_width = max(1, size // 16)

        # Vertices (y grows downward): top, bottom-right, bottom-left.
        angles = (-math.pi / 2.0, math.pi / 6.0, 5.0 * math.pi / 6.0)
        nodes = [
            (x + int(round(r_ring * math.cos(a))),
             y + int(round(r_ring * math.sin(a))))
            for a in angles
        ]

        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                draw.line([nodes[i], nodes[j]], fill=line_color, width=edge_width)

        for (nx, ny) in nodes:
            draw.ellipse([nx - r_node, ny - r_node, nx + r_node, ny + r_node],
                         outline=line_color, fill=line_color)

    def _draw_star_icon(self, draw: ImageDraw.Draw, x: int, y: int, size: int, line_color: int) -> None:
        """Draw a 5-point star icon.

        Used for LED brightness indication.

        Args:
            draw: ImageDraw object.
            x: X center position.
            y: Y center position.
            size: Icon size in pixels.
            line_color: Line color (0 black, 255 white).
        """
        import math

        # Use an outer radius that fits in the icon bounds, leaving a small margin.
        r_outer = max(2, int(size * 0.45))
        r_inner = max(1, int(r_outer * 0.45))

        points = []
        # Start at -90° so a point faces up.
        start_angle = -math.pi / 2.0
        for i in range(10):
            r = r_outer if i % 2 == 0 else r_inner
            angle = start_angle + i * (math.pi / 5.0)
            px = x + int(round(r * math.cos(angle)))
            py = y + int(round(r * math.sin(angle)))
            points.append((px, py))

        # Outline + small fill to read well on e-paper
        draw.polygon(points, outline=line_color, fill=None)
        # Add a subtle inner fill by drawing a smaller star inside
        r_outer2 = max(1, int(r_outer * 0.75))
        r_inner2 = max(1, int(r_inner * 0.75))
        points2 = []
        for i in range(10):
            r = r_outer2 if i % 2 == 0 else r_inner2
            angle = start_angle + i * (math.pi / 5.0)
            px = x + int(round(r * math.cos(angle)))
            py = y + int(round(r * math.sin(angle)))
            points2.append((px, py))
        draw.polygon(points2, outline=None, fill=line_color)
    
    def _draw_knight_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                          size: int, line_color: int, selected: bool,
                          face_right: bool = False):
        """Draw a chess knight icon.
        
        Uses path coordinates derived from python-chess (GPL-3.0+) SVG assets.
        The original SVG viewBox is 45x45, scaled to fit the icon size.
        Points are sampled from the bezier curves in the original SVG paths.
        
        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line/fill color
            selected: Whether button is selected
            face_right: If True, mirror the knight to face right instead of left
        """
        half = size // 2
        s = size / 45.0  # Scale factor (python-chess uses 45x45 viewBox)
        
        # Offset to center the 45x45 coordinate system in our icon area
        ox = x - half
        oy = y - half
        
        def pt(px: float, py: float) -> tuple:
            """Convert python-chess SVG coordinate to icon coordinate."""
            if face_right:
                # Mirror horizontally: x' = 45 - x (flip around center of 45x45)
                px = 45.0 - px
            return (ox + int(px * s), oy + int(py * s))
        
        # Body polygon - sampled from bezier curve:
        # M 22,10 C 32.5,11 38.5,18 38,39 L 15,39 C 15,30 25,32.5 23,18
        body_points = [
            pt(22.0, 10.0), pt(24.8, 10.7), pt(27.7, 11.4), pt(30.0, 12.7),
            pt(32.3, 14.6), pt(34.0, 17.0), pt(35.6, 20.0), pt(36.7, 23.6),
            pt(37.5, 28.0), pt(37.9, 33.2), pt(38.0, 39.0), pt(15.0, 39.0),
            pt(15.3, 36.6), pt(16.0, 34.7), pt(17.1, 33.2), pt(18.3, 31.8),
            pt(19.8, 30.6), pt(21.2, 29.0), pt(22.2, 27.2), pt(22.9, 24.8),
            pt(23.3, 21.9), pt(23.2, 18.5),
        ]
        
        # Head polygon - sampled from bezier curve:
        # M 24,18 C 24.38,20.91 18.45,25.37 16,27 C 13,29 13.18,31.34 11,31
        # C 9.958,30.06 12.41,27.96 11,28 C 10,28 11.19,29.23 10,30
        # C 9,30 5.997,31 6,26 C 6,24 12,14 12,14 C 12,14 13.89,12.1 14,10.5
        # C 13.27,9.506 13.5,8.5 13.5,7.5 C 14.5,6.5 16.5,10 16.5,10
        # L 18.5,10 C 18.5,10 19.28,8.008 21,7 C 22,7 22,10 22,10
        head_points = [
            pt(24.0, 18.0), pt(24.3, 19.5), pt(23.7, 21.1), pt(22.5, 22.5),
            pt(20.9, 23.9), pt(18.8, 25.3), pt(16.0, 27.0), pt(14.4, 28.0),
            pt(12.9, 29.3), pt(11.7, 30.5), pt(11.0, 31.0), pt(10.6, 30.5),
            pt(11.0, 29.4), pt(11.3, 28.3), pt(11.0, 28.0), pt(10.6, 28.0),
            pt(10.6, 28.8), pt(10.3, 29.5), pt(10.0, 30.0), pt(9.2, 30.2),
            pt(7.5, 29.7), pt(6.2, 28.1), pt(6.0, 26.0), pt(6.0, 25.0),
            pt(7.0, 22.5), pt(8.5, 19.5), pt(10.2, 16.8), pt(11.5, 15.0),
            pt(12.0, 14.0), pt(12.0, 14.0), pt(12.5, 13.0), pt(13.2, 12.0),
            pt(13.7, 11.2), pt(14.0, 10.5), pt(13.8, 10.0), pt(13.6, 9.3),
            pt(13.5, 8.5), pt(13.5, 7.5), pt(14.0, 7.2), pt(15.0, 8.0),
            pt(16.0, 9.2), pt(16.5, 10.0), pt(18.5, 10.0), pt(18.5, 10.0),
            pt(19.0, 9.2), pt(19.8, 8.3), pt(21.0, 7.0), pt(21.3, 7.0),
            pt(21.7, 7.5), pt(22.0, 8.5), pt(22.0, 10.0),
        ]
        
        # Draw the main body shape
        draw.polygon(body_points, fill=line_color, outline=line_color)
        # Draw the head on top
        draw.polygon(head_points, fill=line_color, outline=line_color)
        
        # Eye - small filled circle (contrasting color)
        # Original SVG: ellipse at (9, 25.5) with radius 0.5
        eye_x, eye_y = pt(9, 25.5)
        eye_r = max(1, int(1.5 * s))
        eye_color = 0 if selected else 255
        draw.ellipse([eye_x - eye_r, eye_y - eye_r, eye_x + eye_r, eye_y + eye_r],
                    fill=eye_color, outline=eye_color)
        
        # Nostril - small ellipse (contrasting color)
        # Original SVG: rotated ellipse near (14.5, 15.5)
        nostril_x, nostril_y = pt(14, 16)
        nostril_r = max(1, int(1.2 * s))
        draw.ellipse([nostril_x - nostril_r, nostril_y - nostril_r,
                     nostril_x + nostril_r, nostril_y + nostril_r],
                    fill=eye_color, outline=eye_color)
    
    def _draw_universal_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                             size: int, line_color: int, selected: bool):
        """Draw the Universal mode icon using the knight piece.
        
        Uses the same knight design as _draw_knight_icon for consistency.
        """
        # Delegate to the knight icon drawing
        self._draw_knight_icon(draw, x, y, size, line_color, selected)
    
    def _draw_universal_logo(self, draw: ImageDraw.Draw, x: int, y: int,
                             size: int, line_color: int, selected: bool):
        """Draw the Universal/PLAY logo using pre-rendered knight bitmap.
        
        Uses a high-quality bitmap rendered from the python-chess knight SVG
        for crisp rendering at any size.
        
        Args:
            draw: ImageDraw object
            x: Center X position
            y: Center Y position
            size: Icon size (width/height)
            line_color: Stroke/fill color (0=black, 255=white)
            selected: Whether button is selected (inverts colors)
        """
        # Check module-level logo cache
        if size not in _knight_logos:
            # Fallback to simple knight icon if bitmap not available
            log.debug(f"Knight logo size {size} not in cache, using fallback")
            self._draw_knight_icon(draw, x, y, size, line_color, selected)
            return
        
        logo, mask = _knight_logos[size]
        
        # If selected, invert the logo (but keep mask the same)
        if selected:
            logo = Image.eval(logo, lambda p: 255 - p)
        
        # Calculate position (centered)
        paste_x = x - size // 2
        paste_y = y - size // 2
        
        # Get the underlying image from the draw object and paste with mask
        target_img = draw._image
        target_img.paste(logo, (paste_x, paste_y), mask)
    
    def _draw_gear_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                        size: int, line_color: int):
        """Draw a gear/cog icon.
        
        All drawing stays within the icon bounds (x-half to x+half, y-half to y+half).
        Uses proper proportions with a solid gear body and rectangular teeth.
        """
        s = size / 36.0  # Scale factor
        half = size // 2
        
        # Gear body - solid circle with smaller hole
        body_r = int(10 * s)  # Main gear body radius
        hole_r = int(4 * s)   # Center hole radius
        
        # Draw solid gear body
        draw.ellipse([x - body_r, y - body_r, x + body_r, y + body_r],
                    fill=line_color, outline=line_color)
        
        # Draw teeth as rectangles extending from body
        num_teeth = 8
        tooth_width = max(2, int(4 * s))
        tooth_height = max(2, int(5 * s))
        
        for i in range(num_teeth):
            angle = i * (360 / num_teeth) * (math.pi / 180)
            # Tooth center position at edge of body
            tooth_cx = x + int((body_r + tooth_height // 2) * math.cos(angle))
            tooth_cy = y + int((body_r + tooth_height // 2) * math.sin(angle))
            
            # Draw tooth as a small rectangle (approximated with polygon for rotation)
            # Calculate perpendicular direction for tooth width
            perp_angle = angle + math.pi / 2
            hw = tooth_width // 2
            hh = tooth_height // 2
            
            # Four corners of rotated rectangle
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            cos_p, sin_p = math.cos(perp_angle), math.sin(perp_angle)
            
            tooth_points = [
                (tooth_cx + int(-hh * cos_a + hw * cos_p), 
                 tooth_cy + int(-hh * sin_a + hw * sin_p)),
                (tooth_cx + int(hh * cos_a + hw * cos_p), 
                 tooth_cy + int(hh * sin_a + hw * sin_p)),
                (tooth_cx + int(hh * cos_a - hw * cos_p), 
                 tooth_cy + int(hh * sin_a - hw * sin_p)),
                (tooth_cx + int(-hh * cos_a - hw * cos_p), 
                 tooth_cy + int(-hh * sin_a - hw * sin_p)),
            ]
            draw.polygon(tooth_points, fill=line_color, outline=line_color)
        
        # Draw center hole (contrasting color)
        hole_color = 255 if line_color == 0 else 0
        draw.ellipse([x - hole_r, y - hole_r, x + hole_r, y + hole_r],
                    fill=hole_color, outline=hole_color)
    
    def _draw_resign_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                          size: int, line_color: int):
        """Draw a flag (resign) icon."""
        half = size // 2
        left = x - half
        top = y - half
        bottom = y + half
        
        # Flag pole
        pole_x = left + 4
        draw.line([(pole_x, top + 2), (pole_x, bottom - 2)], fill=line_color, width=2)
        
        # Flag (wavy)
        flag_top = top + 4
        flag_bottom = y
        flag_right = x + half - 4
        draw.polygon([
            (pole_x, flag_top),
            (flag_right, flag_top + 4),
            (flag_right - 4, (flag_top + flag_bottom) // 2),
            (flag_right, flag_bottom - 4),
            (pole_x, flag_bottom),
        ], fill=line_color, outline=line_color)
    
    def _draw_resign_flag_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                               size: int, line_color: int, selected: bool,
                               is_white_flag: bool):
        """Draw a colored flag (resign) icon for white/black resignation.
        
        Args:
            draw: ImageDraw object
            x: X position (center of icon)
            y: Y position (center of icon)
            size: Icon size in pixels
            line_color: Color for the pole (based on selected state)
            selected: Whether this icon is in a selected button
            is_white_flag: If True, draw white flag with black border;
                          if False, draw black flag with white border
        """
        half = size // 2
        left = x - half
        top = y - half
        bottom = y + half
        
        # Flag pole - uses line_color (inverts with selection)
        pole_x = left + 4
        draw.line([(pole_x, top + 2), (pole_x, bottom - 2)], fill=line_color, width=2)
        
        # Flag colors: white flag = white fill, black border
        #              black flag = black fill, white border
        if is_white_flag:
            flag_fill = 255  # White
            flag_border = 0   # Black border
        else:
            flag_fill = 0     # Black
            flag_border = 255 # White border
        
        # Flag (wavy shape)
        flag_top = top + 4
        flag_bottom = y
        flag_right = x + half - 4
        flag_points = [
            (pole_x, flag_top),
            (flag_right, flag_top + 4),
            (flag_right - 4, (flag_top + flag_bottom) // 2),
            (flag_right, flag_bottom - 4),
            (pole_x, flag_bottom),
        ]
        
        # Draw flag with fill and contrasting border
        draw.polygon(flag_points, fill=flag_fill, outline=flag_border)
    
    def _draw_draw_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                        size: int, line_color: int):
        """Draw a handshake (draw offer) icon - simplified as two hands."""
        half = size // 2
        
        # Two overlapping circles representing agreement
        r = half // 2
        draw.ellipse([x - r - 4, y - r, x + r - 4, y + r], outline=line_color, width=2)
        draw.ellipse([x - r + 4, y - r, x + r + 4, y + r], outline=line_color, width=2)
        
        # Equals sign below
        draw.line([(x - half + 6, y + half - 8), (x + half - 6, y + half - 8)],
                 fill=line_color, width=2)
        draw.line([(x - half + 6, y + half - 3), (x + half - 6, y + half - 3)],
                 fill=line_color, width=2)
    
    def _draw_cancel_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                          size: int, line_color: int):
        """Draw an X (cancel) icon."""
        half = size // 2
        margin = 6
        
        # X shape
        draw.line([(x - half + margin, y - half + margin),
                  (x + half - margin, y + half - margin)], fill=line_color, width=3)
        draw.line([(x + half - margin, y - half + margin),
                  (x - half + margin, y + half - margin)], fill=line_color, width=3)
    
    def _draw_exit_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                        size: int, line_color: int):
        """Draw a door with arrow (exit) icon."""
        half = size // 2
        left = x - half
        top = y - half
        right = x + half
        bottom = y + half
        
        # Door frame
        draw.rectangle([left + 4, top + 2, right - 8, bottom - 2], outline=line_color, width=2)
        
        # Arrow pointing right/out
        arrow_y = y
        arrow_start = x
        arrow_end = right - 2
        draw.line([(arrow_start, arrow_y), (arrow_end, arrow_y)], fill=line_color, width=2)
        draw.line([(arrow_end - 6, arrow_y - 6), (arrow_end, arrow_y)], fill=line_color, width=2)
        draw.line([(arrow_end - 6, arrow_y + 6), (arrow_end, arrow_y)], fill=line_color, width=2)
    
    def _draw_sound_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                         size: int, line_color: int):
        """Draw a speaker icon.
        
        All drawing stays within the icon bounds (x-half to x+half).
        """
        half = size // 2
        s = size / 36.0  # Scale factor
        
        # Speaker body (left side of icon)
        speaker_left = x - half + int(2*s)
        speaker_width = int(12*s)
        speaker_height = int(8*s)
        cone_width = int(6*s)
        cone_height = int(14*s)
        
        draw.polygon([
            (speaker_left, y - speaker_height//2),
            (speaker_left, y + speaker_height//2),
            (speaker_left + speaker_width//2, y + speaker_height//2),
            (speaker_left + speaker_width, y + cone_height//2),
            (speaker_left + speaker_width, y - cone_height//2),
            (speaker_left + speaker_width//2, y - speaker_height//2),
        ], fill=line_color)
        
        # Sound waves (right side, within bounds)
        wave_start = speaker_left + speaker_width + int(3*s)
        wave_width = max(2, int(3*s))
        for i in range(2):
            arc_x = wave_start + i * int(6*s)
            arc_r = int(4*s) + i * int(2*s)
            # Ensure waves stay within bounds
            if arc_x + wave_width <= x + half:
                draw.arc([arc_x - wave_width, y - arc_r, arc_x + wave_width, y + arc_r],
                        start=-60, end=60, fill=line_color, width=max(1, int(1.5*s)))
    
    def _draw_power_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                         size: int, line_color: int):
        """Draw a power button icon."""
        half = size // 2
        r = half - 4
        
        # Circle with gap at top
        draw.arc([x - r, y - r, x + r, y + r], start=50, end=310, fill=line_color, width=2)
        
        # Vertical line at top
        draw.line([(x, y - r - 2), (x, y - 2)], fill=line_color, width=2)
    
    def _draw_reboot_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                          size: int, line_color: int):
        """Draw a circular arrow (reboot) icon."""
        half = size // 2
        r = half - 4
        
        # Circular arc
        draw.arc([x - r, y - r, x + r, y + r], start=30, end=330, fill=line_color, width=2)
        
        # Arrow head at end of arc
        arrow_x = x + int(r * math.cos(math.radians(30)))
        arrow_y = y - int(r * math.sin(math.radians(30)))
        draw.polygon([
            (arrow_x, arrow_y),
            (arrow_x + 6, arrow_y + 2),
            (arrow_x + 2, arrow_y + 6),
        ], fill=line_color)
    
    def _draw_queen_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                         size: int, line_color: int, selected: bool, is_white: bool):
        """Draw a queen chess piece icon.
        
        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
            selected: Whether button is selected
            is_white: Whether to draw white piece (filled) or black piece (outline)
        """
        half = size // 2
        s = size / 36.0  # Scale factor
        
        # Base
        base_top = y + half - int(6*s)
        draw.rectangle([x - int(10*s), base_top, x + int(10*s), y + half - int(2*s)],
                      fill=line_color, outline=line_color)
        
        # Body (trapezoid)
        body_points = [
            (x - int(8*s), base_top),
            (x - int(6*s), y - int(4*s)),
            (x + int(6*s), y - int(4*s)),
            (x + int(8*s), base_top),
        ]
        draw.polygon(body_points, fill=line_color, outline=line_color)
        
        # Crown points (5 spikes)
        crown_base = y - int(4*s)
        crown_top = y - half + int(2*s)
        for i in range(5):
            spike_x = x + int((i - 2) * 4 * s)
            draw.polygon([
                (spike_x - int(2*s), crown_base),
                (spike_x, crown_top),
                (spike_x + int(2*s), crown_base),
            ], fill=line_color, outline=line_color)
            # Small circle on top of each spike
            draw.ellipse([spike_x - int(2*s), crown_top - int(2*s),
                         spike_x + int(2*s), crown_top + int(2*s)],
                        fill=line_color, outline=line_color)
    
    def _draw_rook_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                        size: int, line_color: int, selected: bool, is_white: bool):
        """Draw a rook chess piece icon."""
        half = size // 2
        s = size / 36.0
        
        # Base
        base_top = y + half - int(6*s)
        draw.rectangle([x - int(10*s), base_top, x + int(10*s), y + half - int(2*s)],
                      fill=line_color, outline=line_color)
        
        # Body
        draw.rectangle([x - int(7*s), y - int(4*s), x + int(7*s), base_top],
                      fill=line_color, outline=line_color)
        
        # Battlements (top). The rectangle spans from the crenellation top
        # (battlement_top, smaller y) down to where it meets the body
        # (battlement_base, larger y). Coordinates must be ordered y0 <= y1;
        # the previous order was inverted and only rendered because older
        # Pillow tolerated swapped rectangle bounds.
        battlement_base = y - int(4*s)
        battlement_top = y - half + int(2*s)
        draw.rectangle([x - int(9*s), battlement_top, x + int(9*s), battlement_base],
                      fill=line_color, outline=line_color)
        
        # Cut out the gaps in battlements
        gap_color = 0 if selected else 255
        for i in [-1, 1]:
            gap_x = x + int(i * 4 * s)
            draw.rectangle([gap_x - int(2*s), battlement_top,
                           gap_x + int(2*s), battlement_top + int(4*s)],
                          fill=gap_color, outline=gap_color)
    
    def _draw_bishop_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                          size: int, line_color: int, selected: bool, is_white: bool):
        """Draw a bishop chess piece icon."""
        half = size // 2
        s = size / 36.0
        
        # Base
        base_top = y + half - int(6*s)
        draw.rectangle([x - int(8*s), base_top, x + int(8*s), y + half - int(2*s)],
                      fill=line_color, outline=line_color)
        
        # Body (tapered)
        body_points = [
            (x - int(6*s), base_top),
            (x - int(4*s), y),
            (x, y - int(8*s)),
            (x + int(4*s), y),
            (x + int(6*s), base_top),
        ]
        draw.polygon(body_points, fill=line_color, outline=line_color)
        
        # Mitre top (pointed hat)
        draw.polygon([
            (x - int(4*s), y - int(8*s)),
            (x, y - half + int(2*s)),
            (x + int(4*s), y - int(8*s)),
        ], fill=line_color, outline=line_color)
        
        # Small ball on top
        draw.ellipse([x - int(2*s), y - half, x + int(2*s), y - half + int(4*s)],
                    fill=line_color, outline=line_color)
        
        # Diagonal slit on mitre
        slit_color = 0 if selected else 255
        draw.line([(x - int(2*s), y - int(6*s)), (x + int(2*s), y - int(10*s))],
                 fill=slit_color, width=max(1, int(1.5*s)))
    
    def _draw_knight_piece_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                                size: int, line_color: int, selected: bool, is_white: bool):
        """Draw a knight chess piece icon (for promotion menu).
        
        Similar to _draw_knight_icon but designed for promotion context.
        """
        # Reuse the existing knight icon drawing
        self._draw_knight_icon(draw, x, y, size, line_color, selected)
    
    def _draw_engine_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                          size: int, line_color: int):
        """Draw an engine/CPU icon (circuit-like design)."""
        half = size // 2
        s = size / 36.0  # Scale factor
        
        # Central rectangle (chip body)
        chip_half = int(10*s)
        draw.rectangle([x - chip_half, y - chip_half, x + chip_half, y + chip_half],
                      outline=line_color, width=max(1, int(2*s)))
        
        # Pins on each side
        pin_len = int(4*s)
        pin_width = max(1, int(2*s))
        for offset in [-int(5*s), int(5*s)]:
            # Top pins
            draw.line([(x + offset, y - chip_half), (x + offset, y - chip_half - pin_len)],
                     fill=line_color, width=pin_width)
            # Bottom pins
            draw.line([(x + offset, y + chip_half), (x + offset, y + chip_half + pin_len)],
                     fill=line_color, width=pin_width)
            # Left pins
            draw.line([(x - chip_half, y + offset), (x - chip_half - pin_len, y + offset)],
                     fill=line_color, width=pin_width)
            # Right pins
            draw.line([(x + chip_half, y + offset), (x + chip_half + pin_len, y + offset)],
                     fill=line_color, width=pin_width)
    
    def _draw_elo_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                       size: int, line_color: int):
        """Draw an ELO/rating icon (chart/graph style)."""
        half = size // 2
        s = size / 36.0  # Scale factor
        
        # Draw bar chart style
        bar_width = int(6*s)
        gap = int(3*s)
        
        # Three bars of increasing height
        heights = [int(8*s), int(14*s), int(20*s)]
        start_x = x - int(12*s)
        base_y = y + int(10*s)
        
        for i, h in enumerate(heights):
            bx = start_x + i * (bar_width + gap)
            draw.rectangle([bx, base_y - h, bx + bar_width, base_y],
                          fill=line_color, outline=line_color)
        
        # Baseline
        draw.line([(x - half + int(2*s), base_y), (x + half - int(2*s), base_y)],
                 fill=line_color, width=max(1, int(2*s)))
    
    def _draw_color_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                         size: int, line_color: int):
        """Draw a color selection icon (half white, half black circle)."""
        half = size // 2
        s = size / 36.0  # Scale factor
        radius = int(14*s)
        
        # Draw circle outline
        draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                    outline=line_color, width=max(1, int(2*s)))
        
        # Fill right half (simulate with arc/pie)
        # Draw a vertical line and fill the right semicircle
        draw.pieslice([x - radius, y - radius, x + radius, y + radius],
                     start=-90, end=90, fill=line_color)
    
    def _draw_king_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                        size: int, line_color: int, is_white: bool):
        """Draw a chess king icon.
        
        Args:
            is_white: If True, draw hollow (white piece), if False, draw filled (black piece)
        """
        half = size // 2
        s = size / 36.0  # Scale factor
        
        # Cross on top
        cross_width = max(1, int(2*s))
        cross_half = int(4*s)
        cross_top = y - half + int(2*s)
        
        # Vertical part of cross
        draw.line([(x, cross_top), (x, cross_top + int(8*s))],
                 fill=line_color, width=cross_width)
        # Horizontal part of cross
        draw.line([(x - cross_half, cross_top + int(3*s)), (x + cross_half, cross_top + int(3*s))],
                 fill=line_color, width=cross_width)
        
        # Crown body (bell shape)
        crown_top = y - int(6*s)
        crown_bottom = y + int(8*s)
        crown_width_top = int(6*s)
        crown_width_bottom = int(10*s)
        
        if is_white:
            # Hollow crown
            draw.polygon([
                (x - crown_width_top, crown_top),
                (x + crown_width_top, crown_top),
                (x + crown_width_bottom, crown_bottom),
                (x - crown_width_bottom, crown_bottom),
            ], outline=line_color)
        else:
            # Filled crown
            draw.polygon([
                (x - crown_width_top, crown_top),
                (x + crown_width_top, crown_top),
                (x + crown_width_bottom, crown_bottom),
                (x - crown_width_bottom, crown_bottom),
            ], fill=line_color)
        
        # Base
        base_y = y + int(10*s)
        base_half = int(12*s)
        draw.rectangle([x - base_half, base_y, x + base_half, base_y + int(4*s)],
                      fill=line_color if not is_white else None,
                      outline=line_color, width=max(1, int(1.5*s)))
    
    def _draw_random_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                          size: int, line_color: int):
        """Draw a random/dice icon."""
        half = size // 2
        s = size / 36.0  # Scale factor
        
        # Dice outline (slightly rotated rectangle effect via diamond)
        dice_half = int(12*s)
        draw.rectangle([x - dice_half, y - dice_half, x + dice_half, y + dice_half],
                      outline=line_color, width=max(1, int(2*s)))
        
        # Dice dots (6 pattern)
        dot_radius = int(2*s)
        dot_positions = [
            (-int(5*s), -int(5*s)),  # top-left
            (int(5*s), -int(5*s)),   # top-right
            (-int(5*s), 0),          # middle-left
            (int(5*s), 0),           # middle-right
            (-int(5*s), int(5*s)),   # bottom-left
            (int(5*s), int(5*s)),    # bottom-right
        ]
        
        for dx, dy in dot_positions:
            draw.ellipse([x + dx - dot_radius, y + dy - dot_radius,
                         x + dx + dot_radius, y + dy + dot_radius],
                        fill=line_color)
    
    def _draw_wifi_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                        size: int, line_color: int):
        """Draw a WiFi signal icon (concentric arcs)."""
        self._draw_wifi_signal_icon(draw, x, y, size, line_color, strength=3)

    def _draw_wifi_signal_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                               size: int, line_color: int, strength: int = 3):
        """Draw a WiFi signal icon with variable strength.

        Draws arcs with thick lines for active strength levels and thin lines
        for inactive levels (strength=0 shows all arcs as thin/inactive).

        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color
            strength: Signal strength 0-3 (0=disconnected, 1=weak, 2=medium, 3=strong)
        """
        s = size / 36.0  # Scale factor

        # Draw concentric arcs from bottom center
        base_y = y + int(10*s)

        # Arc radii
        radii = [int(6*s), int(12*s), int(18*s)]
        thick_width = max(2, int(3*s))
        thin_width = max(1, int(1.5*s))
        
        for i, radius in enumerate(radii):
            # Active arcs (up to strength level) are thick, inactive are thin
            if i < strength:
                draw.arc([x - radius, base_y - radius, x + radius, base_y + radius],
                        start=225, end=315, fill=line_color, width=thick_width)
            else:
                # Draw thin arc for inactive signal levels
                draw.arc([x - radius, base_y - radius, x + radius, base_y + radius],
                        start=225, end=315, fill=line_color, width=thin_width)
        
        # Small dot at the bottom center (always drawn)
        dot_r = max(2, int(3*s))
        draw.ellipse([x - dot_r, base_y - dot_r, x + dot_r, base_y + dot_r],
                    fill=line_color)

    def _draw_wifi_disabled_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                                  size: int, line_color: int):
        """Draw a WiFi icon with a cross overlay indicating disabled state."""
        # Draw the base wifi icon (disconnected/thin arcs)
        self._draw_wifi_signal_icon(draw, x, y, size, line_color, strength=0)
        
        # Draw diagonal cross over the icon
        s = size / 36.0  # Scale factor
        cross_offset = int(12*s)
        cross_width = max(2, int(3*s))
        
        draw.line([x - cross_offset, y - cross_offset, x + cross_offset, y + cross_offset],
                 fill=line_color, width=cross_width)
        draw.line([x + cross_offset, y - cross_offset, x - cross_offset, y + cross_offset],
                 fill=line_color, width=cross_width)
    
    def _draw_system_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                          size: int, line_color: int):
        """Draw a system/wrench icon."""
        half = size // 2
        s = size / 36.0  # Scale factor
        
        # Draw a wrench shape
        # Handle (diagonal rectangle)
        handle_width = int(4*s)
        draw.line([(x - int(10*s), y + int(10*s)), (x + int(4*s), y - int(4*s))],
                 fill=line_color, width=handle_width)
        
        # Wrench head (circular part with opening)
        head_x = x + int(6*s)
        head_y = y - int(6*s)
        head_r = int(8*s)
        draw.arc([head_x - head_r, head_y - head_r, head_x + head_r, head_y + head_r],
                start=45, end=315, fill=line_color, width=max(1, int(3*s)))
        
        # Opening of wrench
        draw.line([(head_x, head_y - head_r), (head_x + int(6*s), head_y - int(10*s))],
                 fill=line_color, width=max(1, int(2*s)))
        draw.line([(head_x + head_r, head_y), (head_x + int(10*s), head_y + int(6*s))],
                 fill=line_color, width=max(1, int(2*s)))
    
    def _draw_positions_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                             size: int, line_color: int):
        """Draw a chess board grid icon representing predefined positions."""
        half = size // 2
        s = size / 36.0  # Scale factor
        
        # Draw a 3x3 or 4x4 mini chess board pattern
        grid_size = 4
        cell_size = int(size * 0.8 / grid_size)
        board_size = cell_size * grid_size
        start_x = x - board_size // 2
        start_y = y - board_size // 2
        
        # Draw outer border
        draw.rectangle([start_x - 1, start_y - 1, 
                       start_x + board_size, start_y + board_size],
                      outline=line_color, width=1)
        
        # Draw filled squares (checkerboard pattern)
        for row in range(grid_size):
            for col in range(grid_size):
                if (row + col) % 2 == 1:  # Dark squares
                    cell_x = start_x + col * cell_size
                    cell_y = start_y + row * cell_size
                    draw.rectangle([cell_x, cell_y, 
                                   cell_x + cell_size - 1, cell_y + cell_size - 1],
                                  fill=line_color)
        
        # Draw a small piece indicator (dot) on one square
        indicator_x = start_x + int(1.5 * cell_size)
        indicator_y = start_y + int(1.5 * cell_size)
        dot_r = max(2, int(cell_size * 0.25))
        # Use opposite color (white on dark square)
        fill_color = 255 if line_color == 0 else 0
        draw.ellipse([indicator_x - dot_r, indicator_y - dot_r,
                     indicator_x + dot_r, indicator_y + dot_r],
                    fill=fill_color, outline=line_color)

    def _draw_positions_test_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                                   size: int, line_color: int):
        """Draw a test/flask icon for test positions category.
        
        Draws a beaker/flask shape with a checkmark, representing test verification.
        All coordinates stay within half = size // 2 from center.
        """
        s = size / 36.0  # Scale factor
        half = size // 2
        
        # Flask outline - stays within bounds (-12s to +12s = within half)
        # Neck (top narrow part)
        neck_w = int(3 * s)
        draw.rectangle([x - neck_w, y - int(12 * s),
                       x + neck_w, y - int(6 * s)],
                      outline=line_color, width=max(1, int(2 * s)))
        
        # Body (trapezoid widening downward)
        body_points = [
            (x - neck_w, y - int(6 * s)),
            (x + neck_w, y - int(6 * s)),
            (x + int(10 * s), y + int(12 * s)),
            (x - int(10 * s), y + int(12 * s)),
        ]
        draw.polygon(body_points, outline=line_color, fill=None)
        
        # Liquid fill (lower portion)
        liquid_points = [
            (x - int(6 * s), y + int(2 * s)),
            (x + int(6 * s), y + int(2 * s)),
            (x + int(9 * s), y + int(11 * s)),
            (x - int(9 * s), y + int(11 * s)),
        ]
        draw.polygon(liquid_points, fill=line_color)
        
        # Checkmark in liquid area (inverted color)
        check_color = 255 if line_color == 0 else 0
        draw.line([(x - int(4 * s), y + int(6 * s)),
                  (x - int(1 * s), y + int(9 * s)),
                  (x + int(4 * s), y + int(4 * s))],
                 fill=check_color, width=max(1, int(2 * s)))

    def _draw_positions_puzzles_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                                      size: int, line_color: int):
        """Draw a lightbulb icon for puzzles category.
        
        Represents the 'aha moment' of solving a puzzle.
        All coordinates stay within half = size // 2 from center.
        """
        s = size / 36.0  # Scale factor
        
        # Bulb body (circle at top)
        bulb_r = int(9 * s)
        bulb_cy = y - int(4 * s)
        draw.ellipse([x - bulb_r, bulb_cy - bulb_r,
                     x + bulb_r, bulb_cy + bulb_r],
                    outline=line_color, width=max(1, int(2 * s)))
        
        # Rays emanating from bulb (short lines)
        ray_inner = int(11 * s)
        ray_outer = int(14 * s)
        for angle_deg in [0, 45, 90, 135, 180, 225, 270, 315]:
            angle = math.radians(angle_deg)
            x1 = x + int(ray_inner * math.cos(angle))
            y1 = bulb_cy + int(ray_inner * math.sin(angle))
            x2 = x + int(ray_outer * math.cos(angle))
            y2 = bulb_cy + int(ray_outer * math.sin(angle))
            draw.line([(x1, y1), (x2, y2)], fill=line_color, width=max(1, int(1.5 * s)))
        
        # Base/screw (rectangular below bulb)
        base_top = bulb_cy + bulb_r - int(2 * s)
        base_w = int(5 * s)
        draw.rectangle([x - base_w, base_top,
                       x + base_w, y + int(12 * s)],
                      fill=line_color)
        
        # Horizontal lines on base for threading
        for i in range(2):
            line_y = base_top + int(4 * s) + i * int(4 * s)
            if line_y < y + int(12 * s):
                fill_color = 255 if line_color == 0 else 0
                draw.line([(x - base_w + 1, line_y), (x + base_w - 1, line_y)],
                         fill=fill_color, width=1)

    def _draw_positions_endgames_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                                       size: int, line_color: int):
        """Draw a king and pawn icon for endgames category.
        
        Shows simplified king + pawn representing classic endgame themes.
        All coordinates stay within half = size // 2 from center.
        """
        s = size / 36.0  # Scale factor
        
        # King on the left (smaller to fit both pieces)
        king_x = x - int(6 * s)
        
        # King cross
        draw.rectangle([king_x - int(1.5 * s), y - int(12 * s),
                       king_x + int(1.5 * s), y - int(7 * s)],
                      fill=line_color)
        draw.rectangle([king_x - int(3 * s), y - int(10 * s),
                       king_x + int(3 * s), y - int(8 * s)],
                      fill=line_color)
        
        # King body
        king_points = [
            (king_x - int(2 * s), y - int(6 * s)),
            (king_x + int(2 * s), y - int(6 * s)),
            (king_x + int(5 * s), y + int(9 * s)),
            (king_x - int(5 * s), y + int(9 * s)),
        ]
        draw.polygon(king_points, fill=line_color)
        
        # King base
        draw.rectangle([king_x - int(6 * s), y + int(9 * s),
                       king_x + int(6 * s), y + int(12 * s)],
                      fill=line_color)
        
        # Pawn on the right
        pawn_x = x + int(7 * s)
        
        # Pawn head
        head_r = int(3 * s)
        draw.ellipse([pawn_x - head_r, y - int(10 * s),
                     pawn_x + head_r, y - int(4 * s)],
                    fill=line_color)
        
        # Pawn body
        pawn_points = [
            (pawn_x - int(1.5 * s), y - int(4 * s)),
            (pawn_x + int(1.5 * s), y - int(4 * s)),
            (pawn_x + int(4 * s), y + int(9 * s)),
            (pawn_x - int(4 * s), y + int(9 * s)),
        ]
        draw.polygon(pawn_points, fill=line_color)
        
        # Pawn base
        draw.rectangle([pawn_x - int(5 * s), y + int(9 * s),
                       pawn_x + int(5 * s), y + int(12 * s)],
                      fill=line_color)

    def _draw_positions_custom_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                                     size: int, line_color: int):
        """Draw a pencil icon for custom positions category.
        
        Represents user-editable/customizable positions.
        All coordinates stay within half = size // 2 from center.
        """
        s = size / 36.0  # Scale factor
        
        # Simplified pencil - diagonal line with details
        # Pencil runs from bottom-left to top-right, within bounds
        
        # Main shaft (thick diagonal line)
        shaft_width = max(2, int(5 * s))
        draw.line([(x - int(10 * s), y + int(10 * s)),
                  (x + int(8 * s), y - int(8 * s))],
                 fill=line_color, width=shaft_width)
        
        # Pencil tip (small triangle at bottom-left)
        tip_points = [
            (x - int(12 * s), y + int(12 * s)),  # Point
            (x - int(9 * s), y + int(9 * s)),
            (x - int(11 * s), y + int(9 * s)),
        ]
        draw.polygon(tip_points, fill=line_color)
        
        # Eraser end (small filled area at top-right)
        fill_color = 255 if line_color == 0 else 0
        eraser_points = [
            (x + int(8 * s), y - int(8 * s)),
            (x + int(11 * s), y - int(11 * s)),
            (x + int(13 * s), y - int(9 * s)),
            (x + int(10 * s), y - int(6 * s)),
        ]
        draw.polygon(eraser_points, fill=fill_color, outline=line_color)
        
        # Plus sign in corner to indicate "add"
        plus_x = x + int(6 * s)
        plus_y = y + int(6 * s)
        plus_len = int(4 * s)
        draw.line([(plus_x - plus_len, plus_y), (plus_x + plus_len, plus_y)],
                 fill=line_color, width=max(1, int(2 * s)))
        draw.line([(plus_x, plus_y - plus_len), (plus_x, plus_y + plus_len)],
                 fill=line_color, width=max(1, int(2 * s)))

    # ========================================================================
    # Test Position Sub-menu Icons
    # ========================================================================

    def _draw_en_passant_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                               size: int, line_color: int):
        """Draw an en passant capture icon.
        
        Shows a pawn capturing diagonally with an arrow indicating the special move.
        """
        s = size / 36.0
        
        # Capturing pawn (left side)
        pawn_x = x - int(6 * s)
        head_r = int(3 * s)
        draw.ellipse([pawn_x - head_r, y - int(6 * s),
                     pawn_x + head_r, y],
                    fill=line_color)
        pawn_points = [
            (pawn_x - int(2 * s), y),
            (pawn_x + int(2 * s), y),
            (pawn_x + int(4 * s), y + int(10 * s)),
            (pawn_x - int(4 * s), y + int(10 * s)),
        ]
        draw.polygon(pawn_points, fill=line_color)
        
        # Target pawn (right, hollow/outlined to show it's captured)
        target_x = x + int(6 * s)
        draw.ellipse([target_x - head_r, y - int(1 * s),
                     target_x + head_r, y + int(5 * s)],
                    outline=line_color, width=max(1, int(1.5 * s)))
        
        # Diagonal arrow showing capture direction
        arrow_start = (x - int(2 * s), y - int(4 * s))
        arrow_end = (x + int(6 * s), y - int(10 * s))
        draw.line([arrow_start, arrow_end], fill=line_color, width=max(1, int(2 * s)))
        # Arrow head
        draw.polygon([
            arrow_end,
            (arrow_end[0] - int(3 * s), arrow_end[1] + int(2 * s)),
            (arrow_end[0] - int(1 * s), arrow_end[1] + int(4 * s)),
        ], fill=line_color)

    def _draw_castling_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                             size: int, line_color: int):
        """Draw a castling icon.
        
        Shows king and rook with arrows indicating the castle maneuver.
        """
        s = size / 36.0
        
        # King (left, simplified crown shape)
        king_x = x - int(6 * s)
        # Crown points
        crown_points = [
            (king_x - int(4 * s), y - int(4 * s)),
            (king_x - int(2 * s), y - int(10 * s)),
            (king_x, y - int(6 * s)),
            (king_x + int(2 * s), y - int(10 * s)),
            (king_x + int(4 * s), y - int(4 * s)),
        ]
        draw.polygon(crown_points, fill=line_color)
        # King body
        draw.rectangle([king_x - int(4 * s), y - int(4 * s),
                       king_x + int(4 * s), y + int(6 * s)],
                      fill=line_color)
        
        # Rook (right, battlements shape)
        rook_x = x + int(7 * s)
        # Battlements
        draw.rectangle([rook_x - int(5 * s), y - int(10 * s),
                       rook_x - int(3 * s), y - int(6 * s)],
                      fill=line_color)
        draw.rectangle([rook_x - int(1 * s), y - int(10 * s),
                       rook_x + int(1 * s), y - int(6 * s)],
                      fill=line_color)
        draw.rectangle([rook_x + int(3 * s), y - int(10 * s),
                       rook_x + int(5 * s), y - int(6 * s)],
                      fill=line_color)
        # Rook body
        draw.rectangle([rook_x - int(5 * s), y - int(6 * s),
                       rook_x + int(5 * s), y + int(6 * s)],
                      fill=line_color)
        
        # Arrows showing swap (curved or straight)
        # Arrow from king going right
        draw.line([(king_x + int(5 * s), y + int(8 * s)),
                  (rook_x - int(6 * s), y + int(8 * s))],
                 fill=line_color, width=max(1, int(1.5 * s)))
        # Arrow head pointing right
        draw.polygon([
            (rook_x - int(6 * s), y + int(8 * s)),
            (rook_x - int(9 * s), y + int(6 * s)),
            (rook_x - int(9 * s), y + int(10 * s)),
        ], fill=line_color)

    def _draw_promotion_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                              size: int, line_color: int):
        """Draw a pawn promotion icon.
        
        Shows a pawn with an upward arrow transforming into a queen crown.
        """
        s = size / 36.0
        
        # Pawn at bottom
        pawn_x = x
        head_r = int(3 * s)
        pawn_y_offset = int(4 * s)
        draw.ellipse([pawn_x - head_r, y + pawn_y_offset,
                     pawn_x + head_r, y + pawn_y_offset + int(6 * s)],
                    fill=line_color)
        pawn_points = [
            (pawn_x - int(2 * s), y + pawn_y_offset + int(6 * s)),
            (pawn_x + int(2 * s), y + pawn_y_offset + int(6 * s)),
            (pawn_x + int(4 * s), y + int(12 * s)),
            (pawn_x - int(4 * s), y + int(12 * s)),
        ]
        draw.polygon(pawn_points, fill=line_color)
        
        # Upward arrow
        arrow_x = x
        draw.line([(arrow_x, y + int(2 * s)), (arrow_x, y - int(6 * s))],
                 fill=line_color, width=max(1, int(2 * s)))
        # Arrow head
        draw.polygon([
            (arrow_x, y - int(8 * s)),
            (arrow_x - int(4 * s), y - int(4 * s)),
            (arrow_x + int(4 * s), y - int(4 * s)),
        ], fill=line_color)
        
        # Queen crown at top
        crown_y = y - int(10 * s)
        crown_points = [
            (x - int(6 * s), crown_y + int(4 * s)),
            (x - int(5 * s), crown_y),
            (x - int(2 * s), crown_y + int(3 * s)),
            (x, crown_y - int(2 * s)),
            (x + int(2 * s), crown_y + int(3 * s)),
            (x + int(5 * s), crown_y),
            (x + int(6 * s), crown_y + int(4 * s)),
        ]
        draw.polygon(crown_points, outline=line_color, fill=None)

    def _draw_timer_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                         size: int, line_color: int, checked: bool = False):
        """Draw a simple square icon with optional checkmark inside.
        
        Uses the same square outline as the default placeholder icon.
        When checked=True, a checkmark is drawn inside the square.
        
        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
            checked: Whether to draw a checkmark inside
        """
        half = size // 2
        left = x - half
        top = y - half
        right = x + half
        bottom = y + half
        
        # Square outline (same as default placeholder)
        draw.rectangle([left + 4, top + 4, right - 4, bottom - 4],
                      outline=line_color, width=2)
        
        if checked:
            # Draw checkmark inside the square
            # Scale checkmark to fit nicely within the square
            s = size / 36.0
            
            # Checkmark points - a V-shape centered in the square
            # Left point of check
            p1 = (x - int(8 * s), y)
            # Bottom point of check (the vertex)
            p2 = (x - int(2 * s), y + int(6 * s))
            # Right point of check (top right)
            p3 = (x + int(8 * s), y - int(6 * s))
            
            draw.line([p1, p2], fill=line_color, width=max(2, int(2.5 * s)))
            draw.line([p2, p3], fill=line_color, width=max(2, int(2.5 * s)))

    def _draw_bluetooth_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                             size: int, line_color: int):
        """Draw the Bluetooth rune (ᛒ), matching the web UI's Material glyph.

        The logo is a vertical stem whose top and bottom connect to two distinct
        right-hand vertices (the two angular "bumps"), plus two long diagonals
        that run from each right vertex across to the opposite left tip. Those
        diagonals cross the stem at its centre -- a single crossing, but two
        separate right vertices. The previous drawing collapsed both right arrows
        onto one mid-right point, which is why the right side looked pinched.

        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
        """
        s = size / 36.0
        line_width = max(2, int(2 * s))

        # Proportions follow the Material Bluetooth path (24px viewBox): the stem
        # spans ~+/-10 of a 12 centre, the right/left tips sit ~+/-6.5 out, and
        # the vertices sit ~+/-4.3 above/below centre. Scaled here to +/-14 stem.
        half_h = int(14 * s)      # stem half-height
        horiz = int(8 * s)        # horizontal reach of the vertices/tips
        vy = int(6 * s)           # vertical offset of the vertices from centre

        top = (x, y - half_h)
        bottom = (x, y + half_h)
        upper_right = (x + horiz, y - vy)
        lower_right = (x + horiz, y + vy)
        upper_left = (x - horiz, y - vy)
        lower_left = (x - horiz, y + vy)

        # Vertical stem.
        draw.line([top, bottom], fill=line_color, width=line_width)
        # Stem ends to the two distinct right vertices (the angular bumps).
        draw.line([top, upper_right], fill=line_color, width=line_width)
        draw.line([bottom, lower_right], fill=line_color, width=line_width)
        # Long diagonals from each right vertex to the opposite left tip; these
        # cross the stem at its centre and form the recognisable rune.
        draw.line([upper_right, lower_left], fill=line_color, width=line_width)
        draw.line([lower_right, upper_left], fill=line_color, width=line_width)

    def _draw_account_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                           size: int, line_color: int):
        """Draw an account/user icon (person silhouette).
        
        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
        """
        s = size / 36.0
        line_width = max(2, int(2 * s))
        
        # Head (circle at top)
        head_radius = int(6 * s)
        head_y = y - int(6 * s)
        draw.ellipse(
            [x - head_radius, head_y - head_radius,
             x + head_radius, head_y + head_radius],
            outline=line_color, width=line_width
        )
        
        # Body (arc/shoulders below)
        body_top = y + int(2 * s)
        body_bottom = y + int(14 * s)
        body_width = int(12 * s)
        
        # Draw shoulders as an arc
        draw.arc(
            [x - body_width, body_top,
             x + body_width, body_bottom + int(10 * s)],
            start=180, end=0,
            fill=line_color, width=line_width
        )

    def _draw_players_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                           size: int, line_color: int):
        """Draw a players icon (two person silhouettes side by side).

        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
        """
        s = size / 36.0
        line_width = max(2, int(2 * s))
        
        # Offset for left and right person
        offset = int(8 * s)
        
        # Left person (slightly smaller, appears behind)
        left_x = x - offset
        head_radius_left = int(5 * s)
        head_y_left = y - int(5 * s)
        draw.ellipse(
            [left_x - head_radius_left, head_y_left - head_radius_left,
             left_x + head_radius_left, head_y_left + head_radius_left],
            outline=line_color, width=line_width
        )
        body_top_left = y + int(2 * s)
        body_bottom_left = y + int(12 * s)
        body_width_left = int(9 * s)
        draw.arc(
            [left_x - body_width_left, body_top_left,
             left_x + body_width_left, body_bottom_left + int(8 * s)],
            start=180, end=0,
            fill=line_color, width=line_width
        )
        
        # Right person (slightly larger, appears in front)
        right_x = x + offset
        head_radius_right = int(5 * s)
        head_y_right = y - int(5 * s)
        draw.ellipse(
            [right_x - head_radius_right, head_y_right - head_radius_right,
             right_x + head_radius_right, head_y_right + head_radius_right],
            outline=line_color, width=line_width
        )
        body_top_right = y + int(2 * s)
        body_bottom_right = y + int(12 * s)
        body_width_right = int(9 * s)
        draw.arc(
            [right_x - body_width_right, body_top_right,
             right_x + body_width_right, body_bottom_right + int(8 * s)],
            start=180, end=0,
            fill=line_color, width=line_width
        )

    def _draw_lichess_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                           size: int, line_color: int):
        """Draw the Lichess logo (simplified knight with flame).
        
        The Lichess logo is a stylized knight chess piece. This draws
        a simplified version suitable for small e-paper display.
        
        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
        """
        s = size / 36.0
        line_width = max(2, int(2 * s))
        
        # Draw a simplified knight head facing left (like Lichess logo)
        # Base/platform
        base_y = y + int(12 * s)
        base_left = x - int(10 * s)
        base_right = x + int(10 * s)
        draw.line([(base_left, base_y), (base_right, base_y)], 
                  fill=line_color, width=line_width)
        
        # Neck (vertical line from base)
        neck_top = y - int(4 * s)
        neck_x = x + int(2 * s)
        draw.line([(neck_x, base_y), (neck_x, neck_top)],
                  fill=line_color, width=line_width)
        
        # Head (angled line for snout)
        snout_x = x - int(10 * s)
        snout_y = y - int(2 * s)
        draw.line([(neck_x, neck_top), (snout_x, snout_y)],
                  fill=line_color, width=line_width)
        
        # Mane/top curve
        mane_top = y - int(12 * s)
        draw.line([(neck_x, neck_top), (x, mane_top)],
                  fill=line_color, width=line_width)
        draw.line([(x, mane_top), (x + int(6 * s), y - int(6 * s))],
                  fill=line_color, width=line_width)
        
        # Ear
        ear_x = x + int(4 * s)
        ear_y = y - int(10 * s)
        draw.line([(x, mane_top), (ear_x, ear_y - int(4 * s))],
                  fill=line_color, width=line_width)
        
        # Eye (small dot)
        eye_x = x - int(2 * s)
        eye_y = y - int(4 * s)
        eye_r = max(1, int(1.5 * s))
        draw.ellipse([eye_x - eye_r, eye_y - eye_r, eye_x + eye_r, eye_y + eye_r],
                     fill=line_color)
    
    def _draw_checkbox_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                            size: int, line_color: int, checked: bool):
        """Draw a checkbox icon (empty box or box with checkmark).
        
        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
            checked: If True, draw checkmark inside the box
        """
        s = size / 36.0
        line_width = max(2, int(2 * s))
        
        # Box dimensions
        box_half = int(12 * s)
        left = x - box_half
        top = y - box_half
        right = x + box_half
        bottom = y + box_half
        
        # Draw the box outline
        draw.rectangle([left, top, right, bottom], outline=line_color, width=line_width)
        
        if checked:
            # Draw checkmark inside the box
            # Checkmark goes from bottom-left area to top-right area
            check_margin = int(4 * s)
            # Start point (lower left)
            p1_x = left + check_margin
            p1_y = y + int(2 * s)
            # Middle point (bottom of checkmark)
            p2_x = x - int(2 * s)
            p2_y = bottom - check_margin
            # End point (upper right)
            p3_x = right - check_margin
            p3_y = top + check_margin
            
            # Draw the two lines of the checkmark
            draw.line([(p1_x, p1_y), (p2_x, p2_y)], fill=line_color, width=line_width)
            draw.line([(p2_x, p2_y), (p3_x, p3_y)], fill=line_color, width=line_width)
    
    def _draw_display_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                           size: int, line_color: int):
        """Draw a display/monitor icon.
        
        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
        """
        s = size / 36.0
        line_width = max(2, int(2 * s))
        
        # Monitor screen (rectangle)
        screen_width = int(24 * s)
        screen_height = int(16 * s)
        screen_left = x - screen_width // 2
        screen_right = x + screen_width // 2
        screen_top = y - int(10 * s)
        screen_bottom = screen_top + screen_height
        
        draw.rectangle([screen_left, screen_top, screen_right, screen_bottom],
                      outline=line_color, width=line_width)
        
        # Stand (vertical line from bottom of screen)
        stand_top = screen_bottom
        stand_bottom = y + int(8 * s)
        draw.line([(x, stand_top), (x, stand_bottom)], fill=line_color, width=line_width)
        
        # Base (horizontal line at bottom)
        base_width = int(12 * s)
        base_y = stand_bottom
        draw.line([(x - base_width // 2, base_y), (x + base_width // 2, base_y)],
                  fill=line_color, width=line_width)

    def _draw_cast_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                        size: int, line_color: int):
        """Draw a Chromecast/cast icon (TV with wireless signal).

        Shows a rounded rectangle (TV screen) with wireless arc signals
        emanating from the bottom-left corner, representing casting.

        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
        """
        s = size / 36.0
        line_width = max(2, int(2 * s))

        # TV/screen rectangle (rounded corners simulated with rectangle)
        screen_width = int(28 * s)
        screen_height = int(20 * s)
        screen_left = x - screen_width // 2
        screen_right = x + screen_width // 2
        screen_top = y - screen_height // 2
        screen_bottom = y + screen_height // 2

        draw.rectangle([screen_left, screen_top, screen_right, screen_bottom],
                      outline=line_color, width=line_width)

        # Wireless signal arcs from bottom-left corner
        # Origin of the signal
        signal_x = screen_left + int(4 * s)
        signal_y = screen_bottom - int(4 * s)

        # Draw concentric arcs (3 arcs for wireless signal)
        for i, radius in enumerate([int(4 * s), int(8 * s), int(12 * s)]):
            # Draw arc in bottom-left quadrant (270 to 360 degrees = bottom-right quarter)
            bbox = [signal_x - radius, signal_y - radius,
                    signal_x + radius, signal_y + radius]
            # Arc from 270 degrees (pointing up) to 360 degrees (pointing right)
            draw.arc(bbox, start=270, end=360, fill=line_color, width=line_width)

        # Small filled circle at signal origin
        dot_r = max(1, int(2 * s))
        draw.ellipse([signal_x - dot_r, signal_y - dot_r,
                     signal_x + dot_r, signal_y + dot_r],
                    fill=line_color)

    def _draw_info_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                        size: int, line_color: int):
        """Draw an info icon (circle with 'i' inside).

        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
        """
        s = size / 36.0
        line_width = max(2, int(2 * s))

        # Outer circle
        radius = int(14 * s)
        draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                    outline=line_color, width=line_width)

        # Dot above the 'i' stem
        dot_y = y - int(6 * s)
        dot_r = max(2, int(2.5 * s))
        draw.ellipse([x - dot_r, dot_y - dot_r, x + dot_r, dot_y + dot_r],
                    fill=line_color)

        # 'i' stem (vertical line below the dot)
        stem_top = y - int(1 * s)
        stem_bottom = y + int(8 * s)
        draw.line([(x, stem_top), (x, stem_bottom)], fill=line_color, width=line_width)

    def _draw_download_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                            size: int, line_color: int):
        """Draw a download icon (arrow pointing down into tray).

        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
        """
        s = size / 36.0
        line_width = max(2, int(2 * s))

        # Arrow stem (vertical line)
        arrow_top = y - int(10 * s)
        arrow_mid = y + int(4 * s)
        draw.line([(x, arrow_top), (x, arrow_mid)], fill=line_color, width=line_width)

        # Arrow head (V shape pointing down)
        arrow_width = int(6 * s)
        draw.line([(x - arrow_width, arrow_mid - arrow_width), (x, arrow_mid)],
                 fill=line_color, width=line_width)
        draw.line([(x + arrow_width, arrow_mid - arrow_width), (x, arrow_mid)],
                 fill=line_color, width=line_width)

        # Tray (U shape at bottom)
        tray_top = y + int(6 * s)
        tray_bottom = y + int(12 * s)
        tray_width = int(10 * s)
        # Left side
        draw.line([(x - tray_width, tray_top), (x - tray_width, tray_bottom)],
                 fill=line_color, width=line_width)
        # Bottom
        draw.line([(x - tray_width, tray_bottom), (x + tray_width, tray_bottom)],
                 fill=line_color, width=line_width)
        # Right side
        draw.line([(x + tray_width, tray_top), (x + tray_width, tray_bottom)],
                 fill=line_color, width=line_width)

    def _draw_play_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                        size: int, line_color: int):
        """Draw a play icon (filled right-pointing triangle).

        Used for PLAY / start actions. Shares the "play" id with the web SVG.

        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
        """
        s = size / 36.0
        # A slightly left-offset triangle reads as centered because its
        # visual mass sits behind the tip.
        half_h = int(11 * s)
        left = x - int(8 * s)
        right = x + int(12 * s)
        draw.polygon(
            [(left, y - half_h), (left, y + half_h), (right, y)],
            fill=line_color,
            outline=line_color,
        )

    def _draw_update_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                          size: int, line_color: int):
        """Draw an update icon (circular refresh arrows).

        Used for software-update entries. Shares the "update" id with the web
        SVG. Drawn as a near-complete ring with a gap and an arrowhead so it
        reads as a refresh symbol on the 1-bit display.

        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
        """
        s = size / 36.0
        line_width = max(2, int(2 * s))
        r = int(11 * s)
        # Arc from 40deg around to -210deg, leaving a gap at the top-right for
        # the arrowhead.
        draw.arc([x - r, y - r, x + r, y + r], start=40, end=300,
                 fill=line_color, width=line_width)
        # Arrowhead at the arc's start (around 40deg, upper-right).
        import math
        a = math.radians(40)
        tip_x = x + int(r * math.cos(a))
        tip_y = y + int(r * math.sin(a))
        head = max(3, int(5 * s))
        draw.polygon(
            [
                (tip_x + head, tip_y - head // 2),
                (tip_x - head, tip_y - head),
                (tip_x, tip_y + head),
            ],
            fill=line_color,
            outline=line_color,
        )

    def _draw_undo_icon(self, draw: ImageDraw.Draw, x: int, y: int,
                        size: int, line_color: int):
        """Draw an undo icon (curved arrow pointing back/left).

        Used for takeback actions. Shares the "undo" id with the web SVG.

        Args:
            draw: ImageDraw object
            x: X center position
            y: Y center position
            size: Icon size in pixels
            line_color: Line color (0=black, 255=white)
        """
        s = size / 36.0
        line_width = max(2, int(2 * s))
        r = int(10 * s)
        # Upper arc curving from the left around the top to the right.
        draw.arc([x - r, y - r, x + r, y + r], start=180, end=20,
                 fill=line_color, width=line_width)
        # Arrowhead at the left end pointing down-left (the "back" direction).
        left_x = x - r
        head = max(3, int(5 * s))
        draw.polygon(
            [
                (left_x - head, y),
                (left_x + head, y - head),
                (left_x + head, y + head),
            ],
            fill=line_color,
            outline=line_color,
        )
