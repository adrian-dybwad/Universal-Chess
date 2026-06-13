"""Pure decision logic for the PLAY button.

The PLAY button is a single universal control:

    * In the game (board showing) it SUSPENDs the game back to the menu, keeping
      the game alive (clock paused, LEDs off) so it can be resumed.
    * In the menu with a suspended game it RESUMEs that game.
    * In the menu with no game it STARTs a new one.

This module is intentionally free of hardware/UI imports so the mapping can be
unit-tested in isolation; main.py supplies the runtime state and performs the
side effects (mirrors the pure ``pairing_confirm`` module pattern).
"""

from enum import Enum, auto


class PlayAction(Enum):
    """What the PLAY button should do in the current context."""

    START_NEW = auto()
    RESUME = auto()
    SUSPEND = auto()


def decide_play_action(
    app_state_is_game: bool,
    has_suspended_game: bool,
) -> PlayAction:
    """Decide the PLAY-button action from the current context.

    Args:
        app_state_is_game: True when the game screen is currently showing.
        has_suspended_game: True when a resumable game is in progress (the game
            managers are alive and the game is not over) while the menu shows.

    Returns:
        SUSPEND while the game is showing (``has_suspended_game`` is then
        irrelevant); otherwise RESUME when a suspended game exists, else
        START_NEW.
    """
    if app_state_is_game:
        return PlayAction.SUSPEND
    if has_suspended_game:
        return PlayAction.RESUME
    return PlayAction.START_NEW
