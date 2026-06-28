"""
About widget for displaying app information and support QR code.

Displays a QR code for support, app name, version, and a dismiss instruction.
This is a full-screen modal widget that blocks until user dismisses it.
"""

import contextlib
import threading
from PIL import Image, ImageDraw
from .framework.widget import Widget
from typing import Optional

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


class AboutWidget(Widget):
    """Full-screen modal widget displaying app information and support QR code.
    
    Shows:
    - Title at top
    - QR code (centered)
    - App name and version
    - Dismiss instruction at bottom
    
    The widget blocks until dismissed via wait_for_dismiss() or timeout.
    """
    
    is_modal = True
    
    # Layout constants
    TITLE_Y = 15
    QR_SIZE = 100
    QR_Y = 35
    APP_NAME_Y = 150
    APP_SUBTITLE_Y = 165
    VERSION_Y = 185
    WARNING_Y = 205
    INSTRUCTION_Y = 280
    
    def __init__(self, update_callback,
                 qr_image: Optional[Image.Image] = None,
                 version: str = "",
                 background_shade: int = 0):
        """Initialize the about widget.
        
        Args:
            update_callback: Callback to trigger display updates. Must not be None.
            qr_image: Optional QR code image. If None, displays text fallback.
            version: Application version string.
            background_shade: Background shade 0-16 (0=white, 16=black).
        """
        super().__init__(0, 0, 128, 296, update_callback, background_shade=background_shade)
        self._qr_image = qr_image
        self._version = version
        self._dismissed = threading.Event()
        self._font_loader = None
    
    def _get_font_loader(self):
        """Lazy-load the font loader."""
        if self._font_loader is None:
            from universalchess.resources import ResourceLoader
            from universalchess.paths import RESOURCES_DIR, USER_RESOURCES_DIR
            self._font_loader = ResourceLoader(RESOURCES_DIR, USER_RESOURCES_DIR)
        return self._font_loader
    
    def render(self, sprite: Image.Image) -> None:
        """Render the about widget onto the sprite.
        
        Args:
            sprite: Sprite image to render onto (0,0 is top-left of widget).
        """
        # Draw background
        self.draw_background_on_sprite(sprite)
        
        draw = ImageDraw.Draw(sprite)
        loader = self._get_font_loader()
        
        # Load fonts
        title_font = loader.get_font(12)
        version_font = loader.get_font(10)
        
        # Draw title
        draw.text((64, self.TITLE_Y), "Get Support", 
                  font=title_font, fill=0, anchor="mm")
        
        # Draw QR code or fallback text
        if self._qr_image:
            # Resize QR if needed and convert to 1-bit
            qr = self._qr_image.resize((self.QR_SIZE, self.QR_SIZE))
            if qr.mode != "1":
                qr = qr.convert("1")
            
            qr_x = (self.width - self.QR_SIZE) // 2
            sprite.paste(qr, (qr_x, self.QR_Y))
        else:
            # Fallback: display URL as text
            draw.text((64, 85), "github.com/", 
                      font=version_font, fill=0, anchor="mm")
            draw.text((64, 100), "EdNekebno/", 
                      font=version_font, fill=0, anchor="mm")
            draw.text((64, 115), "DGTCentaurMods", 
                      font=version_font, fill=0, anchor="mm")
        
        # Draw app name
        draw.text((64, self.APP_NAME_Y), "DGTCentaur", 
                  font=title_font, fill=0, anchor="mm")
        draw.text((64, self.APP_SUBTITLE_Y), "Mods", 
                  font=title_font, fill=0, anchor="mm")
        
        # Draw version if provided
        if self._version:
            draw.text((64, self.VERSION_Y), f"v{self._version}", 
                      font=version_font, fill=0, anchor="mm")
        
        # Draw incomplete shutdown warning if detected. The main module may not
        # be importable (e.g. widget previews) or may lack the attribute.
        with contextlib.suppress(ImportError, AttributeError):
            import universalchess.main as universal_module
            if universal_module.incomplete_shutdown:
                draw.text((64, self.WARNING_Y), "Incomplete shutdown", 
                          font=version_font, fill=0, anchor="mm")
        
        # Draw dismiss instruction
        draw.text((64, self.INSTRUCTION_Y), "Press any button", 
                  font=version_font, fill=0, anchor="mm")
    
    def dismiss(self) -> None:
        """Dismiss the about widget.
        
        Called when user presses a button to close the widget.
        """
        self._dismissed.set()
    
    def wait_for_dismiss(self, timeout: float = 30.0) -> bool:
        """Wait for the widget to be dismissed.
        
        Blocks until dismiss() is called or timeout expires.
        
        Args:
            timeout: Maximum seconds to wait (default 30).
        
        Returns:
            True if dismissed by user, False if timeout expired.
        """
        return self._dismissed.wait(timeout=timeout)
    
    def stop(self) -> None:
        """Stop the widget and release any waiting threads."""
        self._dismissed.set()
        super().stop()
