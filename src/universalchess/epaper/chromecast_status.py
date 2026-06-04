"""
Chromecast status widget.

Displays the Chromecast streaming status in the status bar:
- No icon when idle (hidden)
- Outline icon when connecting/reconnecting
- Filled icon when streaming
- Icon with X when error
"""

from PIL import Image, ImageDraw
from .framework.widget import Widget

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from universalchess.state import get_chromecast as get_chromecast_state
from universalchess.state.chromecast import STATE_STREAMING, STATE_CONNECTING, STATE_RECONNECTING, STATE_ERROR


class ChromecastStatusWidget(Widget):
    """Chromecast status indicator widget.
    
    Args:
        x: X position in status bar
        y: Y position in status bar
        size: Icon size in pixels
        update_callback: Callback to trigger display updates. Must not be None.
    """
    
    def __init__(self, x: int, y: int, size: int, update_callback):
        super().__init__(x, y, size, size, update_callback)
        self._size = size
        self._state = get_chromecast_state()
        self._state.add_observer(self._on_state_changed)
        self.visible = self._state.is_active
    
    def _on_state_changed(self) -> None:
        """Called by the state when it changes."""
        self.visible = self._state.is_active
        self.invalidate_and_update()
    
    def stop(self) -> None:
        """Stop the widget (unregister from state).
        
        Note: This does NOT stop the service itself, just removes this
        widget as an observer.
        """
        try:
            self._state.remove_observer(self._on_state_changed)
        except Exception as e:
            log.debug(f"[ChromecastStatusWidget] Error removing observer: {e}")
    
    def _draw_cast_icon(self, draw: ImageDraw.Draw, filled: bool = True) -> None:
        """Draw a Chromecast-style cast icon onto sprite.
        
        The icon has 1px horizontal margin on left and right, no vertical margin.
        
        Args:
            draw: ImageDraw object for the sprite
            filled: If True, draw filled icon (streaming). If False, outline only.
        """
        # 1px margin on left and right only
        margin_h = 1
        icon_x = margin_h
        icon_w = self._size - 2 * margin_h
        icon_h = self._size  # Full height, no vertical margin
        
        # TV/monitor outline - positioned as fractions of icon dimensions
        tv_left = icon_x + max(1, icon_w // 14)
        tv_top = max(1, icon_h // 8)
        tv_right = icon_x + icon_w - max(1, icon_w // 14)
        tv_bottom = icon_h - max(2, icon_h // 4)
        
        # Draw TV outline
        draw.rectangle([tv_left, tv_top, tv_right, tv_bottom], fill=255, outline=0, width=1)
        
        # Draw wireless signal arcs (bottom-left corner of TV)
        arc_x = tv_left + max(1, icon_w // 7)
        arc_y = tv_bottom - max(1, icon_h // 8)
        
        # Arc radii as fractions of icon width
        small_radius = max(1, icon_w // 7)
        large_radius = max(2, icon_w // 4)
        
        if filled:
            # When streaming: filled arcs with thicker lines
            for radius in [small_radius, large_radius]:
                draw.arc([arc_x - radius, arc_y - radius, 
                         arc_x + radius, arc_y + radius],
                        start=180, end=270, fill=0, width=max(1, icon_w // 10))
            
            # Small dot at origin
            dot_r = max(1, icon_w // 14)
            draw.ellipse([arc_x - dot_r, arc_y - dot_r, arc_x + dot_r, arc_y + dot_r], fill=0)
        else:
            # When connecting/error: just outline arcs (thinner)
            for radius in [small_radius, large_radius]:
                draw.arc([arc_x - radius, arc_y - radius, 
                         arc_x + radius, arc_y + radius],
                        start=180, end=270, fill=0, width=1)
    
    def render(self, sprite: Image.Image) -> None:
        """Render the Chromecast status icon based on state."""
        draw = ImageDraw.Draw(sprite)
        
        # Sprite is pre-filled white
        
        # Query state directly (single source of truth)
        state = self._state.state
        
        if state == STATE_STREAMING:
            self._draw_cast_icon(draw, filled=True)
        elif state in (STATE_CONNECTING, STATE_RECONNECTING):
            self._draw_cast_icon(draw, filled=False)
        elif state == STATE_ERROR:
            self._draw_cast_icon(draw, filled=False)
            # Draw small X in upper-right of icon area
            margin_h = 1
            icon_x = margin_h
            icon_w = self._size - 2 * margin_h
            icon_h = self._size
            x1 = icon_x + (icon_w * 3) // 5
            y1 = icon_h // 8
            x2 = icon_x + icon_w - 1
            y2 = (icon_h * 7) // 16
            draw.line([x1, y1, x2, y2], fill=0, width=1)
            draw.line([x1, y2, x2, y1], fill=0, width=1)
