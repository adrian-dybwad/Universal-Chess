"""Settings persistence utilities.

Generic helpers for loading and saving settings to/from centaur.ini.
Does not contain any knowledge of specific setting names or structures.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _get_settings_module():
    """Lazy import of Settings to avoid circular imports."""
    from universalchess.board.settings import Settings
    return Settings


def load_str(section: str, key: str, default: str = "") -> str:
    """Load a string setting from centaur.ini.

    Args:
        section: Section name in config file
        key: Setting key
        default: Default value if not present

    Returns:
        String value of the setting
    """
    Settings = _get_settings_module()
    return Settings.read(section, key, default)


def load_bool(section: str, key: str, default: bool = True) -> bool:
    """Load a boolean setting from centaur.ini.

    Args:
        section: Section name in config file
        key: Setting key
        default: Default value if not present

    Returns:
        Boolean value of the setting
    """
    Settings = _get_settings_module()
    value = Settings.read(section, key, "true" if default else "false")
    if value.lower() in ("false", "0", ""):
        return False
    return True


def load_int(section: str, key: str, default: int = 0) -> int:
    """Load an integer setting from centaur.ini.

    Args:
        section: Section name in config file
        key: Setting key
        default: Default value if not present

    Returns:
        Integer value of the setting
    """
    Settings = _get_settings_module()
    value = Settings.read(section, key, str(default))
    try:
        return int(value)
    except ValueError:
        return default


def load_section(section: str, defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Load all settings from a section using defaults for type inference and missing values.

    Type inference is based on the default value type:
    - bool default -> load_bool
    - int default -> load_int
    - str default -> load_str

    Args:
        section: Section name in config file
        defaults: Dict of key -> default_value pairs

    Returns:
        Dict with loaded values (same keys as defaults)
    """
    result = {}
    for key, default in defaults.items():
        if isinstance(default, bool):
            result[key] = load_bool(section, key, default)
        elif isinstance(default, int):
            result[key] = load_int(section, key, default)
        else:
            result[key] = load_str(section, key, str(default) if default is not None else "")
    return result


def save_setting(section: str, key: str, value: Any, *, broadcast: bool = True) -> bool:
    """Save a single setting to centaur.ini.

    Args:
        section: Section name in config file
        key: Setting key
        value: Setting value (string, bool, or int)
        broadcast: If True, broadcast settings_changed event to web clients

    Returns:
        True if saved successfully, False otherwise
    """
    try:
        Settings = _get_settings_module()
        if isinstance(value, bool):
            str_value = "true" if value else "false"
        else:
            str_value = str(value)
        Settings.write(section, key, str_value)
        
        if broadcast:
            try:
                from universalchess.services.game_broadcast import broadcast_settings_changed
                broadcast_settings_changed()
            except Exception:  # noqa: S110  # nosec B110 - broadcast is an optional enhancement; a failure must not block the settings write
                pass  # Broadcast is optional enhancement
        
        return True
    except Exception:
        return False


def clear_section(section: str) -> bool:
    """Clear all settings in a section.

    Args:
        section: Section name in config file

    Returns:
        True if cleared successfully, False otherwise
    """
    try:
        Settings = _get_settings_module()
        import configparser
        config = configparser.ConfigParser()
        config.read(Settings.configfile)
        if config.has_section(section):
            for key in list(config.options(section)):
                config.remove_option(section, key)
            Settings.write_config(config)
        return True
    except Exception:
        return False


