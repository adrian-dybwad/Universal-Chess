"""
Base widget class for ePaper display.
"""

from abc import ABC
from PIL import Image, ImageChops
from typing import Optional, TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .scheduler import Scheduler

import logging
log = logging.getLogger(__name__)
#log.setLevel(logging.INFO)


# 8x8 Bayer matrix for ordered dithering threshold values (0-63)
# This provides smoother gradients than 4x4 and less obvious tiling patterns
_BAYER_8x8 = [
    [ 0, 32,  8, 40,  2, 34, 10, 42],
    [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44,  4, 36, 14, 46,  6, 38],
    [60, 28, 52, 20, 62, 30, 54, 22],
    [ 3, 35, 11, 43,  1, 33,  9, 41],
    [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47,  7, 39, 13, 45,  5, 37],
    [63, 31, 55, 23, 61, 29, 53, 21],
]


def _generate_dither_pattern(shade: int) -> list:
    """Generate an 8x8 dither pattern for a given shade level.
    
    Args:
        shade: Shade level 0-16 (0=white, 16=black)
        
    Returns:
        8x8 list of 0s (white) and 1s (black)
    """
    # Map shade 0-16 to threshold 0-64
    # shade 0 = threshold 0 (all white)
    # shade 16 = threshold 64 (all black)
    threshold = shade * 4  # 0, 4, 8, 12, ... 64
    
    pattern = []
    for row in _BAYER_8x8:
        pattern_row = []
        for val in row:
            # Pixel is black if Bayer value is less than threshold
            pattern_row.append(1 if val < threshold else 0)
        pattern.append(pattern_row)
    return pattern


# Pre-generate patterns for shade levels 0-16
DITHER_PATTERNS = {shade: _generate_dither_pattern(shade) for shade in range(17)}


