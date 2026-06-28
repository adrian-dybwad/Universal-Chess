"""
Text display widget.
"""

from PIL import Image, ImageDraw, ImageFont
from .framework.widget import Widget, DITHER_PATTERNS
from enum import Enum

from universalchess.resources import get_font


class Justify(Enum):
    """Text justification options."""
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


class TextWidget(Widget):
    """Text display widget with configurable background dithering, text wrapping, justification, and bold.
    
    Fonts can be provided directly via the `font` parameter, or loaded via the
    module-level resource loader if set.
    
    Performance optimizations:
    - Pre-rendered text sprites: text is rendered once and cached until settings change
    - Dirty flag tracking: only re-renders when text or settings actually change
    - Blit-based draw_on(): cached sprite is copied directly without re-rasterizing
    """
    
    def __init__(self, x: int, y: int, width: int, height: int, 
                 update_callback,
                 text: str = "", 
                 background: int = -1, font_size: int = 12, font: ImageFont.FreeTypeFont = None,
                 wrapText: bool = False, justify: Justify = Justify.LEFT,
                 transparent: bool = True, bold: bool = False):
        """
        Initialize text widget.
        
        Args:
            x: X position
            y: Y position
            width: Widget width
            height: Widget height
            update_callback: Callback to trigger display updates. Must not be None.
            text: Text to display
            background: Background dithering level (-1 to 5)
                -1 = transparent (default, inherits parent background)
                0 = no background (white)
                1 = very light dither (~17% black)
                2 = light dither (~33% black)
                3 = medium dither (~50% black, checkerboard)
                4 = heavy dither (~67% black)
                5 = solid black
            font_size: Font size in points (used if font not provided)
            font: Optional PIL ImageFont object. If provided, font_size is ignored.
            wrapText: If True, wrap text to fit within widget width and height
            justify: Text justification (Justify.LEFT, Justify.CENTER, or Justify.RIGHT)
            transparent: If True (default), background is transparent and text appears
                        over parent widget's pixels. Overrides background=-1.
            bold: If True, simulate bold by drawing text twice with 1px horizontal offset
        """
        super().__init__(x, y, width, height, update_callback)
        self.text = text
        self.transparent = transparent
        # If transparent is True, force background to -1
        if transparent:
            self.background = -1
        else:
            self.background = max(-1, min(5, background))
        self.font_size = font_size
        self.wrapText = wrapText
        self.justify = justify
        self.bold = bold
        self._font = font if font is not None else self._load_font()
        self._mask = None  # Cached mask image
        
        # Pre-rendered text sprite cache
        # Key: (text_color,) - we cache one sprite per text color used
        # This allows efficient draw_on() calls that just blit the cached image
        self._sprite_cache = {}  # {text_color: Image}
        self._sprite_cache_key = None  # Hash of settings that affect sprite rendering
    
    def _load_font(self):
        """Resolve the widget font via the app-wide ResourceLoader.

        Delegates to resources.get_font, which shares one loader and font cache
        across all widgets and falls back to PIL's default font when no loader
        has been registered.
        """
        return get_font(self.font_size)
    
    def _get_sprite_cache_key(self) -> tuple:
        """Get a hashable key representing all settings that affect sprite rendering.
        
        If this key changes, all cached sprites must be invalidated.
        """
        return (self.text, self.width, self.height, self.font_size, 
                self.wrapText, self.justify, self.bold)
    
    def _invalidate_caches(self) -> None:
        """Invalidate all cached data (sprites, masks, rendered images)."""
        self.invalidate_cache()  # Invalidate base class sprite cache
        self._mask = None
        self._sprite_cache.clear()
        self._sprite_cache_key = None
    
    def set_text(self, text: str) -> None:
        """Set the text to display."""
        if self.text != text:
            self.text = text
            self._invalidate_caches()
            self.request_update(full=False)
    
    def set_wrap_text(self, wrapText: bool) -> None:
        """Enable or disable text wrapping."""
        if self.wrapText != wrapText:
            self.wrapText = wrapText
            self._invalidate_caches()
            self.request_update(full=False)
    
    def set_background(self, background: int) -> None:
        """Set background dithering level (-1 to 5).
        
        Args:
            background: -1 for transparent, 0-5 for dithered backgrounds
        """
        background = max(-1, min(5, background))
        if self.background != background:
            self.background = background
            self.transparent = (background == -1)
            self._invalidate_caches()
            self.request_update(full=False)
    
    def set_transparent(self, transparent: bool) -> None:
        """Set whether the background is transparent.
        
        Args:
            transparent: If True, background is transparent (inherits parent)
        """
        if self.transparent != transparent:
            self.transparent = transparent
            if transparent:
                self.background = -1
            elif self.background == -1:
                self.background = 0  # Default to white if was transparent
            self._invalidate_caches()
            self.request_update(full=False)
    
    def set_justify(self, justify: Justify) -> None:
        """Set text justification."""
        if self.justify != justify:
            self.justify = justify
            self._invalidate_caches()
            self.request_update(full=False)
    
    def set_bold(self, bold: bool) -> None:
        """Set whether text is rendered bold.
        
        Bold is simulated by drawing text twice with 1px horizontal offset.
        
        Args:
            bold: If True, render text in bold
        """
        if self.bold != bold:
            self.bold = bold
            self._invalidate_caches()
            self.request_update(full=False)
    
    def _draw_text(self, draw: ImageDraw.Draw, x: int, y: int, text: str, fill: int) -> None:
        """Draw text with optional bold effect.
        
        Simulates bold by drawing text twice with 1px horizontal offset.
        
        Args:
            draw: ImageDraw object
            x: X position
            y: Y position
            text: Text to draw
            fill: Color value (0 or 255)
        """
        draw.text((x, y), text, font=self._font, fill=fill)
        if self.bold:
            draw.text((x + 1, y), text, font=self._font, fill=fill)
    
    def _get_text_width(self, text: str, draw: ImageDraw.Draw) -> int:
        """Get the width of text in pixels.
        
        Args:
            text: Text to measure
            draw: ImageDraw object for measuring
            
        Returns:
            Width in pixels
        """
        try:
            bbox = draw.textbbox((0, 0), text, font=self._font)
            return bbox[2] - bbox[0]
        except (AttributeError, ValueError):
            # AttributeError: older PIL versions without textbbox
            # ValueError: textbbox called with non-TrueType font (e.g., default bitmap font)
            return int(draw.textlength(text, font=self._font))
    
    def _get_x_position(self, text: str, draw: ImageDraw.Draw) -> int:
        """Calculate x position based on justification.
        
        Args:
            text: Text to position
            draw: ImageDraw object for measuring
            
        Returns:
            X position in pixels
        """
        if self.justify == Justify.LEFT:
            return 0
        
        text_width = self._get_text_width(text, draw)
        
        if self.justify == Justify.CENTER:
            return (self.width - text_width) // 2
        elif self.justify == Justify.RIGHT:
            return self.width - text_width
        
        return 0
    
    def _create_dither_pattern(self) -> Image.Image:
        """Create dither pattern based on background level.
        
        Uses an 8x8 Bayer matrix for ordered dithering.
        For transparent backgrounds (-1), returns a white image since
        the actual compositing is handled via get_mask().
        """
        img = Image.new("1", (self.width, self.height), 255)
        
        if self.background <= 0:
            # Transparent (-1) or white (0) - all white
            return img
        elif self.background == 5:
            # Solid black
            draw = ImageDraw.Draw(img)
            draw.rectangle([0, 0, self.width, self.height], fill=0)
            return img
        
        # Map background levels 1-4 to shade levels for Bayer patterns
        # Level 1: ~20% black (shade 3)
        # Level 2: ~38% black (shade 6)
        # Level 3: ~50% black (shade 8)
        # Level 4: ~69% black (shade 11)
        shade_map = {1: 3, 2: 6, 3: 8, 4: 11}
        shade = shade_map.get(self.background, 8)
        
        pattern = DITHER_PATTERNS.get(shade, DITHER_PATTERNS[0])
        pixels = img.load()
        
        for y in range(self.height):
            pattern_row = pattern[y % 8]
            for x in range(self.width):
                if pattern_row[x % 8] == 1:
                    pixels[x, y] = 0  # Black pixel
        
        return img
    
    def get_mask(self):
        """Get mask for transparent compositing.
        
        For transparent TextWidgets, returns a mask where text pixels are
        white (opaque) and background pixels are black (transparent).
        This allows the parent widget's background to show through.
        
        Returns:
            Image mask if transparent, None otherwise
        """
        if not self.transparent:
            return None
        
        # Create mask where text is white (opaque) and background is black (transparent)
        # We render the text to determine which pixels should be opaque
        if self._mask is None:
            self._mask = self._create_text_mask()
        return self._mask
    
    def _create_text_mask(self) -> Image.Image:
        """Create a mask image for transparent text compositing.
        
        Respects bold setting for consistent mask generation.
        
        Returns:
            1-bit image where white=opaque (text), black=transparent (background)
        """
        # Start with all black (transparent)
        mask = Image.new("1", (self.width, self.height), 0)
        draw = ImageDraw.Draw(mask)
        
        if self.wrapText:
            wrapped_lines = self._wrap_text(self.text, self.width)
            line_height = self.font_size + 2
            max_lines = max(1, self.height // line_height)
            for idx, line in enumerate(wrapped_lines[:max_lines]):
                y_pos = idx * line_height
                if y_pos + line_height > self.height:
                    break
                x_pos = self._get_x_position(line, draw)
                # Draw text in white (opaque) on mask, respecting bold
                self._draw_text(draw, x_pos, y_pos - 1, line, 255)
        else:
            x_pos = self._get_x_position(self.text, draw)
            self._draw_text(draw, x_pos, -1, self.text, 255)
        
        return mask
    
    def _wrap_text(self, text: str, max_width: int) -> list:
        """
        Wrap text to fit within max_width using the widget's font.
        
        Respects explicit newlines in the text - each newline starts a new line.
        Then wraps long lines to fit within max_width.
        
        Args:
            text: Text to wrap (may contain \\n for explicit line breaks)
            max_width: Maximum width in pixels
            
        Returns:
            List of wrapped text lines
        """
        if not text:
            return []
        
        temp_image = Image.new("1", (1, 1), 255)
        temp_draw = ImageDraw.Draw(temp_image)
        
        lines = []
        
        # First split by explicit newlines
        paragraphs = text.split('\n')
        
        for paragraph in paragraphs:
            # Handle empty paragraphs (consecutive newlines)
            if not paragraph.strip():
                lines.append("")
                continue
            
            # Wrap this paragraph
            words = paragraph.split()
            if not words:
                lines.append("")
                continue
            
            current = words[0]
            for word in words[1:]:
                candidate = f"{current} {word}"
                if temp_draw.textlength(candidate, font=self._font) <= max_width:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
            
            lines.append(current)
        
        return lines
    
    def _render_text_sprite(self, text_color: int) -> Image.Image:
        """Render text to a sprite image (no background, just text pixels).
        
        Creates a white image and draws text in the specified color.
        The resulting image can be blitted onto target images.
        
        Args:
            text_color: Text color (0=black, 255=white)
            
        Returns:
            Pre-rendered text sprite image
        """
        # Create white background image
        sprite = Image.new("1", (self.width, self.height), 255)
        draw = ImageDraw.Draw(sprite)
        
        if self.wrapText:
            wrapped_lines = self._wrap_text(self.text, self.width)
            line_height = self.font_size + 2
            max_lines = max(1, self.height // line_height)
            
            for idx, line in enumerate(wrapped_lines[:max_lines]):
                y_pos = idx * line_height
                if y_pos + line_height > self.height:
                    break
                x_pos = self._get_x_position(line, draw)
                self._draw_text(draw, x_pos, y_pos - 1, line, text_color)
        else:
            x_pos = self._get_x_position(self.text, draw)
            self._draw_text(draw, x_pos, -1, self.text, text_color)
        
        return sprite
    
    def _get_sprite(self, text_color: int) -> Image.Image:
        """Get cached text sprite, rendering if necessary.
        
        Sprites are cached per text_color and invalidated when any rendering
        setting changes (text, font, justification, etc.).
        
        Args:
            text_color: Text color (0=black, 255=white)
            
        Returns:
            Cached or newly rendered text sprite
        """
        # Check if cache is still valid
        current_key = self._get_sprite_cache_key()
        if self._sprite_cache_key != current_key:
            # Settings changed, invalidate all cached sprites
            self._sprite_cache.clear()
            self._sprite_cache_key = current_key
        
        # Check if we have a cached sprite for this color
        if text_color not in self._sprite_cache:
            self._sprite_cache[text_color] = self._render_text_sprite(text_color)
        
        return self._sprite_cache[text_color]
    
    def draw_on(self, target: Image.Image, x: int, y: int, text_color: int = None) -> None:
        """Draw text onto a target image using cached sprites.
        
        Uses pre-rendered text sprites for efficiency. The sprite is rendered
        once and cached until text or settings change. Subsequent calls just
        blit the cached image.
        
        Note: This method does NOT draw any background - it only draws the text.
        The caller is responsible for any background rendering.
        
        Args:
            target: Target image to draw onto
            x: X offset on target image
            y: Y offset on target image
            text_color: Text color (0=black, 255=white). If None, auto-determines
                       based on background setting.
        """
        # Determine text color if not specified
        if text_color is None:
            text_fill = 0 if self.background < 3 else 255
        else:
            text_fill = text_color
        
        # Get cached sprite (rendered on demand)
        sprite = self._get_sprite(text_fill)
        
        # Create mask for transparent blitting (text pixels only)
        # For black text on white: mask where sprite is black (text) = white (opaque)
        # For white text on white: we need the inverse
        if text_fill == 0:
            # Black text: mask where pixels are black (0)
            mask = Image.eval(sprite, lambda p: 255 if p == 0 else 0)
        else:
            # White text: mask where pixels are white (255) - but sprite bg is also white
            # We need to use the text mask instead
            mask = self._get_sprite_mask()
        
        # Blit sprite onto target with mask
        target.paste(sprite, (x, y), mask)
    
    def _get_sprite_mask(self) -> Image.Image:
        """Get mask for white text sprites.
        
        For white text, we can't distinguish text from background by color.
        We use the cached mask instead.
        
        Returns:
            Mask image where text pixels are white (opaque)
        """
        if self._mask is None:
            self._mask = self._create_text_mask()
        return self._mask
