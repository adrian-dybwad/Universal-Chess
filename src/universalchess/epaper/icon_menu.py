"""
Icon menu widget for e-paper display.

A menu composed of IconButtonWidget items with keyboard navigation.
Supports callbacks for selection and external key event routing.
"""

from PIL import Image
from .framework.widget import Widget
from .icon_button import IconButtonWidget
from typing import Optional, Callable, List
from dataclasses import dataclass
import threading

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)


# Lazy import of board module to avoid circular imports and premature hardware initialization
_board_module = None


def _get_board():
    """Lazily import and return the board module."""
    global _board_module
    if _board_module is None:
        from universalchess.board import board
        _board_module = board
    return _board_module


@dataclass
class IconMenuEntry:
    """Configuration for a menu entry.

    Attributes:
        key: Unique identifier returned on selection
        label: Display text
        icon_name: Icon identifier for rendering
        enabled: Whether entry is enabled/visible (disabled entries are hidden)
        selectable: Whether entry can be selected (non-selectable entries are
                   displayed but skipped during navigation and cannot be activated)
        height_ratio: Relative height weight (default 1.0, use 2.0 for double height)
        max_height: Maximum height in pixels (None for no limit)
        icon_size: Custom icon size in pixels (None uses default based on button height)
        layout: Button layout - 'horizontal' (icon left) or 'vertical' (icon top centered)
        font_size: Font size in pixels (default 16)
        bold: Whether to render text in bold (default False)
        border_width: Width of button border in pixels (default 2)
        description: Optional long description text rendered below the icon+label area.
                    Displayed as smaller, word-wrapped text spanning the full button width.
        description_font_size: Font size for description text (default 11)
        icon_image: Optional pre-rendered image used as the main icon instead of
                    a drawn icon_name (e.g. a chess-piece sprite preview).
        icon_mask: Optional transparency mask for icon_image (opaque where the
                    image should show); required to composite onto the menu
                    background rather than painting a solid box.
        trailing_icon_name: Optional icon drawn at the right edge of the button
                    (e.g. "radio_checked"/"radio_empty" to mark a radio selection).
    """
    key: str
    label: str
    icon_name: str
    enabled: bool = True
    selectable: bool = True
    # Help tip shown by the e-paper help dialog when this entry is focused and
    # the HELP key is pressed. Sourced from the menu catalog; None when no tip.
    help: Optional[str] = None
    height_ratio: float = 1.0
    max_height: int = None
    icon_size: int = None
    layout: str = "horizontal"
    font_size: int = 16
    bold: bool = False
    border_width: int = 2
    description: str = None
    description_font_size: int = 11
    icon_image: Optional[Image.Image] = None
    icon_mask: Optional[Image.Image] = None
    trailing_icon_name: Optional[str] = None


