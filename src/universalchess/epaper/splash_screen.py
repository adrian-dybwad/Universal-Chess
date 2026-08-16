"""
Splash screen widget displayed on startup.

Displays the knight logo with "UNIVERSAL" text below,
and an updateable message at the bottom.
"""

import threading
from PIL import Image
from .framework.widget import Widget
from .text import TextWidget, Justify
from .status_bar import STATUS_BAR_HEIGHT
from typing import Callable, Optional, Tuple

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
    
    # Layout configuration.
    #
    # The logo is the full knight piece, which is portrait (taller than wide), so
    # it gets a tall band: LOGO_HEIGHT drives the vertical space while the width
    # follows the source aspect and stays within LOGO_MAX_WIDTH (it is narrower
    # than the band is tall). UNIVERSAL/message positions sit just below the band.
    # The whole stack (8 + 140 + text) must fit the 280px status-bar variant.
    LOGO_HEIGHT = 140  # Height of the knight logo band
    LOGO_MAX_WIDTH = 100  # Horizontal footprint the logo is centered within
    LOGO_Y = 8  # Y position for logo (from top of widget)
    UNIVERSAL_Y = 154  # Y position for "UNIVERSAL" text (below the logo band)
    TEXT_MARGIN = 4  # Margin on each side
    TEXT_Y = 186  # Y position for message text (below "UNIVERSAL")
    TEXT_HEIGHT = 110  # Height for 5 lines of text at font size 18 (296 - TEXT_Y)
    DISMISS_TEXT_HEIGHT = 72  # Leave a band at the bottom for "Press any button"
    INSTRUCTION_HEIGHT = 16
    DISMISS_INSTRUCTION = "Press any button"
    DISMISS_TIMEOUT_SECONDS = 30.0
    # Optional byline shown under "UNIVERSAL" (only when a tagline is supplied,
    # i.e. the boot/idle and shutdown screens). When present the message is
    # pushed below it. Sized for the wrapped byline at font 16.
    TAGLINE_Y = 182
    TAGLINE_HEIGHT = 56

    # Optional battery indicator, shown below a single-line message (e.g. the
    # shutdown "Press [>]" prompt). Kept small so it fits beneath the byline +
    # message within the 296px screen; its Y is computed per-instance from the
    # message position (see __init__) rather than pinned to TEXT_Y.
    BATTERY_W = 30
    BATTERY_H = 15

    def __init__(self, update_callback, message: str = "Press [OK]", background_shade: int = 4,
                 leave_room_for_status_bar: bool = True,
                 logo: Image.Image = None, logo_mask: Image.Image = None,
                 show_battery: bool = False, tagline: Optional[str] = None,
                 dismissible: bool = False):
        """Initialize splash screen widget.
        
        Args:
            update_callback: Callback to trigger display updates. Must not be None.
            message: Initial message to display
            background_shade: Dithered background shade 0-16 (default 4 = ~25% grey)
            leave_room_for_status_bar: If True, start below status bar; if False, use full screen
            logo: Optional knight logo image. If None, uses module-level logo.
            logo_mask: Optional mask for logo transparency.
            show_battery: If True, draw the current battery level (icon + percentage)
                below the message. Used by the shutdown prompt so the user sees the
                charge state before the board sleeps.
            tagline: Optional byline drawn under "UNIVERSAL" (e.g. the boot screen).
                Injected as text so the widget stays free of the i18n catalog. When
                set, the message is pushed down to leave room for it.
            dismissible: When True, draw a "Press any button" instruction and
                unblock ``wait_for_dismiss`` on any key. Used for error splashes
                so the user can read the message instead of a non-selectable menu
                row that cannot be dismissed.
        """
        if leave_room_for_status_bar:
            y_pos = STATUS_BAR_HEIGHT
            height = 296 - STATUS_BAR_HEIGHT
        else:
            y_pos = 0
            height = 296
        super().__init__(0, y_pos, 128, height, update_callback, background_shade=background_shade)
        self.message = message
        self._show_battery = show_battery
        self._dismissible = dismissible
        self._dismissed = threading.Event()
        
        # Use provided logo or module-level logo
        if logo is not None:
            self._logo = logo
            self._logo_mask = logo_mask
        elif _knight_logo is not None:
            self._logo, self._logo_mask = _knight_logo
        else:
            log.error("No knight logo provided and none set at module level")
            self._logo = Image.new("1", (self.LOGO_MAX_WIDTH, self.LOGO_HEIGHT), 255)
            self._logo_mask = None
        
        # Calculate text widget dimensions with margins for centering
        text_width = self.width - (self.TEXT_MARGIN * 2)
        
        # Create child TextWidgets - they use parent's handler so parent controls updates
        self._universal_text = TextWidget(
            x=0, y=0, width=self.width, height=28,
            update_callback=self._handle_child_update,
            text="UNIVERSAL", font_size=24, justify=Justify.CENTER, transparent=True
        )
        
        message_height = self.DISMISS_TEXT_HEIGHT if dismissible else self.TEXT_HEIGHT
        self._text_widget = TextWidget(
            x=0, y=0, width=text_width, height=message_height,
            update_callback=self._handle_child_update,
            text=message, font_size=18, justify=Justify.CENTER, wrapText=True
        )

        self._instruction_text = None
        if dismissible:
            from universalchess.i18n import t

            self._instruction_text = TextWidget(
                x=0, y=0, width=self.width, height=self.INSTRUCTION_HEIGHT,
                update_callback=self._handle_child_update,
                text=t("about.press_any_button") or self.DISMISS_INSTRUCTION,
                font_size=12, justify=Justify.CENTER, transparent=True
            )

        # Optional byline under "UNIVERSAL". Present on the boot/idle and shutdown
        # screens; when shown, the message starts below it, otherwise it keeps
        # TEXT_Y.
        self._tagline_text = None
        if tagline:
            self._tagline_text = TextWidget(
                x=0, y=0, width=text_width, height=self.TAGLINE_HEIGHT,
                update_callback=self._handle_child_update,
                text=tagline, font_size=16, justify=Justify.CENTER,
                transparent=True, wrapText=True
            )
            self._message_y = self.TAGLINE_Y + self.TAGLINE_HEIGHT
        else:
            self._message_y = self.TEXT_Y

        # Battery sits just under the (single-line) message; derive its Y from the
        # message position so it follows the byline when one is shown, and the
        # percentage sits just under the icon.
        self._battery_y = self._message_y + 24
        self._battery_percent_y = self._battery_y + self.BATTERY_H + 2

        # Percentage label beneath the battery icon, only built when needed.
        self._battery_percent_text = None
        if self._show_battery:
            self._battery_percent_text = TextWidget(
                x=0, y=0, width=self.width, height=16,
                update_callback=self._handle_child_update,
                text="", font_size=13, justify=Justify.CENTER, transparent=True
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
        
        # Draw knight logo centered horizontally with transparency. The logo is
        # portrait, so center on its actual width rather than a fixed square.
        logo_x = (self.width - self._logo.width) // 2
        if self._logo_mask:
            sprite.paste(self._logo, (logo_x, self.LOGO_Y), self._logo_mask)
        else:
            sprite.paste(self._logo, (logo_x, self.LOGO_Y))
        
        # Draw "UNIVERSAL" text directly onto the sprite
        self._universal_text.draw_on(sprite, 0, self.UNIVERSAL_Y)

        # Draw the optional byline between "UNIVERSAL" and the message.
        if self._tagline_text is not None:
            self._tagline_text.draw_on(sprite, self.TEXT_MARGIN, self.TAGLINE_Y)

        # Draw message text directly onto the sprite (below the byline when shown)
        self._text_widget.draw_on(sprite, self.TEXT_MARGIN, self._message_y)

        if self._instruction_text is not None:
            self._instruction_text.draw_on(
                sprite, 0, self.height - self.INSTRUCTION_HEIGHT
            )

        if self._show_battery:
            self._render_battery(sprite)

    def _render_battery(self, sprite: Image.Image) -> None:
        """Draw the current battery level (icon + percentage) below the message.

        Reads the latest battery state at render time rather than holding an
        observer: the shutdown splash is a one-shot frame drawn while subsystems
        are tearing down, so the last polled value is what should be shown.
        """
        from universalchess.state import get_system
        from .battery import render_battery

        state = get_system()
        level = state.battery_level
        percent = state.battery_percent
        charger_connected = state.charger_connected

        # level is 0-20; default to a half icon when unknown so the glyph still
        # reads as a battery, and show "--%" so the unknown state is explicit.
        icon_level = level if level is not None else 10
        battery_x = (self.width - self.BATTERY_W) // 2
        render_battery(sprite, battery_x, self._battery_y,
                       self.BATTERY_W, self.BATTERY_H, icon_level, charger_connected)

        if self._battery_percent_text is not None:
            self._battery_percent_text.text = "--%" if percent is None else f"{percent}%"
            self._battery_percent_text.draw_on(sprite, 0, self._battery_percent_y)

    def handle_key(self, key_id: object) -> bool:
        """Dismiss on any key when this splash is waiting for the user.

        Returns True so the key does not reach a menu underneath. A
        non-dismissible splash (boot, shutdown, waiting) ignores keys.
        """
        if not self._dismissible:
            return False
        self.dismiss()
        return True

    def dismiss(self) -> None:
        """Unblock ``wait_for_dismiss`` (any key, or the idle timeout caller)."""
        self._dismissed.set()

    def wait_for_dismiss(self, timeout: float = DISMISS_TIMEOUT_SECONDS) -> bool:
        """Block until a key dismisses this splash, or ``timeout`` seconds elapse.

        Returns:
            True if dismissed by a key, False if the idle window expired.
        """
        return self._dismissed.wait(timeout=timeout)

    def stop(self) -> None:
        """Release a thread blocked in ``wait_for_dismiss`` when the widget is torn down."""
        self._dismissed.set()
        super().stop()


def show_fullscreen_splash(manager, message: str, timeout: float = 5.0,
                           show_battery: bool = False,
                           tagline: Optional[str] = None) -> bool:
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
        show_battery: When True, draw the current battery level below the message
            (used by the shutdown prompt).
        tagline: Optional byline drawn under "UNIVERSAL" (used by the shutdown
            prompt so the slogan appears there too).

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
                         leave_room_for_status_bar=False,
                         show_battery=show_battery, tagline=tagline)
        )
        if promise:
            promise.result(timeout=timeout)
        return True
    except Exception as e:
        log.debug(f"[Splash] Failed to show splash '{message}': {e}")
        return False


