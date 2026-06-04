"""
Update status widget.

Displays update availability in the status bar:
- Hidden when no update available
- Down arrow icon when update is ready to install
- Animated when downloading
"""

from PIL import Image, ImageDraw
from .framework.widget import Widget

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


class UpdateStatusWidget(Widget):
    """Update status indicator for the status bar.
    
    Displays a small download icon when an update is available or pending.
    
    States:
    - Hidden: No update available
    - Static down arrow: Update ready to install
    - Animated: Currently downloading
    
    Args:
        x: X position in status bar
        y: Y position in status bar
        size: Icon size in pixels (default 16)
        update_callback: Callback to trigger display updates. Must not be None.
    """
    
    def __init__(self, x: int, y: int, size: int, update_callback):
        super().__init__(x, y, size, size, update_callback)
        self._size = size
        self._has_update = False
        self._has_pending = False
        self._is_downloading = False
        self._animation_frame = 0
        self.visible = False
        
        # Register with update service
        self._register_listener()
    
    def _register_listener(self) -> None:
        """Register as a listener with the update service."""
        try:
            from universalchess.services.update_service import get_update_service, UpdateEvent
            service = get_update_service()
            service.add_listener(self._on_update_event)
            
            # Check initial state
            status = service.get_status_dict()
            self._has_update = status.get("available_version") is not None
            self._has_pending = status.get("has_pending_update", False)
            self._is_downloading = status.get("is_downloading", False)
            self.visible = self._has_update or self._has_pending or self._is_downloading
            
            log.debug(f"[UpdateStatusWidget] Registered: update={self._has_update}, pending={self._has_pending}")
        except Exception as e:
            log.warning(f"[UpdateStatusWidget] Could not register with update service: {e}")
    
    def _on_update_event(self, event, message: str) -> None:
        """Handle update service events."""
        from universalchess.services.update_service import UpdateEvent
        
        if event == UpdateEvent.UPDATE_AVAILABLE:
            self._has_update = True
            self._is_downloading = False
            self.visible = True
        elif event == UpdateEvent.UP_TO_DATE:
            self._has_update = False
            self._has_pending = False
            self.visible = False
        elif event == UpdateEvent.DOWNLOADING:
            self._is_downloading = True
            self.visible = True
        elif event == UpdateEvent.DOWNLOAD_COMPLETE:
            self._is_downloading = False
            self._has_pending = True
            self.visible = True
        elif event == UpdateEvent.DOWNLOAD_FAILED:
            self._is_downloading = False
            # Keep visible if we still have an available update
        elif event == UpdateEvent.INSTALL_COMPLETE:
            self._has_update = False
            self._has_pending = False
            self.visible = False
        
        self.invalidate_and_update()
    
    def stop(self) -> None:
        """Unregister from update service."""
        try:
            from universalchess.services.update_service import get_update_service
            service = get_update_service()
            service.remove_listener(self._on_update_event)
        except Exception:
            pass
    
    def _draw_download_icon(self, draw: ImageDraw.Draw):
        """Draw a download arrow icon."""
        s = self._size
        margin = 2
        
        # Arrow pointing down
        mid_x = s // 2
        arrow_top = margin + 2
        arrow_bottom = s - margin - 2
        arrow_width = s // 3
        
        # Draw arrow shaft
        shaft_width = 2
        draw.rectangle(
            [mid_x - shaft_width // 2, arrow_top, 
             mid_x + shaft_width // 2, arrow_bottom - 3],
            fill=0
        )
        
        # Draw arrow head (triangle)
        draw.polygon([
            (mid_x, arrow_bottom),  # Bottom point
            (mid_x - arrow_width, arrow_bottom - arrow_width),  # Left
            (mid_x + arrow_width, arrow_bottom - arrow_width),  # Right
        ], fill=0)
        
        # Draw horizontal line at bottom (download target)
        line_y = s - margin
        draw.line([(margin, line_y), (s - margin, line_y)], fill=0, width=1)
    
    def _draw_downloading_animation(self, draw: ImageDraw.Draw):
        """Draw animated downloading indicator."""
        # Same icon but with animation dots
        self._draw_download_icon(draw)
        
        # Add animated dots below the arrow
        dot_y = self._size - 4
        dot_count = 3
        dot_spacing = self._size // (dot_count + 1)
        
        # Only show some dots based on frame
        active_dots = (self._animation_frame % 4)
        for i in range(min(active_dots, dot_count)):
            x = dot_spacing * (i + 1)
            draw.ellipse([(x - 1, dot_y - 1), (x + 1, dot_y + 1)], fill=0)
        
        self._animation_frame += 1
    
    def render(self, sprite: Image.Image) -> None:
        """Render the update status icon onto the sprite.
        
        Args:
            sprite: Pre-sized image to draw onto
        """
        if not self.visible:
            return
        
        draw = ImageDraw.Draw(sprite)
        
        if self._is_downloading:
            self._draw_downloading_animation(draw)
        else:
            self._draw_download_icon(draw)

