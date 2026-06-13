"""Menu manager for managing menu navigation and state.

Provides a centralized manager for menu navigation that:
- Handles break results (CLIENT_CONNECTED, PIECE_MOVED) automatically
- Manages menu state and navigation
- Simplifies menu handler code by removing boilerplate

The MenuManager is a singleton that manages the active menu widget
and provides a clean API for showing menus and handling results.
"""

import logging
from enum import Enum, auto
from typing import List, Callable, Optional, Any, Union
from dataclasses import dataclass

from universalchess.epaper.icon_menu import IconMenuEntry, IconMenuWidget

log = logging.getLogger(__name__)


class MenuResult(Enum):
    """Standard menu result types."""
    BACK = auto()           # User pressed back
    SHUTDOWN = auto()       # Shutdown requested
    HELP = auto()           # Help requested
    CLIENT_CONNECTED = auto()  # BLE/RFCOMM client connected
    PIECE_MOVED = auto()    # Piece moved on board
    PLAY = auto()           # PLAY pressed - start/resume game from any menu depth
    

# Result strings that map to MenuResult enum
RESULT_MAP = {
    "BACK": MenuResult.BACK,
    "SHUTDOWN": MenuResult.SHUTDOWN,
    "HELP": MenuResult.HELP,
    "CLIENT_CONNECTED": MenuResult.CLIENT_CONNECTED,
    "PIECE_MOVED": MenuResult.PIECE_MOVED,
    "PLAY": MenuResult.PLAY,
}

# Results that should break out of all nested menus. PLAY is included so that
# pressing PLAY in any submenu (including loops driven by run_menu_loop or
# handlers that test MenuSelection.is_break directly) unwinds to the main loop
# to start/resume a game, rather than being treated as an unknown selection that
# just refreshes the current menu.
BREAK_RESULTS = {MenuResult.CLIENT_CONNECTED, MenuResult.PIECE_MOVED, MenuResult.PLAY}


@dataclass
class MenuSelection:
    """Result of a menu selection.
    
    Attributes:
        key: The key of the selected entry (string)
        result_type: Standard result type if applicable (MenuResult enum or None)
        is_break: True if this result should break out of all nested menus
    """
    key: str
    result_type: Optional[MenuResult] = None
    is_break: bool = False
    
    @classmethod
    def from_key(cls, key: str) -> 'MenuSelection':
        """Create MenuSelection from a key string."""
        result_type = RESULT_MAP.get(key)
        is_break = result_type in BREAK_RESULTS if result_type else False
        return cls(key=key, result_type=result_type, is_break=is_break)
    
    def is_back(self) -> bool:
        """Check if this is a BACK result."""
        return self.result_type == MenuResult.BACK
    
    def is_exit(self) -> bool:
        """Check if this result should exit the current menu (BACK, SHUTDOWN, HELP, or break)."""
        return self.is_break or self.result_type in {MenuResult.BACK, MenuResult.SHUTDOWN, MenuResult.HELP}