class IconMenuWidget(Widget):
    """Widget displaying a menu of large icon buttons.
    
    Displays a vertical list of icon buttons with keyboard navigation.
    Supports UP/DOWN for navigation, TICK for selection, BACK for cancel.
    
    When there are more entries than can fit on screen (based on min_button_height),
    the menu becomes scrollable. Navigation automatically scrolls to keep the
    selected item visible.
    
    Can be used in two modes:
    1. Callback mode: Provide on_select callback, call handle_key() externally
    2. Blocking mode: Call wait_for_selection() which blocks until user selects
    
    Attributes:
        entries: List of menu entry configurations
        selected_index: Currently highlighted entry index
        scroll_offset: Index of first visible entry (for scrolling)
    """
    
    def __init__(self, x: int, y: int, width: int, height: int, update_callback,
                 entries: List[IconMenuEntry],
                 selected_index: int = 0,
                 on_select: Optional[Callable[[str], None]] = None,
                 on_back: Optional[Callable[[], None]] = None,
                 on_index_change: Optional[Callable[[int], None]] = None,
                 button_height: int = 70,
                 button_margin: int = 4,
                 background_shade: int = 2,
                 min_button_height: int = 45):
        """Initialize icon menu widget.
        
        Args:
            x: X position of widget
            y: Y position of widget
            width: Widget width
            height: Widget height
            update_callback: Callback to trigger display updates. Must not be None.
            entries: List of menu entry configurations
            selected_index: Initial selected entry index
            on_select: Optional callback(key) when entry is selected with TICK
            on_back: Optional callback() when BACK is pressed
            on_index_change: Optional callback(index) fired whenever the cursor
                moves to a different entry. Used to persist the live cursor
                position so it survives a process restart (which interrupts the
                blocked menu before any save-on-exit could run).
            button_height: Height of each button (default 70)
            button_margin: Margin around buttons, passed to each button (default 4)
            background_shade: Dithered background shade 0-16 (default 2 = ~12.5% grey)
            min_button_height: Minimum button height before scrolling (default 45)
        """
        super().__init__(x, y, width, height, update_callback, background_shade=background_shade)
        
        # Filter disabled entries (disabled entries are not shown at all)
        self.entries = [e for e in entries if e.enabled]
        
        # Clamp selected_index to valid range
        self.selected_index = min(selected_index, max(0, len(self.entries) - 1))
        
        # If initial selection is non-selectable, find first selectable entry
        if self.entries and not self.entries[self.selected_index].selectable:
            for i, entry in enumerate(self.entries):
                if entry.selectable:
                    self.selected_index = i
                    break
        
        # Callbacks for external use
        self.on_select = on_select
        self.on_back = on_back
        self.on_index_change = on_index_change
        
        # Layout
        self.button_height = button_height
        self.button_margin = button_margin
        self.min_button_height = min_button_height
        
        # Scrolling state
        self.scroll_offset = 0  # Index of first visible entry
        self._visible_count = 0  # Number of entries that fit on screen
        
        # Selection event handling for blocking mode
        self._selection_event = threading.Event()
        self._selection_result: Optional[str] = None
        self._active = False
        
        # Create button widgets for visible entries
        self._buttons: List[IconButtonWidget] = []
        self._calculate_visible_count()
        
        # Adjust scroll_offset so selected item is visible before creating buttons
        if self._visible_count < len(self.entries) and self.selected_index >= self._visible_count:
            # Selected item is below the initially visible area
            self.scroll_offset = self.selected_index - self._visible_count + 1
        
        self._create_buttons()
        
        log.info(f"IconMenuWidget: Created with {len(self.entries)} entries, "
                 f"{self._visible_count} visible at a time")
    
    def _handle_child_update(self, full: bool = False, immediate: bool = False):
        """Handle update requests from child widgets by forwarding to parent callback."""
        return self._update_callback(full, immediate)
    
    def _calculate_visible_count(self) -> None:
        """Calculate how many entries can fit on screen.
        
        Uses min_button_height to determine if scrolling is needed.
        Each entry's minimum height is min_button_height * height_ratio.
        """
        if not self.entries:
            self._visible_count = 0
            return
        
        # Calculate minimum required height for each entry based on its height_ratio
        # An entry with height_ratio=2.0 needs 2x the min_button_height
        min_total_height = sum(self.min_button_height * entry.height_ratio for entry in self.entries)
        
        if min_total_height <= self.height:
            # All entries fit without scrolling
            self._visible_count = len(self.entries)
        else:
            # Calculate how many entries fit by accumulating minimum heights
            accumulated_height = 0
            visible = 0
            for entry in self.entries:
                entry_min_height = self.min_button_height * entry.height_ratio
                if accumulated_height + entry_min_height <= self.height:
                    accumulated_height += entry_min_height
                    visible += 1
                else:
                    break
            self._visible_count = max(1, visible)
    
    def _create_buttons(self) -> None:
        """Create IconButtonWidget instances for visible entries.
        
        Only creates buttons for entries from scroll_offset to 
        scroll_offset + visible_count. Buttons are placed directly 
        adjacent to each other with their own transparent margins.
        
        Button heights are proportional to their height_ratio values
        within the visible subset.
        """
        self._buttons = []
        
        if not self.entries or self._visible_count == 0:
            return
        
        # Get visible entries
        visible_start = self.scroll_offset
        visible_end = min(visible_start + self._visible_count, len(self.entries))
        visible_entries = self.entries[visible_start:visible_end]
        
        if not visible_entries:
            return
        
        # Calculate total height ratio for visible entries
        total_ratio = sum(entry.height_ratio for entry in visible_entries)
        available_height = self.height
        
        current_y = 0
        for vis_idx, entry in enumerate(visible_entries):
            # Actual index in full entries list
            actual_idx = visible_start + vis_idx
            
            # Calculate this button's height based on its ratio
            button_height = int(available_height * entry.height_ratio / total_ratio)
            
            # Apply max_height constraint if specified (only for selectable entries)
            # Non-selectable info widgets are exempt from height constraints
            if entry.selectable and entry.max_height is not None and button_height > entry.max_height:
                button_height = entry.max_height
            
            # Determine icon size - use entry's custom size or derive from height
            if entry.icon_size is not None:
                icon_size = entry.icon_size
            else:
                # Default icon size scales with button height
                icon_size = min(36, max(20, button_height - 24))
            
            is_selected = (actual_idx == self.selected_index)
            button = IconButtonWidget(
                0,
                current_y,
                self.width,
                button_height,
                self._handle_child_update,
                key=entry.key,
                label=entry.label,
                icon_name=entry.icon_name,
                selected=is_selected,
                margin=self.button_margin,
                icon_size=icon_size,
                layout=entry.layout,
                font_size=entry.font_size,
                bold=entry.bold,
                border_width=entry.border_width,
                description=entry.description,
                description_font_size=entry.description_font_size,
                icon_image=entry.icon_image,
                icon_mask=entry.icon_mask,
                trailing_icon_name=entry.trailing_icon_name
            )
            self._buttons.append(button)
            current_y += button_height
    
    def _ensure_selection_visible(self) -> bool:
        """Ensure the currently selected item is within the visible scroll region.
        
        Adjusts scroll_offset if the selected item is outside the visible area.
        
        Returns:
            True if scroll_offset was changed, False otherwise
        """
        if self._visible_count >= len(self.entries):
            # No scrolling needed - all items visible
            return False
        
        if self.selected_index < self.scroll_offset:
            # Selected item is above visible area - scroll up
            self.scroll_offset = self.selected_index
            return True
        elif self.selected_index >= self.scroll_offset + self._visible_count:
            # Selected item is below visible area - scroll down
            self.scroll_offset = self.selected_index - self._visible_count + 1
            return True
        
        return False
    
    def _is_selectable(self, index: int) -> bool:
        """Check if the entry at the given index is selectable.
        
        Args:
            index: Entry index to check
            
        Returns:
            True if entry exists and is selectable, False otherwise
        """
        if 0 <= index < len(self.entries):
            return self.entries[index].selectable
        return False
    
    def _find_next_selectable(self, start: int, direction: int) -> int:
        """Find the next selectable entry in the given direction.
        
        Wraps around if reaching the end of the list.
        
        Args:
            start: Starting index
            direction: 1 for forward/down, -1 for backward/up
            
        Returns:
            Index of next selectable entry, or start if none found
        """
        if not self.entries:
            return start
        
        count = len(self.entries)
        current = start
        
        # Try each entry once
        for _ in range(count):
            current = (current + direction) % count
            if self._is_selectable(current):
                return current
        
        # No selectable entries found, return original
        return start
    
    def _find_first_selectable(self) -> int:
        """Find the first selectable entry.
        
        Returns:
            Index of first selectable entry, or 0 if none found
        """
        for i, entry in enumerate(self.entries):
            if entry.selectable:
                return i
        return 0
    
    def set_selection(self, index: int) -> None:
        """Set the current selection index, scrolling if needed.
        
        Automatically adjusts scroll_offset to keep the selected item visible.
        
        Args:
            index: New selection index
        """
        new_index = max(0, min(index, len(self.entries) - 1))
        if new_index == self.selected_index:
            return
        
        self.selected_index = new_index
        
        # Persist the new cursor position immediately. A process restart (SIGTERM)
        # interrupts the blocked menu wait, so persisting only on menu exit would
        # lose the live cursor; the callback writes it on every move instead.
        if self.on_index_change is not None:
            self.on_index_change(new_index)
        
        # Check if we need to scroll to keep selection visible
        needs_rebuild = self._ensure_selection_visible()
        
        if needs_rebuild:
            # Rebuild buttons with new scroll position
            self._create_buttons()
        else:
            # Just update selection state on existing buttons
            visible_start = self.scroll_offset
            for vis_idx, button in enumerate(self._buttons):
                actual_idx = visible_start + vis_idx
                button.set_selected(actual_idx == self.selected_index)
        
        self.invalidate_and_update(immediate=True)
    
    def get_selected_key(self) -> Optional[str]:
        """Get the key of the currently selected entry.
        
        Returns:
            Key string of selected entry, or None if no entries
        """
        if self.entries and self.selected_index < len(self.entries):
            return self.entries[self.selected_index].key
        return None

    def get_selected_help(self) -> Optional[str]:
        """Get the help tip of the currently focused entry.

        Returns the focused entry's ``help`` text (from the catalog), or None if
        there are no entries or the entry has no help. Used by the board help
        dialog when HELP is pressed.
        """
        if self.entries and self.selected_index < len(self.entries):
            return self.entries[self.selected_index].help
        return None
    
    def render(self, sprite: Image.Image) -> None:
        """Render the menu with all buttons onto the sprite."""
        # Draw background
        self.draw_background_on_sprite(sprite)
        
        # Draw each button onto sprite
        for button in self._buttons:
            button.draw_on(sprite, button.x, button.y)
    
    def handle_key(self, key_id) -> bool:
        """Handle key press events.
        
        Routes key events to navigate menu and trigger selection.
        Can be called externally to send key events to this menu.
        
        Non-selectable entries are skipped during navigation. Pressing TICK
        on a non-selectable entry does nothing.
        
        Args:
            key_id: Key identifier from board
            
        Returns:
            True if key was handled, False otherwise
        """
        if not self._active:
            return False
        
        board = _get_board()
        
        if key_id == board.Key.UP:
            # Move up, skipping non-selectable entries, with wrap-around
            next_idx = self._find_next_selectable(self.selected_index, -1)
            self.set_selection(next_idx)
            return True
        
        elif key_id == board.Key.DOWN:
            # Move down, skipping non-selectable entries, with wrap-around
            next_idx = self._find_next_selectable(self.selected_index, 1)
            self.set_selection(next_idx)
            return True
        
        elif key_id == board.Key.TICK:
            # Selection confirmed - only if current entry is selectable
            if not self._is_selectable(self.selected_index):
                # Non-selectable entry - do nothing
                return True
            
            selected_key = self.get_selected_key()
            if selected_key:
                self._selection_result = selected_key
                if self.on_select:
                    self.on_select(selected_key)
            else:
                self._selection_result = "BACK"
            self._selection_event.set()
            return True
        
        elif key_id == board.Key.BACK:
            self._selection_result = "BACK"
            if self.on_back:
                self.on_back()
            self._selection_event.set()
            return True
        
        elif key_id == board.Key.HELP:
            self._selection_result = "HELP"
            self._selection_event.set()
            return True
        
        elif key_id == board.Key.LONG_PLAY:
            self._selection_result = "SHUTDOWN"
            self._selection_event.set()
            return True
        
        return False
    
    def activate(self) -> None:
        """Activate the menu for key handling.
        
        Call this before using handle_key() in callback mode.
        
        Note: Does not clear selection state if a result is already pending.
        This handles the race condition where cancel_selection() is called
        before wait_for_selection() starts waiting.
        """
        self._active = True
        # Only clear if there's no pending result (handles race with cancel_selection)
        if self._selection_result is None:
            self._selection_event.clear()
    
    def deactivate(self) -> None:
        """Deactivate the menu from key handling."""
        self._active = False
    
    def wait_for_selection(self, initial_index: int = 0) -> str:
        """Block and wait for user selection via key presses.
        
        This is the blocking mode of operation. For non-blocking mode,
        use activate(), handle_key(), and deactivate() directly.
        
        Ensures the initial selection is scrolled into view even if the
        index hasn't changed (e.g., when returning to a parent menu).
        
        If the initial_index points to a non-selectable entry, the first
        selectable entry is selected instead.
        
        Args:
            initial_index: Initial selection index (must be selectable)
            
        Returns:
            Selected entry key, "BACK", "HELP", or "SHUTDOWN"
        """
        # Ensure initial_index points to a selectable entry
        if not self._is_selectable(initial_index):
            initial_index = self._find_first_selectable()
        
        # Set initial selection
        self.set_selection(initial_index)
        
        # Ensure the selected item is visible even if selection didn't change
        # This handles the case of returning to a menu where the selected item
        # is not in the visible scroll region
        if self._ensure_selection_visible():
            self._create_buttons()
            self.invalidate_and_update()
        
        # Activate key handling
        self.activate()
        
        try:
            log.info("IconMenuWidget: Waiting for selection...")
            self._selection_event.wait()
            result = self._selection_result or "BACK"
            log.info(f"IconMenuWidget: Selection result='{result}'")
            return result
        finally:
            self.deactivate()
    
    def cancel_selection(self, result: str = "CANCELLED") -> None:
        """Cancel the current selection wait with a custom result.
        
        This is useful for external events (like BLE connection) that need
        to interrupt the menu and trigger a specific action.
        
        Args:
            result: The result to return from wait_for_selection
        """
        self._active = False
        self._selection_result = result
        self._selection_event.set()
    
    def stop(self) -> None:
        """Stop the widget and release any blocked waits."""
        self._active = False
        self._selection_result = "BACK"
        self._selection_event.set()
        super().stop()


def create_icon_menu_entries(entries_config: List[dict]) -> List[IconMenuEntry]:
    """Helper to create IconMenuEntry list from config dictionaries.
    
    Args:
        entries_config: List of dicts with 'key', 'label', 'icon_name',
                       and optional 'enabled', 'height_ratio', 'max_height',
                       'icon_size', 'layout', 'font_size', 'bold',
                       'description', 'description_font_size'
        
    Returns:
        List of IconMenuEntry objects
    """
    return [
        IconMenuEntry(
            key=e['key'],
            label=e['label'],
            icon_name=e['icon_name'],
            enabled=e.get('enabled', True),
            height_ratio=e.get('height_ratio', 1.0),
            max_height=e.get('max_height', None),
            icon_size=e.get('icon_size', None),
            layout=e.get('layout', 'horizontal'),
            font_size=e.get('font_size', 16),
            bold=e.get('bold', False),
            description=e.get('description', None),
            description_font_size=e.get('description_font_size', 11)
        )
        for e in entries_config
    ]
