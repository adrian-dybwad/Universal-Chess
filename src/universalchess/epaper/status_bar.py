"""
Status bar widget displaying time, Chromecast, WiFi, Bluetooth, and battery icons.
"""

from PIL import Image
from .framework.widget import Widget
from .clock import ClockWidget
from .wifi_status import WiFiStatusWidget
from .bluetooth_status import BluetoothStatusWidget
from .battery import BatteryWidget
from .chromecast_status import ChromecastStatusWidget
from .install_status import InstallStatusWidget
from .update_status import UpdateStatusWidget
import os
from typing import List

# Status bar height constant
STATUS_BAR_HEIGHT = 16

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


class StatusBarWidget(Widget):
    """Status bar widget displaying time, Chromecast, WiFi, Bluetooth, and battery icons.
    
    Layout rules (128px wide, 16px tall):
    - All widgets are 16px tall (full height)
    - Clock: 2.5x wider than tall = 40px, starts at x=0
    - Chromecast: square = 16x16 (only visible when streaming)
    - WiFi: square = 16x16
    - Bluetooth: 3/4 as wide as tall = 12x16
    - Battery: 5/4 as wide as tall = 20x16, right-aligned at x=128
    
    Positions calculated from right edge:
    - Battery: x=108, ends at 128
    - Bluetooth: x=96
    - WiFi: x=80
    - Chromecast: x=64 (when visible)
    - Clock: x=0
    
    Each widget controls its own visibility based on its state.
    """
    
    # Positions calculated from right edge, all widgets 16px tall
    BATTERY_X = 108     # 20px wide, ends at 128
    BLUETOOTH_X = 96    # 12px wide
    WIFI_X = 80         # 16px wide
    CHROMECAST_X = 64   # 16px wide (only when streaming)
    UPDATE_X = 48       # 16px wide, for update indicator
    INSTALL_X = 44      # 16px wide, in gap between clock and chromecast (overlaps when both visible)
    
    def __init__(self, x: int, y: int, update_callback):
        """Initialize status bar widget.
        
        Args:
            x: X position on display
            y: Y position on display
            update_callback: Callback to trigger display updates. Must not be None.
        """
        super().__init__(x, y, 128, STATUS_BAR_HEIGHT, update_callback)
        
        # Child widgets - created with shared update_callback
        font_path = '/opt/universalchess/resources/Font.ttc'
        if not os.path.exists(font_path):
            font_path = 'resources/Font.ttc'
        
        # Children route their updates through _handle_child_update so this
        # composite's cached sprite is invalidated when an autonomous child (clock
        # tick, battery/wifi/bluetooth state change) updates; otherwise the stale
        # composite would be re-pasted and the change never shown.
        child_callback = self._handle_child_update
        
        # Clock widget: 2.5x wider than tall = 40x16, starts at x=0
        self._clock_widget = ClockWidget(0, 0, 40, 16, child_callback,
                                         font_size=14, font_path=font_path,
                                         show_seconds=False)
        
        # Install status widget (shows during engine installation)
        self._install_widget = InstallStatusWidget(self.INSTALL_X, 0, 16, child_callback)
        
        # Update status widget (shows when update is available)
        self._update_widget = UpdateStatusWidget(self.UPDATE_X, 0, 16, child_callback)
        
        # Chromecast status widget (observes the ChromecastService singleton)
        self._chromecast_widget = ChromecastStatusWidget(self.CHROMECAST_X, 0, 16, child_callback)
        
        # Other status widgets - all 16px tall to fill status bar
        self._wifi_widget = WiFiStatusWidget(self.WIFI_X, 0, 16, child_callback)
        self._bluetooth_widget = BluetoothStatusWidget(self.BLUETOOTH_X, 0, 12, 16, child_callback)
        self._battery_widget = BatteryWidget(self.BATTERY_X, 0, 20, 16, child_callback)
        
        # Collect child widgets for unified lifecycle management
        self._child_widgets: List[Widget] = [
            self._clock_widget,
            self._install_widget,
            self._update_widget,
            self._chromecast_widget,
            self._wifi_widget,
            self._bluetooth_widget,
            self._battery_widget,
        ]
        
        # Start battery widget polling thread
        self._battery_widget.start()
    
    def set_scheduler(self, scheduler) -> None:
        """Set scheduler and propagate to all child widgets."""
        super().set_scheduler(scheduler)
        for widget in self._child_widgets:
            widget.set_scheduler(scheduler)
    
    def set_update_callback(self, callback) -> None:
        """Set this widget's update callback.

        Children keep routing through _handle_child_update (which reads the new
        callback via self._update_callback), so the composite cache is still
        invalidated on child changes. Re-asserted here in case a child's callback
        was replaced elsewhere.
        """
        super().set_update_callback(callback)
        for widget in self._child_widgets:
            widget.set_update_callback(self._handle_child_update)

    def _handle_child_update(self, full: bool = False, immediate: bool = False):
        """Invalidate the composite cache on a child change, then refresh.

        The status bar composites its children into a single cached sprite that
        the Manager blits directly; render() (which redraws the children) only
        runs on a cache miss. An autonomous child invalidates only its OWN cache,
        so without invalidating this parent the stale composite would be re-pasted
        and the child's change would never appear.
        """
        self.invalidate_cache()
        return self._update_callback(full, immediate)
    
    def update(self, full: bool = False):
        """Invalidate cache and request display update.
        
        Args:
            full: If True, force a full refresh instead of partial refresh.
        
        Returns:
            Future that completes when the display refresh finishes.
        """
        return self.invalidate_and_update(full=full)
    
    def stop(self) -> None:
        """Stop all child widgets and perform cleanup."""
        for widget in self._child_widgets:
            try:
                widget.stop()
            except Exception as e:
                log.debug(f"Error stopping {widget.__class__.__name__}: {e}")
    
    def render(self, sprite: Image.Image) -> None:
        """Render status bar with all visible child widgets.
        
        Order from left to right: Clock, [Chromecast], WiFi, Bluetooth, Battery
        Each widget controls its own visibility.
        """
        # Draw background (white)
        self.draw_background_on_sprite(sprite)
        
        # Draw all visible child widgets onto sprite
        for widget in self._child_widgets:
            if widget.visible:
                widget.draw_on(sprite, widget.x, widget.y)