class MenuManager:
    """Manager for menu navigation and state.
    
    Singleton class that provides centralized menu management.
    Handles the active menu widget, break results, and state transitions.
    
    Includes key queuing to handle the case where users press keys before
    a menu finishes loading. Keys pressed during menu loading are queued
    and replayed after the menu becomes active, or their net effect is
    applied to the initial selection index.
    
    Usage:
        manager = MenuManager.get_instance()
        
        # Simple menu display
        result = manager.show_menu(entries)
        if result.is_break:
            return result  # Propagate break to caller
        if result.is_back():
            return  # Exit this menu level
            
        # Handle specific selections
        if result.key == "SomeOption":
            handle_some_option()
    """
    
    _instance: Optional['MenuManager'] = None
    
    def __init__(self):
        """Initialize the menu manager."""
        self._active_widget: Optional[IconMenuWidget] = None
        self._board = None  # Set via set_board()
        self._status_bar_height = 16  # Default, can be overridden
        self._display_width = 128
        self._display_height = 296
        
        # Key queue for caching key presses while menu is loading
        # This allows users to press keys before a menu finishes rendering
        self._key_queue: List[Any] = []
        self._menu_loading = False  # True while menu is being set up

        # Latched when a shutdown is requested (long-press PLAY) so that any
        # subsequent menu display unwinds immediately. See show_menu().
        self._shutdown_requested = False
    
    @classmethod
    def get_instance(cls) -> 'MenuManager':
        """Get the singleton instance of MenuManager."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def set_board(self, board):
        """Set the board module reference.
        
        Args:
            board: The board module for display management
        """
        self._board = board
    
    def set_dimensions(self, width: int, height: int, status_bar_height: int = 16):
        """Set display dimensions.
        
        Args:
            width: Display width in pixels
            height: Display height in pixels  
            status_bar_height: Height of status bar in pixels
        """
        self._display_width = width
        self._display_height = height
        self._status_bar_height = status_bar_height
    
    @property
    def active_widget(self) -> Optional[IconMenuWidget]:
        """Get the currently active menu widget."""
        return self._active_widget
    
    @property
    def is_loading(self) -> bool:
        """Check if a menu is currently loading (not yet ready for key events)."""
        return self._menu_loading
    
    def queue_key(self, key_id) -> bool:
        """Queue a key press while menu is loading.
        
        Call this when a key event arrives during menu loading. The key will
        be replayed after the menu becomes active.
        
        Args:
            key_id: The key that was pressed
            
        Returns:
            True if key was queued, False if menu is not loading
        """
        if self._menu_loading:
            self._key_queue.append(key_id)
            log.info(f"[MenuManager] Queued key {key_id} during menu load (queue size: {len(self._key_queue)})")
            return True
        return False
    
    def _replay_queued_keys(self):
        """Replay any keys that were queued during menu loading.
        
        Called after menu is fully loaded and active. Keys are replayed in
        order to apply their effect (navigation, selection, etc.).
        """
        if not self._key_queue:
            return
        
        log.info(f"[MenuManager] Replaying {len(self._key_queue)} queued keys")
        
        # Take the queue and clear it (in case handle_key causes re-entrancy)
        keys_to_replay = self._key_queue.copy()
        self._key_queue.clear()
        
        # Replay each key in order
        for key_id in keys_to_replay:
            if self._active_widget is not None:
                log.debug(f"[MenuManager] Replaying queued key: {key_id}")
                self._active_widget.handle_key(key_id)
            else:
                log.warning(f"[MenuManager] Cannot replay key {key_id} - no active widget")
                break
    
    def cancel_selection(self, result: str):
        """Cancel the current menu with a specific result.
        
        Used to interrupt menus when external events occur (BLE connection, etc.)
        
        Args:
            result: Result string to return from the menu
        """
        # Latch shutdown before touching the active widget. A shutdown can be
        # requested while a deeply nested submenu (e.g. WiFi) is on screen; the
        # latch makes every later show_menu() return SHUTDOWN so the request is
        # not swallowed by intermediate handlers that only propagate break
        # results. Set unconditionally (even with no active widget) to avoid a
        # race where the request lands between menu displays.
        if result == "SHUTDOWN":
            self._shutdown_requested = True

        # Clear any queued keys when cancelling
        self._key_queue.clear()
        if self._active_widget is not None:
            log.info(f"[MenuManager] Cancelling menu with result: {result}")
            self._active_widget.cancel_selection(result)
    
    def refresh_menu(self):
        """Signal the current menu to refresh (rebuild entries with updated settings).
        
        Used when settings change from an external source (web app) and the
        currently displayed menu should update to reflect the new values.
        
        The menu loop will see the REFRESH result and rebuild entries.
        """
        if self._active_widget is not None:
            log.info("[MenuManager] Refreshing menu due to settings change")
            self._active_widget.cancel_selection("REFRESH")
    
    def show_menu(
        self,
        entries: List[IconMenuEntry],
        initial_index: int = 0
    ) -> MenuSelection:
        """Display a menu and wait for selection.
        
        This is the primary method for showing menus. It handles:
        - Clearing existing widgets and adding status bar
        - Creating and displaying the menu widget
        - Managing the active widget state
        - Converting the result to a MenuSelection object
        - Queuing and replaying keys pressed during menu loading
        
        Keys pressed while the menu is loading (rendering) are queued and
        replayed after the menu becomes active. This allows users to navigate
        before the menu visually appears.
        
        Args:
            entries: List of menu entry configurations
            initial_index: Index of entry to select initially
            
        Returns:
            MenuSelection with the user's selection or break result
        """
        # Once shutdown is requested (long-press PLAY), unwind instead of
        # rendering. The request can arrive while a nested submenu is on screen;
        # without this short-circuit the SHUTDOWN result is swallowed by
        # intermediate handlers (which only propagate break results) and the
        # user is dropped one level up with nothing happening. Returning
        # SHUTDOWN here lets each level's existing SHUTDOWN/exit check unwind to
        # the top, where _shutdown() runs. Checked before the board guard since
        # the display may already be tearing down. Guards against the submenu
        # shutdown-hang regression.
        if self._shutdown_requested:
            return MenuSelection.from_key("SHUTDOWN")

        if self._board is None:
            raise RuntimeError("MenuManager.set_board() must be called before show_menu()")

        # Clear any stale queued keys and mark as loading
        self._key_queue.clear()
        self._menu_loading = True
        
        # Clear existing widgets and add status bar before showing menu
        # This ensures a clean slate after splash screens or other temporary displays
        self._board.display_manager.clear_widgets()
        
        # Create menu widget
        log.info(f"[DEBUG MenuManager] Creating IconMenuWidget with selected_index={initial_index}")
        menu_widget = IconMenuWidget(
            0,
            self._status_bar_height,
            self._display_width,
            self._display_height - self._status_bar_height,
            self._board.display_manager.update,
            entries=entries,
            selected_index=initial_index
        )
        log.info(f"[DEBUG MenuManager] IconMenuWidget created, menu_widget.selected_index={menu_widget.selected_index}")
        
        # Register as active menu (keys will be queued until loading completes)
        self._active_widget = menu_widget
        
        # Add widget to display
        promise = self._board.display_manager.add_widget(menu_widget)
        if promise:
            try:
                promise.result(timeout=5.0)
            except Exception as e:
                log.warning(f"[MenuManager] Error waiting for menu render: {e}")
        
        try:
            # Activate the widget for key handling
            menu_widget.activate()
            
            # Menu is now ready - stop queuing keys
            self._menu_loading = False
            
            # Replay any keys that were pressed during loading
            self._replay_queued_keys()
            
            # Wait for selection (widget is already activated)
            log.info("MenuManager: Waiting for selection...")
            menu_widget._selection_event.wait()
            result_key = menu_widget._selection_result or "BACK"
            log.info(f"MenuManager: Selection result='{result_key}'")
            return MenuSelection.from_key(result_key)
        finally:
            self._menu_loading = False
            self._key_queue.clear()
            menu_widget.deactivate()
            self._active_widget = None
    
    def run_menu_loop(
        self,
        build_entries: Callable[[], List[IconMenuEntry]],
        handle_selection: Callable[[MenuSelection], Optional[MenuSelection]],
        initial_index: int = 0,
        track_selection: bool = True
    ) -> Optional[MenuSelection]:
        """Run a menu loop with automatic break handling.
        
        Simplifies the common pattern of:
        - Build entries
        - Show menu
        - Check for breaks/back
        - Handle selection
        - Loop
        
        Args:
            build_entries: Function that returns the menu entries (called each iteration)
            handle_selection: Function to handle the selection. Should return:
                             - None to continue the loop
                             - MenuSelection to exit (propagates breaks/back)
            initial_index: Starting selection index
            track_selection: If True, tracks last selection and uses it on next iteration
            
        Returns:
            MenuSelection if exited due to break/back, None if handle_selection returned
        """
        last_index = initial_index
        
        while True:
            entries = build_entries()
            result = self.show_menu(entries, initial_index=last_index)
            
            # Handle settings refresh - rebuild entries with updated values
            if result.key == "REFRESH":
                continue
            
            # Always propagate break results
            if result.is_break:
                return result
            
            # Update tracked index
            if track_selection:
                last_index = self._find_entry_index(entries, result.key)
            
            # Exit on standard exit results
            if result.result_type in {MenuResult.BACK, MenuResult.SHUTDOWN, MenuResult.HELP}:
                return result
            
            # Let handler process the selection
            handler_result = handle_selection(result)
            if handler_result is not None:
                return handler_result
    
    def _find_entry_index(self, entries: List[IconMenuEntry], key: str) -> int:
        """Find the index of an entry by its key.
        
        Args:
            entries: List of menu entries
            key: Key to search for
            
        Returns:
            Index of matching entry, or 0 if not found
        """
        for i, entry in enumerate(entries):
            if entry.key == key:
                return i
        return 0


def is_break_result(result: Union[str, MenuSelection, None]) -> bool:
    """Check if a result should break out of all nested menus.
    
    Utility function for checking results without MenuManager.
    
    Args:
        result: String key, MenuSelection, or None
        
    Returns:
        True if this is a break result, False if None or not a break result
    """
    if result is None:
        return False
    if isinstance(result, MenuSelection):
        return result.is_break
    # Derive from the same enum source (BREAK_RESULTS via from_key) so the string
    # and MenuSelection paths cannot drift. A new break result therefore only
    # needs to be added to RESULT_MAP/BREAK_RESULTS in one place.
    return MenuSelection.from_key(result).is_break


def is_refresh_result(result: Union[str, MenuSelection, None]) -> bool:
    """Check if a result indicates the menu should refresh.
    
    When settings change from an external source (web app), menus receive
    a REFRESH result. The menu loop should continue to rebuild entries
    with updated settings.
    
    Args:
        result: String key, MenuSelection, or None
        
    Returns:
        True if this is a refresh result, False otherwise
    """
    if result is None:
        return False
    if isinstance(result, MenuSelection):
        return result.result_type == MenuResult.BACK and result.key == "REFRESH"
    return result == "REFRESH"


def find_entry_index(entries: List[IconMenuEntry], key: str) -> int:
    """Find the index of an entry by its key.
    
    Utility function for finding entry indices.
    
    Args:
        entries: List of menu entries
        key: Key to search for
        
    Returns:
        Index of matching entry, or 0 if not found
    """
    for i, entry in enumerate(entries):
        if entry.key == key:
            return i
    return 0
