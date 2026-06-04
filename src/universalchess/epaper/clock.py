"""
Clock widget displaying current time.
"""

from PIL import Image, ImageDraw, ImageFont
from datetime import datetime
from .framework.widget import Widget
import os
import threading
import time
from typing import Optional

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


class ClockWidget(Widget):
    """Clock widget displaying current time with automatic updates."""
    
    def __init__(self, x: int, y: int, width: int, height: int, update_callback,
                 font_size: int = None, font_path: str = None,
                 show_seconds: bool = True):
        super().__init__(x, y, width, height, update_callback)
        self.show_seconds = show_seconds
        # Set format based on show_seconds
        if show_seconds:
            self.format = "%H:%M:%S"
        else:
            self.format = "%H:%M"
        self.font_size = font_size
        self.font_path = font_path
        self._font = None
        self._load_font()
        
        # Background update thread
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_rendered_time: Optional[str] = None
        self._stop_event = threading.Event()
        self._start_update_loop()
    
    def _start_update_loop(self) -> None:
        """Start the background update loop."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._update_loop, name="clock-widget", daemon=True)
        self._thread.start()
    
    def _stop_update_loop(self) -> None:
        """Stop the background update loop."""
        self._running = False
        self._stop_event.set()  # Signal the event to wake up any sleeping thread
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
    
    def stop(self) -> None:
        """Stop the widget and perform cleanup tasks."""
        self._stop_update_loop()
    
    def _update_loop(self) -> None:
        """Background loop that checks time every 0.1 seconds and triggers updates."""
        while self._running:
            try:
                now = datetime.now()
                current_time_str = now.strftime(self.format)
                
                # Check if time has changed
                if current_time_str != self._last_rendered_time:
                    self._last_rendered_time = current_time_str
                    self.invalidate_and_update()
                
                # Sleep for 0.1 seconds using interruptible wait
                # This allows the thread to stop quickly when requested
                if not self._stop_event.wait(timeout=0.1):
                    # Timeout occurred (normal case), clear the event for next iteration
                    self._stop_event.clear()
            except Exception as e:
                log.error(f"Error in clock update loop: {e}")
                # On error, also use interruptible sleep
                if not self._stop_event.wait(timeout=0.1):
                    self._stop_event.clear()
    
    
    def _load_font(self):
        """Load font with fallbacks."""
        if self._font is not None:
            return self._font
        
        # If font_path and font_size are specified, use them
        if self.font_path and self.font_size:
            if os.path.exists(self.font_path):
                try:
                    self._font = ImageFont.truetype(self.font_path, self.font_size)
                    return self._font
                except:
                    pass
        
        # Try default font paths if font_size is specified
        if self.font_size:
            font_paths = [
                '/opt/universalchess/resources/Font.ttc',
                'resources/Font.ttc',
                '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            ]
            for path in font_paths:
                if os.path.exists(path):
                    try:
                        self._font = ImageFont.truetype(path, self.font_size)
                        return self._font
                    except:
                        pass
        
        # Fallback to default font
        self._font = ImageFont.load_default()
        return self._font
    
    def render(self, sprite: Image.Image) -> None:
        """Render current time onto the sprite image."""
        draw = ImageDraw.Draw(sprite)
        now = datetime.now()
        time_str = now.strftime(self.format)
        
        # Update last rendered time
        self._last_rendered_time = time_str
        
        # Use cached font or load it
        if self._font is None:
            self._load_font()
        
        # Sprite is pre-filled white, just draw text
        draw.text((0, -1), time_str, font=self._font, fill=0)
