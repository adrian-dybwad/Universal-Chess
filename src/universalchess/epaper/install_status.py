"""
Engine install status widget.

Displays engine installation progress in the status bar:
- Hidden when no installation active
- Spinning/animated icon when installing
- Brief flash on completion
"""

import contextlib
from PIL import Image, ImageDraw
from .framework.widget import Widget

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


class InstallStatusWidget(Widget):
    """Engine install status indicator for the status bar.
    
    Displays a small wrench/gear icon with animation during installation.
    Shows queue count when multiple engines queued.
    
    Args:
        x: X position in status bar
        y: Y position in status bar
        size: Icon size in pixels (default 16)
        update_callback: Callback to trigger display updates. Must not be None.
    """
    
    def __init__(self, x: int, y: int, size: int, update_callback):
        super().__init__(x, y, size, size, update_callback)
        self._size = size
        self._engine_manager = None
        self._installing = False
        self._queue_count = 0
        self._current_engine = ""
        self._animation_frame = 0
        self.visible = False
        
        # Register with engine manager
        self._register_listener()
    
    def _register_listener(self) -> None:
        """Register as a progress listener with the engine manager."""
        try:
            from universalchess.managers.engine_manager import get_engine_manager
            self._engine_manager = get_engine_manager()
            self._engine_manager.add_progress_listener(self._on_progress)
            log.debug("[InstallStatusWidget] Registered with engine manager")
        except Exception as e:
            log.warning(f"[InstallStatusWidget] Could not register with engine manager: {e}")
    
    def _on_progress(self, engine_name: str, status: str, message: str) -> None:
        """Handle progress events from engine manager.
        
        Args:
            engine_name: Name of engine
            status: One of "queued", "installing", "completed", "failed", "cancelled"
            message: Progress message
        """
        log.debug(f"[InstallStatusWidget] Progress: {engine_name} {status}: {message}")
        
        # Update state based on status
        if status == "installing":
            self._installing = True
            self._current_engine = engine_name
            self.visible = True
            self._animation_frame = (self._animation_frame + 1) % 4
        elif status in ("completed", "failed", "cancelled"):
            # Check if more items in queue
            if self._engine_manager:
                queue = self._engine_manager.get_queue_status()
                self._queue_count = len(queue)
                if self._queue_count == 0:
                    self._installing = False
                    self.visible = False
        elif status == "queued":
            if self._engine_manager:
                queue = self._engine_manager.get_queue_status()
                self._queue_count = len(queue)
            if not self._installing:
                self.visible = True  # Show that something is queued
        
        self.invalidate_and_update()
    
    def stop(self) -> None:
        """Unregister from engine manager."""
        if self._engine_manager:
            try:
                self._engine_manager.remove_progress_listener(self._on_progress)
            except Exception as e:
                log.debug(f"[InstallStatusWidget] Error removing listener: {e}")
    
    def render(self, sprite: Image.Image) -> None:
        """Render the install status icon."""
        if not self.visible:
            return
        
        draw = ImageDraw.Draw(sprite)
        
        # Draw a simple gear/cog icon with animation
        self._draw_gear_icon(draw, self._installing, self._animation_frame)
        
        # Draw queue count badge if more than 1 queued
        if self._queue_count > 1:
            self._draw_queue_badge(draw, self._queue_count)
    
    def _draw_gear_icon(self, draw: ImageDraw.Draw, spinning: bool, frame: int) -> None:
        """Draw a gear/cog icon.
        
        Args:
            draw: ImageDraw instance
            spinning: If True, animate the gear
            frame: Animation frame (0-3)
        """
        size = self._size
        cx, cy = size // 2, size // 2
        outer_r = size // 2 - 2
        inner_r = size // 4
        
        # Rotation offset for animation
        rotation_offset = frame * 22.5 if spinning else 0  # 22.5 degrees per frame
        
        # Draw 8 teeth around the gear
        import math
        for i in range(8):
            angle = math.radians(i * 45 + rotation_offset)
            # Tooth position
            x1 = cx + int(inner_r * math.cos(angle))
            y1 = cy + int(inner_r * math.sin(angle))
            x2 = cx + int(outer_r * math.cos(angle))
            y2 = cy + int(outer_r * math.sin(angle))
            draw.line([(x1, y1), (x2, y2)], fill=0, width=2)
        
        # Draw center circle
        draw.ellipse([cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r], outline=0, width=1)
        
        # Draw small center dot
        dot_r = 2
        draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=0)
    
    def _draw_queue_badge(self, draw: ImageDraw.Draw, count: int) -> None:
        """Draw a small count badge in the corner.
        
        Args:
            draw: ImageDraw instance
            count: Queue count to display
        """
        # Small circle in bottom-right corner
        badge_r = 4
        bx = self._size - badge_r - 1
        by = self._size - badge_r - 1
        
        # Draw filled circle
        draw.ellipse([bx - badge_r, by - badge_r, bx + badge_r, by + badge_r], fill=0)
        
        # Draw count (just for counts 2-9, otherwise just show filled dot)
        if 2 <= count <= 9:
            # Use built-in font for tiny text. Skip the count display if font
            # loading/measurement fails rather than breaking the whole render.
            with contextlib.suppress(Exception):
                from PIL import ImageFont
                font = ImageFont.load_default()
                text = str(count)
                # Get text size using textbbox
                bbox = draw.textbbox((0, 0), text, font=font)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
                draw.text((bx - tw // 2, by - th // 2 - 1), text, fill=255, font=font)

