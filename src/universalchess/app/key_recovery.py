"""Noticing that the board has stopped responding to its buttons.

Every key press is routed to something: an overlay, the menu widget, or the
game. When routing finds nothing to give a key to, the board is in a state it
should not be in -- a menu with no widget, a game whose managers are gone -- and
from the user's side it is dead: presses do nothing, and there is no way out
except cutting the power.

Counting consecutive unhandled presses and forcing a return to the main menu
after enough of them is what makes that recoverable. The count must be
consecutive, and the threshold must be more than one, because recovery tears
down the running game: a press that arrives during a screen transition has
nowhere to go and is not evidence of anything.
"""


class KeyRecovery:
    """Consecutive unhandled key presses, and when they mean the board is stuck."""

    #: Consecutive unhandled presses that mean the board is not routing keys.
    THRESHOLD = 5

    def __init__(self) -> None:
        """Start with a board that is answering its keys."""
        self.unhandled_count = 0

    def record_handled(self) -> None:
        """Note that a key reached something, clearing the count."""
        self.unhandled_count = 0

    def record_unhandled(self) -> bool:
        """Note that a key reached nothing.

        Returns:
            True when the threshold is reached and the caller should recover.
            The count restarts, so a board that stays stuck recovers once per
            further run of presses rather than on every press after the first.
        """
        self.unhandled_count += 1
        if self.unhandled_count < self.THRESHOLD:
            return False
        self.unhandled_count = 0
        return True