@dataclass
class MenuContext:
    """Tracks menu navigation state with full path and index stack.

    Maintains both the menu path hierarchy (for navigation) and the selected
    index at each level (for restoring exact cursor position). The path and
    indices are kept in sync - each menu level has a corresponding index.

    Storage format in centaur.ini:
        path: "Settings/Players/Player1" (slash-separated menu names)
        indices: "2:0:1" (colon-separated indices at each level)

    Attributes:
        path_stack: List of menu names from root to current level
        index_stack: List of selected indices at each corresponding level
        _nav_depth: Current navigation depth (how deep we've navigated during runtime)
        _log: Optional logger for debug output
        _section: Section name for persistence
    """

    path_stack: List[str] = field(default_factory=list)
    index_stack: List[int] = field(default_factory=list)
    _nav_depth: int = field(default=0, repr=False)
    _log: Optional[Any] = field(default=None, repr=False)
    _section: str = field(default="MenuState", repr=False)
    _frozen: bool = field(default=False, repr=False)

    def freeze(self) -> None:
        """Stop persisting further navigation changes for the process lifetime.

        Called once at the start of shutdown/cleanup. On SIGTERM the process
        exits via ``sys.exit`` from the signal handler, which raises SystemExit
        and unwinds the blocked menu stack -- running every ``leave_menu`` in a
        ``finally``. Those pops would rewrite the persisted path shallower and
        shallower (Settings/connectivity/bluetooth -> Settings), destroying the
        exact position the next launch must restore to. Freezing makes ``save``
        a no-op so the deepest position -- already on disk from when the user
        entered it -- survives the teardown. In-memory mutation still proceeds
        (harmless in a dying process); only persistence is suppressed.
        """
        self._frozen = True

    def push(self, menu_name: str, index: int = 0) -> None:
        """Push a new menu level onto the navigation stack.

        Args:
            menu_name: Name of the menu being entered
            index: Initial selected index in that menu (default 0)
        """
        self.path_stack.append(menu_name)
        self.index_stack.append(index)
        self.save()

    def pop(self) -> tuple:
        """Pop the current menu level and return to parent.

        Returns:
            Tuple of (menu_name, index) that was popped, or (None, 0) if empty
        """
        if not self.path_stack:
            return (None, 0)
        menu_name = self.path_stack.pop()
        index = self.index_stack.pop() if self.index_stack else 0
        self.save()
        return (menu_name, index)

    def update_index(self, index: int) -> None:
        """Update the selected index at the current navigation depth.

        Args:
            index: New selected index to store
        """
        idx = self._nav_depth - 1 if self._nav_depth > 0 else 0
        if idx < len(self.index_stack):
            self.index_stack[idx] = index
            self.save()

    def current_index(self) -> int:
        """Get the selected index at the current navigation depth.

        Returns:
            Current level's index, or 0 if at invalid depth
        """
        idx = self._nav_depth - 1 if self._nav_depth > 0 else 0
        if idx < len(self.index_stack):
            return self.index_stack[idx]
        return 0

    def current_menu(self) -> Optional[str]:
        """Get the name of the current menu.

        Returns:
            Current menu name, or None if at root
        """
        return self.path_stack[-1] if self.path_stack else None

    def depth(self) -> int:
        """Get the current navigation depth.

        Returns:
            Number of menus in the stack (0 = at main menu)
        """
        return len(self.path_stack)

    def path_str(self) -> str:
        """Get the path as a slash-separated string.

        Returns:
            Path string like "Settings/Players/Player1"
        """
        return "/".join(self.path_stack)

    def indices_str(self) -> str:
        """Get the index stack as a colon-separated string.

        Returns:
            Indices string like "2:0:1"
        """
        return ":".join(str(i) for i in self.index_stack) if self.index_stack else "0"

    def clear(self) -> None:
        """Clear the navigation stack (return to main menu)."""
        self.path_stack.clear()
        self.index_stack.clear()
        self._nav_depth = 0
        self.save()

    def save(self) -> None:
        """Persist the current state to centaur.ini.

        Suppressed after :meth:`freeze` so the shutdown teardown unwind cannot
        overwrite the deep restore path already on disk (see ``freeze``).
        """
        if self._frozen:
            return
        try:
            path = self.path_str()
            indices = self.indices_str()

            if not path:
                save_setting(self._section, "path", "")
                save_setting(self._section, "indices", "0")
                if self._log:
                    self._log.debug("[MenuContext] Cleared menu state")
            else:
                save_setting(self._section, "path", path)
                save_setting(self._section, "indices", indices)
                if self._log:
                    self._log.debug(f"[MenuContext] Saved: path={path}, indices={indices}")
        except Exception as e:
            if self._log:
                self._log.warning(f"[MenuContext] Error saving state: {e}")

    @classmethod
    def load(cls, section: str = "MenuState", log=None) -> "MenuContext":
        """Load menu state from centaur.ini.

        Args:
            section: Section name for menu state persistence
            log: Optional logger for debug output

        Returns:
            MenuContext with restored path and index stacks
        """
        try:
            path = load_str(section, "path", "")
            indices_str = load_str(section, "indices", "0")

            # Parse path
            path_stack = path.split("/") if path else []

            # Parse indices
            if indices_str:
                try:
                    index_stack = [int(x) for x in indices_str.split(":") if x]
                except ValueError:
                    index_stack = [0] * len(path_stack)
            else:
                index_stack = [0] * len(path_stack)

            # Ensure stacks are same length
            while len(index_stack) < len(path_stack):
                index_stack.append(0)
            while len(index_stack) > len(path_stack):
                index_stack.pop()

            ctx = cls(
                path_stack=path_stack,
                index_stack=index_stack,
                _log=log,
                _section=section,
            )

            if log:
                if path:
                    log.info(f"[MenuContext] Loaded: path={path}, indices={indices_str}")
                else:
                    log.debug("[MenuContext] No saved menu state")

            return ctx
        except Exception as e:
            if log:
                log.warning(f"[MenuContext] Error loading state: {e}")
            return cls(_log=log, _section=section)

    def get_restore_path(self) -> List[tuple]:
        """Get the full restoration path as a list of (menu_name, index) tuples.

        Useful for programmatically navigating to a saved position.

        Returns:
            List of (menu_name, index) tuples from root to current position
        """
        return list(zip(self.path_stack, self.index_stack))

    def restore_from_path(self, path: List[tuple]) -> None:
        """Reset navigation to a previously captured (menu_name, index) path.

        Repopulates the path/index stacks and rewinds the runtime navigation
        depth to the root so a subsequent re-entry (e.g. _handle_settings calling
        enter_menu) restores level by level. Used to re-enter a submenu after a
        game is suspended back to the full menu, mirroring startup restoration.

        Args:
            path: List of (menu_name, index) tuples from root to target level.
        """
        self.path_stack = [name for name, _ in path]
        self.index_stack = [index for _, index in path]
        self._nav_depth = 0
        self.save()

    def truncate_below_current(self) -> None:
        """Drop any saved path deeper than the current navigation depth.

        Called when a restore descent stops at the current level because the
        next saved container is unreachable (e.g. a dynamic item -- a paired
        device or scanned network -- that no longer exists). The lingering
        deeper tokens no longer reflect a reachable position, so they must not
        persist or hijack a subsequent navigation. A no-op when nothing is saved
        below the current depth.
        """
        if self._nav_depth < len(self.path_stack):
            self.path_stack = self.path_stack[: self._nav_depth]
            self.index_stack = self.index_stack[: self._nav_depth]
            self.save()

    def next_restore_token(self) -> Optional[str]:
        """Peek the saved menu token the current depth would descend into next.

        Returns ``path_stack[_nav_depth]`` -- the child container recorded one
        level below the current position -- or ``None`` when the saved chain is
        exhausted (the deepest recorded level has been entered) or has been
        truncated by a divergent (fresh) navigation.

        The engine uses this during restore to auto-dispatch the row that leads
        into the next saved container, replaying the full navigation path level
        by level. It only peeks; ``enter_menu`` is what consumes a level.
        """
        if self._nav_depth < len(self.path_stack):
            return self.path_stack[self._nav_depth]
        return None

    def enter_menu(self, menu_name: str, default_index: int = 0) -> int:
        """Enter a submenu, handling both fresh navigation and restoration.

        Uses _nav_depth to track current position in the navigation hierarchy.
        If the saved path at _nav_depth matches menu_name, we're restoring and
        return the saved index. Otherwise, we truncate the saved path and start
        fresh navigation from this point.

        Args:
            menu_name: Name of the menu being entered
            default_index: Index to use if not restoring (default 0)

        Returns:
            The index to use for initial selection in the submenu
        """
        current_depth = self._nav_depth

        # Check if saved path has this menu at the current depth (restoration mode)
        if current_depth < len(self.path_stack) and self.path_stack[current_depth] == menu_name:
            # Restoring - use saved index, advance nav depth
            saved_index = (
                self.index_stack[current_depth] if current_depth < len(self.index_stack) else 0
            )
            self._nav_depth += 1
            if self._log:
                self._log.debug(
                    f"[MenuContext] Restoring to {menu_name} with saved index {saved_index} "
                    f"(depth now {self._nav_depth})"
                )
            return saved_index
        else:
            # Fresh navigation or path diverged - truncate saved state and push new menu
            self.path_stack = self.path_stack[:current_depth]
            self.index_stack = self.index_stack[:current_depth]

            # Push the new menu
            self.path_stack.append(menu_name)
            self.index_stack.append(default_index)
            self._nav_depth += 1
            self.save()
            if self._log:
                self._log.debug(
                    f"[MenuContext] Fresh nav to {menu_name} with index {default_index} "
                    f"(depth now {self._nav_depth})"
                )
            return default_index

    def leave_menu(self) -> None:
        """Leave the current submenu.

        Decrements navigation depth. If we were in fresh navigation mode
        (nav_depth at end of path), also pops from the stacks.
        """
        if self._nav_depth <= 0:
            return

        self._nav_depth -= 1

        # If we're leaving a menu that was at the end of the path, pop it
        if self._nav_depth >= len(self.path_stack) - 1 and self.path_stack:
            self.pop()

        if self._log:
            self._log.debug(f"[MenuContext] Left menu, depth now {self._nav_depth}")
