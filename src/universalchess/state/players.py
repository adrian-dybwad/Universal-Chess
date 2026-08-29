"""
Players state.

Holds observable player names for UI widgets.
UI widgets observe this state to display player information.

The actual Player objects and game logic remain in PlayerManager -
this state object provides an observable interface for the UI layer.
"""

import logging
from typing import Optional, Callable, List

log = logging.getLogger(__name__)


def format_player_label(name: str, rating) -> str:
    """Clock / LiveBoard label: ``alice(1500)``, or name alone when unrated.

    Empty parentheses after an unrated or AI name hid the username and
    looked like a missing rating.
    """
    text = (name or "").strip()
    rating_text = "" if rating is None else str(rating).strip()
    if text and rating_text:
        return f"{text}({rating_text})"
    return text


class PlayersState:
    """Observable players state.
    
    Holds player names and hand-brain mode (white and black) for UI display.
    
    Observers are notified when player names or hand-brain settings change
    (e.g., when a remote client takes over from the local engine, or when
    Lichess game info arrives with actual player names).
    
    Thread safety: Properties are simple reads. The PlayerManager that owns
    the actual players should update this state atomically.
    """
    
    def __init__(self):
        """Initialize players state with defaults."""
        self._white_name: str = ""
        self._black_name: str = ""
        self._white_hand_brain: bool = False
        self._black_hand_brain: bool = False
        
        # Observer callbacks
        self._on_names_change: List[Callable[[str, str], None]] = []
    
    # -------------------------------------------------------------------------
    # Properties (read-only access to state)
    # -------------------------------------------------------------------------
    
    @property
    def white_name(self) -> str:
        """White player's display name."""
        return self._white_name
    
    @property
    def black_name(self) -> str:
        """Black player's display name."""
        return self._black_name
    
    @property
    def white_hand_brain(self) -> bool:
        """Whether white player is in hand-brain mode."""
        return self._white_hand_brain
    
    @property
    def black_hand_brain(self) -> bool:
        """Whether black player is in hand-brain mode."""
        return self._black_hand_brain
    
    # -------------------------------------------------------------------------
    # Observer management
    # -------------------------------------------------------------------------
    
    def on_names_change(self, callback: Callable[[str, str], None]) -> None:
        """Register callback for player name changes.
        
        Called when player names change (e.g., when a remote client
        takes over from the local engine).
        
        Args:
            callback: Function(white_name, black_name) called on change.
        """
        if callback not in self._on_names_change:
            self._on_names_change.append(callback)
    
    def remove_observer(self, callback: Callable) -> None:
        """Remove a previously registered callback.
        
        Args:
            callback: The callback to remove.
        """
        if callback in self._on_names_change:
            self._on_names_change.remove(callback)
    
    def _notify_names_change(self) -> None:
        """Notify all name change observers."""
        for callback in self._on_names_change:
            try:
                callback(self._white_name, self._black_name)
            except Exception:
                log.exception("Player-name observer callback failed")
    
    # -------------------------------------------------------------------------
    # State mutations (called by PlayerManager)
    # -------------------------------------------------------------------------
    
    def set_player_names(self, white_name: str, black_name: str) -> None:
        """Set player names.
        
        Called when PlayerManager is initialized, players are swapped,
        or Lichess game info arrives with actual player names.
        
        Args:
            white_name: White player's display name.
            black_name: Black player's display name.
        """
        if self._white_name != white_name or self._black_name != black_name:
            self._white_name = white_name
            self._black_name = black_name
            self._notify_names_change()
    
    def set_hand_brain(self, white_hand_brain: bool, black_hand_brain: bool) -> None:
        """Set hand-brain mode for each player.
        
        Called when PlayerManager is initialized with hand-brain settings.
        
        Args:
            white_hand_brain: Whether white player is in hand-brain mode.
            black_hand_brain: Whether black player is in hand-brain mode.
        """
        self._white_hand_brain = white_hand_brain
        self._black_hand_brain = black_hand_brain
    
    def reset(self) -> None:
        """Reset to initial state.
        
        Called when a game ends or is cleaned up.
        """
        if self._white_name or self._black_name:
            self._white_name = ""
            self._black_name = ""
            self._notify_names_change()
        self._white_hand_brain = False
        self._black_hand_brain = False


# -----------------------------------------------------------------------------
# Singleton instance
# -----------------------------------------------------------------------------

_instance: Optional[PlayersState] = None


def get_players_state() -> PlayersState:
    """Get the singleton PlayersState instance.
    
    Returns:
        The global PlayersState instance.
    """
    global _instance
    if _instance is None:
        _instance = PlayersState()
    return _instance


def reset_players_state() -> PlayersState:
    """Reset the singleton to a fresh instance.
    
    Primarily for testing.
    
    Returns:
        The new PlayersState instance.
    """
    global _instance
    _instance = PlayersState()
    return _instance
