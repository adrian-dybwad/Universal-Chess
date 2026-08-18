"""The handles that live only as long as one game on the board.

A game's protocol, display, controller, coach and Lichess session are built
together when the game starts and must be discarded together when it ends. They
were seven module-level names in the application, cleared one at a time by a
teardown that nothing checked: a handle missed there survives into the next game,
drawing on a display that game does not own or routing board events into a game
that has already ended.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Any, Optional, Tuple

log = logging.getLogger(__name__)

# Each component and the method that releases it, in the order teardown requires.
# Stated as data so the order is readable in one place instead of being the order
# of four near-identical blocks.
_TEARDOWN_ORDER: Tuple[Tuple[str, str, str], ...] = (
    ("lichess_session", "close", "Lichess session"),
    ("controller", "cleanup", "controller manager"),
    ("protocol", "cleanup", "game handler"),
    ("display", "cleanup", "display manager"),
)


@dataclass
class GameRuntime:
    """Handles to the running game, all empty when no game is running.

    ``protocol`` doubles as the "a game exists" flag (see :attr:`is_running`): a
    live protocol while the full menu is showing means the game is suspended and
    resumable, which is what the PLAY button and the RESUME relabel consult.
    """

    protocol: Optional[Any] = None
    display: Optional[Any] = None
    controller: Optional[Any] = None
    # Held so a board-reset new game -- which reuses the same coordinator instead
    # of rebuilding it -- can drop the prior game's cached statements and never
    # coach a new move with an old game's text.
    coach: Optional[Any] = None
    lichess_session: Optional[Any] = None
    is_position_game: bool = False
    # The player-defining settings this game was built from, so a settings change
    # made while the game is suspended starts a fresh game instead of resuming one
    # whose players no longer match.
    player_signature: Optional[tuple] = None

    @property
    def is_running(self) -> bool:
        """True while the game's handles are held, whether playing or suspended."""
        return self.protocol is not None

    def close(self) -> None:
        """Release every component and clear every handle.

        The order in ``_TEARDOWN_ORDER`` is required, not incidental. A component
        whose release raises is logged and dropped rather than aborting the rest,
        because the alternative leaves the remaining handles alive with no code
        path that will ever close them. Every field is then returned to its
        declared default, which is what stops a field added later from being
        forgotten here and surviving into the next game.
        """
        for attribute, method, label in _TEARDOWN_ORDER:
            component = getattr(self, attribute)
            if component is None:
                continue
            try:
                getattr(component, method)()
            except Exception as e:
                log.debug(f"Error cleaning up {label}: {e}")

        for field in dataclasses.fields(self):
            setattr(self, field.name, field.default)