def show_dismissible_splash(
    manager,
    message: str,
    timeout: float = SplashScreen.DISMISS_TIMEOUT_SECONDS,
    bind_keys: Optional[Callable[[Optional[object]], None]] = None,
) -> bool:
    """Overlay a splash, block until any key (or idle timeout), then remove it.

    Does not clear the panel: the splash is modal, so the menu underneath stays
    in the widget list and is visible again after ``remove_widget``. Clearing
    would return the user to a blank panel.

    ``bind_keys`` is called with the widget so the application can route board
    keys to ``widget.handle_key``, and called with ``None`` in ``finally`` so a
    render failure cannot leave later keys swallowed. Without it the splash still
    waits out ``timeout``.

    Args:
        manager: Panel manager exposing add_widget/remove_widget/update. None
            renders nothing and returns False.
        message: Error (or other) copy to show.
        timeout: Seconds to wait for a key before closing.
        bind_keys: Optional ``widget -> None`` registrar; ``None`` unbinds.

    Returns:
        True if the splash was shown and the wait finished, False if there was
        no manager or rendering failed.
    """
    if manager is None:
        return False
    widget = None
    try:
        widget = SplashScreen(
            manager.update,
            message=message,
            leave_room_for_status_bar=False,
            dismissible=True,
        )
        if bind_keys is not None:
            bind_keys(widget)
        promise = manager.add_widget(widget)
        if promise:
            promise.result(timeout=5.0)
        widget.wait_for_dismiss(timeout=timeout)
        return True
    except Exception as e:
        log.debug(f"[Splash] Failed to show dismissible splash '{message}': {e}")
        return False
    finally:
        if bind_keys is not None:
            bind_keys(None)
        if widget is not None:
            try:
                manager.remove_widget(widget)
            except Exception as e:
                log.debug(f"[Splash] Failed to remove dismissible splash: {e}")
