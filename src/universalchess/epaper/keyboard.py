"""
Keyboard widget for e-paper display using chess board pieces as input.

This widget displays a virtual keyboard on the e-paper where each board square
corresponds to a character. Placing a piece on a square types that character.
The user lifts any piece and places it on the desired character square.

Usage:
    keyboard = KeyboardWidget(title="WiFi Password")
    board.display_manager.add_widget(keyboard)
    result = keyboard.wait_for_input()  # Returns password string or None
"""

from PIL import Image, ImageDraw, ImageFont
from .framework.widget import Widget
from typing import Optional, Callable, TYPE_CHECKING
import threading

if TYPE_CHECKING:
    from universalchess.resources import ResourceLoader

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


# Module-level resource loader, set by application at startup
_resource_loader: "ResourceLoader" = None


def set_resource_loader(loader: "ResourceLoader") -> None:
    """Set the module-level resource loader for fonts.
    
    Called once at application startup.
    """
    global _resource_loader
    _resource_loader = loader


# Display dimensions
DISPLAY_WIDTH = 128
DISPLAY_HEIGHT = 296


class KeyboardWidget(Widget):
    """Virtual keyboard widget using chess board pieces for input.
    
    Displays a character grid on the e-paper corresponding to board squares.
    Characters are typed by lifting and placing pieces on squares.
    
    Character Layout (8x8 grid, 64 characters per page):
        Page 1: a-z, 0-9, common symbols
        Page 2: A-Z, additional symbols
    
    Controls:
        - Place piece on square: Type character at that square
        - UP: Previous page
        - DOWN: Next page  
        - BACK: Delete last character (or cancel if empty)
        - TICK: Confirm input
        - PLAY: Cancel input
    
    Attributes:
        title: Display title/prompt
        text: Current input text
        current_page: Current character page (1 or 2)
    """
    
    # Character sets for each page (64 chars each, arranged as 8x8 grid)
    # Row 0 = rank 8 (top), Row 7 = rank 1 (bottom)
    # Col 0 = file a (left), Col 7 = file h (right)
    CHARS_PAGE1 = (
        "abcdefgh"  # Rank 8: a-h
        "ijklmnop"  # Rank 7: i-p
        "qrstuvwx"  # Rank 6: q-x
        "yz012345"  # Rank 5: y-z, 0-5
        "6789!@#$"  # Rank 4: 6-9, symbols
        "%^&*()-_"  # Rank 3: symbols
        "=+[]{}\\|"  # Rank 2: symbols
        ";':\",./<>"  # Rank 1: symbols
    )
    
    CHARS_PAGE2 = (
        "ABCDEFGH"  # Rank 8: A-H
        "IJKLMNOP"  # Rank 7: I-P
        "QRSTUVWX"  # Rank 6: Q-X
        "YZ`~    "  # Rank 5: Y-Z, backtick, tilde, spaces
        "        "  # Rank 4: spaces (reserved)
        "        "  # Rank 3: spaces (reserved)
        "        "  # Rank 2: spaces (reserved)
        "        "  # Rank 1: spaces (reserved)
    )
    
    def __init__(self, update_callback, title: str = "Enter Text", max_length: int = 64,
                 on_complete: Optional[Callable[[Optional[str]], None]] = None):
        """Initialize keyboard widget.
        
        Args:
            update_callback: Callback to trigger display updates. Must not be None.
            title: Title/prompt to display
            max_length: Maximum input length
            on_complete: Callback when input is complete (receives text or None)
        """
        super().__init__(0, 0, DISPLAY_WIDTH, DISPLAY_HEIGHT, update_callback)
        
        self.title = title
        self.max_length = max_length
        self.on_complete = on_complete
        
        self.text = ""
        self.current_page = 1
        self.max_pages = 2
        
        # Input state
        self._input_complete = threading.Event()
        self._result: Optional[str] = None
        self._cancelled = False
        
        # Load fonts
        self._font = None
        self._font_small = None
        self._font_tiny = None
        self._load_fonts()
    
    def _load_fonts(self):
        """Load fonts from module-level resource loader."""
        if _resource_loader is not None:
            self._font = _resource_loader.get_font(16)
            self._font_small = _resource_loader.get_font(12)
            self._font_tiny = _resource_loader.get_font(10)
        else:
            self._font = ImageFont.load_default()
            self._font_small = self._font
            self._font_tiny = self._font
    
    def _get_chars(self) -> str:
        """Get character set for current page."""
        if self.current_page == 1:
            return self.CHARS_PAGE1
        else:
            return self.CHARS_PAGE2
    
    def _field_to_char(self, field: int) -> Optional[str]:
        """Convert field index to character.
        
        Field indices: 0-63 where 0=a1, 1=b1, ..., 63=h8
        We need to map this to our character grid where row 0 is rank 8.
        
        Args:
            field: Board field index (0-63)
        
        Returns:
            Character at that position, or None if space/invalid
        """
        if field < 0 or field >= 64:
            return None
        
        # Convert field to row/col
        # field = rank * 8 + file, where rank 0 = rank 1 (bottom)
        file_idx = field % 8  # 0-7 for a-h
        rank_idx = field // 8  # 0-7 for rank 1-8
        
        # Our char grid has row 0 = rank 8, row 7 = rank 1
        # So we need to flip the rank
        grid_row = 7 - rank_idx
        grid_col = file_idx
        
        char_idx = grid_row * 8 + grid_col
        chars = self._get_chars()
        
        if char_idx < len(chars):
            char = chars[char_idx]
            if char != " ":
                return char
        
        return None
    
    def handle_key(self, key_id: int) -> bool:
        """Handle key press events.
        
        Args:
            key_id: Key identifier
        
        Returns:
            True if key was handled, False otherwise
        """
        # Import board module for key constants
        try:
            from universalchess.board import board
            Key = board.Key
        except ImportError:
            log.error("[Keyboard] Cannot import board module")
            return False
        
        if key_id == Key.BACK:
            if self.text:
                self.text = self.text[:-1]
                board.beep(board.SOUND_GENERAL)
                self.invalidate_cache()
                self.request_update(full=False)
            else:
                # Empty text - cancel
                self._cancelled = True
                self._result = None
                self._input_complete.set()
            return True
        
        elif key_id == Key.TICK:
            # Confirm input
            self._result = self.text
            self._input_complete.set()
            board.beep(board.SOUND_GENERAL)
            return True
        
        elif key_id == Key.UP:
            if self.current_page > 1:
                self.current_page -= 1
                board.beep(board.SOUND_GENERAL)
                self.invalidate_cache()
                self.request_update(full=False)
            return True
        
        elif key_id == Key.DOWN:
            if self.current_page < self.max_pages:
                self.current_page += 1
                board.beep(board.SOUND_GENERAL)
                self.invalidate_cache()
                self.request_update(full=False)
            return True
        
        elif key_id == Key.PLAY:
            # Cancel input
            self._cancelled = True
            self._result = None
            self._input_complete.set()
            return True
        
        return False
    
    def handle_field_event(self, field: int, piece_present: bool) -> bool:
        """Handle piece placement/removal events.
        
        A character is typed when a piece is placed on any square.
        The user lifts a piece from anywhere and places it on the character
        square they want to type.
        
        Args:
            field: Board field index (0-63)
            piece_present: True if piece now present, False if removed
        
        Returns:
            True if event was handled
        """
        if field < 0 or field >= 64:
            return False
        
        # Type character on piece placement (not removal)
        if piece_present:
            char = self._field_to_char(field)
            if char and len(self.text) < self.max_length:
                self.text += char
                try:
                    from universalchess.board import board
                    board.beep(board.SOUND_GENERAL)
                except ImportError:
                    pass
                self.invalidate_cache()
                self.request_update(full=False)
                return True
        
        return False
    
    def wait_for_input(self, timeout: float = 300.0) -> Optional[str]:
        """Wait for user to complete input.
        
        Blocks until user confirms or cancels, or timeout expires.
        
        Args:
            timeout: Maximum time to wait in seconds
        
        Returns:
            Input text if confirmed, None if cancelled or timeout
        """
        self._input_complete.wait(timeout=timeout)
        
        if self.on_complete:
            self.on_complete(self._result)
        
        return self._result
    
    def cancel(self):
        """Cancel input externally."""
        self._cancelled = True
        self._result = None
        self._input_complete.set()
    
    def render(self, sprite: Image.Image) -> None:
        """Render the keyboard widget onto the sprite."""
        draw = ImageDraw.Draw(sprite)
        
        # Draw background
        self.draw_background_on_sprite(sprite)
        
        # Title area (top)
        draw.text((4, 2), self.title[:16], font=self._font_small, fill=0)
        
        # Text input area
        input_y = 18
        draw.rectangle([2, input_y, self.width - 3, input_y + 22], outline=0, width=1)
        
        # Display text with cursor
        display_text = self.text
        if len(display_text) > 12:
            display_text = "..." + display_text[-9:]
        display_text += "_"
        draw.text((6, input_y + 3), display_text, font=self._font, fill=0)
        
        # Page indicator
        page_y = input_y + 26
        page_text = f"Page {self.current_page}/{self.max_pages}"
        draw.text((4, page_y), page_text, font=self._font_tiny, fill=0)
        draw.text((70, page_y), "UP/DOWN", font=self._font_tiny, fill=0)
        
        # Character grid (8x8)
        grid_y = page_y + 14
        cell_w = self.width // 8
        cell_h = 22
        
        chars = self._get_chars()
        
        for row in range(8):
            for col in range(8):
                char_idx = row * 8 + col
                char = chars[char_idx] if char_idx < len(chars) else " "
                
                cx = col * cell_w
                cy = grid_y + row * cell_h
                
                # Draw cell border
                draw.rectangle([cx, cy, cx + cell_w - 1, cy + cell_h - 1], outline=0)
                
                # Draw character (centered)
                if char != " ":
                    # Center the character in the cell
                    try:
                        bbox = self._font_small.getbbox(char)
                        char_w = bbox[2] - bbox[0]
                        char_h = bbox[3] - bbox[1]
                    except AttributeError:
                        char_w, char_h = 8, 12
                    
                    text_x = cx + (cell_w - char_w) // 2
                    text_y = cy + (cell_h - char_h) // 2 - 1
                    draw.text((text_x, text_y), char, font=self._font_small, fill=0)
        
        # Instructions at bottom
        inst_y = grid_y + 8 * cell_h + 2
        draw.text((2, inst_y), "Place piece: type", font=self._font_tiny, fill=0)
        draw.text((2, inst_y + 11), "BACK:del TICK:ok", font=self._font_tiny, fill=0)
        draw.text((2, inst_y + 22), "PLAY: cancel", font=self._font_tiny, fill=0)