class Widget(ABC):
    """Base class for all display widgets."""
    
    # Class-level flag indicating if this widget type is modal.
    # When a modal widget is present, only it is rendered.
    is_modal: bool = False
    
    def __init__(self, x: int, y: int, width: int, height: int, 
                 update_callback: Callable[[bool, bool], object],
                 background_shade: int = 0):
        """Initialize a widget.
        
        Args:
            x: X position on display
            y: Y position on display
            width: Widget width in pixels
            height: Widget height in pixels
            update_callback: Callback to trigger display updates. Must not be None.
                            Accepts 'full' and 'immediate' boolean parameters and returns a Future.
            background_shade: Dithered background shade 0-16 (0=white, 8=50% gray, 16=black)
        
        Raises:
            ValueError: If update_callback is None
        """
        if update_callback is None:
            raise ValueError(f"{self.__class__.__name__}: update_callback must not be None")
        
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.visible = True  # Whether the widget should be rendered by the Manager
        self._background_shade = max(0, min(16, background_shade))
        self._cached_sprite: Optional[Image.Image] = None  # Cached rendered image for fast blit
        # Cached RED overlay sprite for three-color mode. Mirrors _cached_sprite
        # but holds the widget's red mask (0 = red, 255 = not red). Only built for
        # widgets that override render_red(); None means "no red contribution".
        self._cached_red_sprite: Optional[Image.Image] = None
        self._scheduler: Optional['Scheduler'] = None
        self._update_callback: Callable[[bool], object] = update_callback
        log.debug(f"Widget.__init__(): Created {self.__class__.__name__} instance id={id(self)} at ({x}, {y}) size {width}x{height}")
    
    def set_scheduler(self, scheduler: 'Scheduler') -> None:
        """Set the scheduler for this widget to trigger updates."""
        self._scheduler = scheduler
        log.debug(f"Widget.set_scheduler(): {self.__class__.__name__} id={id(self)} scheduler set")
    
    def set_update_callback(self, callback: Callable[[bool, bool], object]) -> None:
        """Set a callback to trigger Manager.update() when widget state changes.
        
        The callback should accept 'full' and 'immediate' boolean parameters and return a Future.
        This allows widgets to trigger full update cycles that render all widgets.
        
        Composite widgets should override this to propagate the callback to child widgets.
        
        Args:
            callback: The update callback (full, immediate) -> Future. Must not be None.
            
        Raises:
            ValueError: If callback is None
        """
        if callback is None:
            raise ValueError(f"{self.__class__.__name__}: update_callback must not be None")
        self._update_callback = callback
        log.debug(f"Widget.set_update_callback(): {self.__class__.__name__} id={id(self)} update callback set")
            
    def get_scheduler(self) -> Optional['Scheduler']:
        """Get the scheduler for this widget."""
        return self._scheduler
    
    def request_update(self, full: bool = False, forced: bool = False, immediate: bool = False):
        """Request a display update.
        
        This method should be called by widgets when their state changes
        and they need the display to refresh. It calls Manager.update() which:
        1. Renders all widgets to the framebuffer
        2. Submits the complete framebuffer to the scheduler
        
        If the widget is not visible and forced is False, the request is ignored
        since hidden widgets are not rendered and would cause unnecessary update cycles.
        
        Args:
            full: If True, force a full refresh instead of partial refresh.
            forced: If True, ignore visibility check (used by show/hide to update display).
            immediate: If True, wake scheduler immediately to bypass batching delay.
                      Use for time-sensitive UI like menu navigation.
        
        Returns:
            Future: A Future that completes when the display refresh finishes.
            Returns None if widget is hidden.
        
        Note:
            Widgets should NOT call the scheduler directly. The Manager must
            render all widgets first before submitting to ensure consistent state.

            request_update() only triggers the (global) refresh; it does NOT
            invalidate this widget's sprite cache. For the common case where the
            widget's own content changed, use invalidate_and_update(), which
            pairs invalidate_cache() with this call. Keep them separate when
            either is needed alone: invalidate_cache() to defer the refresh, or
            request_update() to refresh without re-rendering this widget.
        """
        # Ignore update requests from hidden widgets (unless forced)
        if not self.visible and not forced:
            log.debug(f"Widget.request_update(): {self.__class__.__name__} id={id(self)} ignored (widget is hidden)")
            return None
        
        if full:
            log.warning(f"Widget.request_update(): {self.__class__.__name__} requesting FULL refresh (will cause flashing)")
        else:
            log.debug(f"Widget.request_update(): {self.__class__.__name__} id={id(self)} requesting partial update")
        
        return self._update_callback(full, immediate)

    def invalidate_and_update(self, full: bool = False, forced: bool = False, immediate: bool = False):
        """Invalidate this widget's sprite cache, then request a display refresh.

        Convenience for the common case where a widget's OWN content changed and
        it needs to be re-rendered and shown. Equivalent to calling
        invalidate_cache() followed by request_update(full, forced, immediate).

        Invalidation happens first, before request_update()'s visibility check,
        so the cache is cleared even if the request is suppressed for a hidden,
        non-forced widget (it then re-renders fresh when next shown).

        Use the primitives directly when only one is needed:
        - invalidate_cache() alone to batch several state changes before a single
          coordinated refresh (e.g. the chess board's reveal setters).
        - request_update() alone to refresh without re-rendering this widget
          (e.g. forwarding a child widget's change).

        Args:
            full: If True, force a full refresh instead of partial.
            forced: If True, ignore the visibility check (used by show/hide).
            immediate: If True, wake the scheduler immediately (bypass batching).

        Returns:
            Future: request_update()'s result (None if the widget is hidden and
            not forced).
        """
        self.invalidate_cache()
        return self.request_update(full=full, forced=forced, immediate=immediate)
    
    def set_background_shade(self, shade: int) -> None:
        """Set the background shade level.
        
        Args:
            shade: Grayscale level 0-16 (0=white, 8=50% gray, 16=black)
        """
        shade = max(0, min(16, shade))
        if shade != self._background_shade:
            self._background_shade = shade
            self.invalidate_and_update()
    
    def invalidate_cache(self) -> None:
        """Invalidate the cached sprites, forcing re-render on next draw.
        
        Subclasses should call this when their state changes and they need
        to be re-rendered. This is more efficient than re-rendering immediately
        as multiple state changes can be batched into a single render.

        Clears BOTH the black/white sprite and the red overlay sprite: a state
        change that alters a widget's content (e.g. a new check square) usually
        affects its red mask too, and a stale red sprite would keep showing the
        previous highlight on a three-color panel.
        """
        self._cached_sprite = None
        self._cached_red_sprite = None
    
    def draw_background_on_sprite(self, sprite: Image.Image) -> None:
        """Draw dithered background pattern onto the sprite image.
        
        Draws the widget's dithered background pattern onto the sprite.
        Called by render() implementations before drawing content.
        
        Uses an 8x8 Bayer matrix for ordered dithering, which provides
        smoother gradients and less obvious tiling than 4x4 patterns.
        
        Args:
            sprite: The widget's sprite image to draw the background onto.
        """
        if self._background_shade == 0:
            # Pure white background - already white from Image.new()
            return
        
        pattern = DITHER_PATTERNS.get(self._background_shade, DITHER_PATTERNS[0])
        pixels = sprite.load()
        for y in range(self.height):
            pattern_row = pattern[y % 8]
            for x in range(self.width):
                if pattern_row[x % 8] == 1:
                    pixels[x, y] = 0  # Black pixel
                # White pixels already set from Image.new()
    
    def draw_on(self, canvas: Image.Image, draw_x: int, draw_y: int) -> None:
        """Draw the widget onto the canvas using sprite caching.
        
        If the sprite cache is valid, pastes the cached image (fast blit).
        If cache is invalidated, calls render() to generate a new sprite,
        caches it, then pastes it.
        
        This is called by the Manager during display updates.
        Thread-safe: captures sprite reference to avoid race with invalidate_cache().
        
        Args:
            canvas: Target canvas image to draw onto.
            draw_x: X coordinate on canvas where widget starts.
            draw_y: Y coordinate on canvas where widget starts.
        """
        # Capture sprite reference to avoid race condition with background threads
        # calling invalidate_cache() between the check and the paste
        sprite = self._cached_sprite
        if sprite is None:
            # Cache miss - render to new sprite
            sprite = Image.new('1', (self.width, self.height), 255)
            self.render(sprite)
            self._cached_sprite = sprite
            log.debug(f"Widget.draw_on(): {self.__class__.__name__} cache miss, rendered new sprite")
        
        # Fast blit from cache to canvas
        canvas.paste(sprite, (draw_x, draw_y))
    
    def render(self, sprite: Image.Image) -> None:
        """Render the widget content onto the sprite image.
        
        Subclasses should implement this to draw their content. The sprite is
        pre-sized to the widget dimensions and pre-filled with white.
        
        Typical implementation:
            1. Call self.draw_background_on_sprite(sprite) if using dithered background
            2. Draw content using PIL ImageDraw
        
        Widgets that override draw_on() entirely (e.g., for transparency or
        special compositing) don't need to implement render().
        
        Args:
            sprite: The widget's sprite image to render onto (0,0 is top-left of widget).
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement render() or override draw_on()"
        )

    def render_red(self, sprite: Image.Image) -> None:
        """Render this widget's RED overlay onto the sprite (three-color mode).

        The sprite is a 1-bit mask, pre-sized to the widget and pre-filled white
        (255 = not red). Subclasses that want to highlight content in red paint
        those pixels black (0 = red); the value 0 is the red channel, mirroring
        the black=0 convention of the black/white plane.

        The default is a no-op: a widget contributes no red. ``draw_red_on``
        detects an un-overridden ``render_red`` and skips it entirely, so plain
        widgets cost nothing on a three-color panel and never leak red onto
        unrelated content.

        Args:
            sprite: The widget's red-mask sprite (0,0 is top-left of widget).
        """
        return

    def _paints_red(self) -> bool:
        """Whether this widget overrides render_red (i.e. can contribute red)."""
        return type(self).render_red is not Widget.render_red

    def draw_red_on(self, canvas: Image.Image, draw_x: int, draw_y: int) -> None:
        """Composite this widget's red overlay onto the shared red canvas.

        Additive compositing: only the widget's red pixels (0) are written to the
        canvas; not-red areas (255) leave the canvas untouched so a lower widget's
        red is never erased by an upper widget that simply has no red there. This
        mirrors transparent compositing and is why a plain ``paste`` (which would
        overwrite the whole region) must not be used.

        No-op for widgets that do not override ``render_red`` (the common case),
        and sprite-cached exactly like ``draw_on`` for fast repeated blits.

        Args:
            canvas: The shared red-mask canvas (0 = red, 255 = not red).
            draw_x: X coordinate on the canvas where the widget starts.
            draw_y: Y coordinate on the canvas where the widget starts.
        """
        if not self._paints_red():
            return

        sprite = self._cached_red_sprite
        if sprite is None:
            sprite = Image.new('1', (self.width, self.height), 255)
            self.render_red(sprite)
            self._cached_red_sprite = sprite

        # Paste solid red (0) only where the sprite is red. The mask must be
        # non-zero where red, so invert the sprite (red 0 -> mask 255). A 4-tuple
        # box is used (not a 2-tuple) so PIL takes the region size from the box
        # rather than inferring it from the mask via isImageType -- the latter
        # breaks when a test has left a second PIL module in sys.modules (see
        # test_resources_sprites' note on PIL-mock pollution).
        mask = ImageChops.invert(sprite)
        canvas.paste(0, (draw_x, draw_y, draw_x + self.width, draw_y + self.height), mask)
    
    def show(self) -> None:
        """Show the widget (make it visible).
        
        When visible, the widget will be rendered by the Manager.
        Triggers a display update to reflect the change.
        """
        if not self.visible:
            self.visible = True
            log.info(f"Widget.show(): {self.__class__.__name__} id={id(self)} now visible")
            self.invalidate_and_update(forced=True)
    
    def hide(self) -> None:
        """Hide the widget (make it invisible).
        
        When hidden, the widget will not be rendered by the Manager.
        The widget remains in the display manager and continues any
        background processing (e.g., analysis), but its region on the
        display will be left for other widgets or background.
        Triggers a display update to reflect the change.
        """
        if self.visible:
            self.visible = False
            log.info(f"Widget.hide(): {self.__class__.__name__} id={id(self)} now hidden")
            self.invalidate_and_update(forced=True)
    
    def stop(self) -> None:
        """Stop the widget and perform cleanup tasks.
        
        This method should be overridden by widgets that have background threads,
        timers, or other resources that need cleanup. The default implementation
        does nothing.
        
        This method is called by Manager.shutdown() to ensure proper cleanup
        of all widgets before the display is shut down.
        """
        log.debug(f"Widget.stop(): {self.__class__.__name__} id={id(self)} stop() called")