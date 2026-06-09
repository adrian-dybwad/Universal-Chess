"""
Splash screen widget displayed on startup.

Displays the knight logo with "UNIVERSAL" text below,
and an updateable message at the bottom.
"""

from PIL import Image
from .framework.widget import Widget
from .text import TextWidget, Justify
from .status_bar import STATUS_BAR_HEIGHT
from typing import Optional, Tuple

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


# Module-level knight logo and mask, set by application at startup
_knight_logo: Optional[Tuple[Image.Image, Image.Image]] = None


def set_knight_logo(logo: Image.Image, mask: Image.Image) -> None:
    """Set the module-level knight logo and mask.
    
    Called once at application startup to provide the logo.
    
    Args:
        logo: PIL Image of the knight logo
        mask: PIL Image mask for transparency
    """
    global _knight_logo
    _knight_logo = (logo, mask)


class SplashScreen(Widget):
    """Splash screen widget with knight logo and updateable centered message.
    
    Displays the knight logo centered at the top, with "UNIVERSAL" text below,
    and a customizable message at the bottom.
    
    The message can be updated after creation using set_message().
    Text is automatically centered horizontally using TextWidget with Justify.CENTER.
    Supports multi-line text with wrapping.
    
    This is a modal widget - when present, only this widget is rendered.
    """
    
    # SplashScreen is modal - when present, only it is rendered
    is_modal = True
    
    # Layout configuration
    LOGO_SIZE = 100  # Size of the knight logo
    LOGO_Y = 10  # Y position for logo (from top of widget)
    UNIVERSAL_Y = 120  # Y position for "UNIVERSAL" text
    TEXT_MARGIN = 4  # Margin on each side
    TEXT_Y = 170  # Y position for message text (below logo)
    TEXT_HEIGHT = 88  # Height for 4 lines of text at font size 18
    
    def __init__(self, update_callback, message: str = "Press [OK]", background_shade: int = 4,
                 leave_room_for_status_bar: bool = True,
                 logo: Image.Image = None, logo_mask: Image.Image = None):
        """Initialize splash screen widget.
        
        Args:
            update_callback: Callback to trigger display updates. Must not be None.
            message: Initial message to display
            background_shade: Dithered background shade 0-16 (default 4 = ~25% grey)
            leave_room_for_status_bar: If True, start below status bar; if False, use full screen
            logo: Optional knight logo image. If None, uses module-level logo.
            logo_mask: Optional mask for logo transparency.
        """
        if leave_room_for_status_bar:
            y_pos = STATUS_BAR_HEIGHT
            height = 296 - STATUS_BAR_HEIGHT
        else:
            y_pos = 0
            height = 296
        super().__init__(0, y_pos, 128, height, update_callback, background_shade=background_shade)
        self.message = message
        
        # Use provided logo or module-level logo
        if logo is not None:
            self._logo = logo
            self._logo_mask = logo_mask
        elif _knight_logo is not None:
            self._logo, self._logo_mask = _knight_logo
        else:
            log.error("No knight logo provided and none set at module level")
            self._logo = Image.new("1", (self.LOGO_SIZE, self.LOGO_SIZE), 255)
            self._logo_mask = None
        
        # Calculate text widget dimensions with margins for centering
        text_width = self.width - (self.TEXT_MARGIN * 2)
        
        # Create child TextWidgets - they use parent's handler so parent controls updates
        self._universal_text = TextWidget(
            x=0, y=0, width=self.width, height=28,
            update_callback=self._handle_child_update,
            text="UNIVERSAL", font_size=24, justify=Justify.CENTER, transparent=True
        )
        
        self._text_widget = TextWidget(
            x=0, y=0, width=text_width, height=self.TEXT_HEIGHT,
            update_callback=self._handle_child_update,
            text=message, font_size=18, justify=Justify.CENTER, wrapText=True
        )
    
    def _handle_child_update(self, full: bool = False, immediate: bool = False):
        """Handle update requests from child widgets by forwarding to parent callback."""
        return self._update_callback(full, immediate)
    
    def set_message(self, message: str):
        """Update the splash screen message and trigger a re-render.
        
        Only requests a display update if the message actually changes.
        Also logs the message prominently for startup visibility.
        
        Args:
            message: New message to display (will be centered)
        """
        if message == self.message:
            return
        self.message = message
        # Update text widget without triggering its own update (we'll do one update)
        self._text_widget.text = message
        self._text_widget._invalidate_caches()
        
        # Log prominently so startup messages are visible in logs
        log.info("=" * 60)
        log.info(f"[Startup] {message}")
        log.info("=" * 60)
        
        self.invalidate_and_update()
    
    def render(self, sprite: Image.Image) -> None:
        """Render the splash screen with knight logo, UNIVERSAL text, and message.
        
        Uses TextWidget for all text rendering.
        """
        # Draw dithered background
        self.draw_background_on_sprite(sprite)
        
        # Draw knight logo centered horizontally with transparency
        logo_x = (self.width - self.LOGO_SIZE) // 2
        if self._logo_mask:
            sprite.paste(self._logo, (logo_x, self.LOGO_Y), self._logo_mask)
        else:
            sprite.paste(self._logo, (logo_x, self.LOGO_Y))
        
        # Draw "UNIVERSAL" text directly onto the sprite
        self._universal_text.draw_on(sprite, 0, self.UNIVERSAL_Y)
        
        # Draw message text directly onto the sprite
        self._text_widget.draw_on(sprite, self.TEXT_MARGIN, self.TEXT_Y)


def show_fullscreen_splash(manager, message: str, timeout: float = 5.0) -> bool:
    """Render a full-screen modal splash on the given panel manager.

    Replaces whatever widgets are currently on the panel with a single
    SplashScreen, then waits (up to ``timeout``) for the render to complete so the
    caller can rely on the frame reaching the e-paper before proceeding.

    The manager is injected rather than imported so this stays usable from any app
    state and unit-testable. It MUST be the low-level epaper Manager that owns the
    panel (``board.display_manager``): the game-level DisplayManager implements
    none of the widget API (add_widget / clear_widgets / update) and forwards those
    calls to the panel manager, so passing it here would raise AttributeError and
    silently render nothing.

    Args:
        manager: Panel manager exposing clear_widgets/add_widget/update. May be
            None, in which case nothing is rendered and False is returned, so
            callers need not guard.
        message: Text to show on the splash.
        timeout: Seconds to wait for the render promise to resolve.

    Returns:
        True if the splash was rendered, False if no manager was available or
        rendering failed.
    """
    if manager is None:
        return False
    try:
        manager.clear_widgets(addStatusBar=False)
        promise = manager.add_widget(
            SplashScreen(manager.update, message=message,
                         leave_room_for_status_bar=False)
        )
        if promise:
            promise.result(timeout=timeout)
        return True
    except Exception as e:
        log.debug(f"[Splash] Failed to show splash '{message}': {e}")
        return False
