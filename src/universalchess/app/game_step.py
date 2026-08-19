"""Choosing which deferred repair the main loop performs while a game is running.

Callback threads leave work in :class:`~universalchess.app.pending_work.PendingWork`
and the loop performs one item per pass, on the main thread, because each of them
rebuilds players or widgets. Which item, when several are waiting, was an elif
chain buried in the loop: the priority existed only as the order the branches were
written in, and nothing established that the work not chosen stayed pending.

The choice is made here, as a pure function of the pending state, so both halves
are testable -- the ranking, and the rule that exactly one request is claimed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from universalchess.app.pending_work import PendingWork


class GameAction(Enum):
    """What the loop should do about the game this pass."""

    #: Nothing is waiting; the loop sleeps.
    IDLE = "idle"
    #: Leave a position (practice) game and start a normal one.
    SWITCH_TO_NORMAL_GAME = "switch_to_normal_game"
    #: Restart play with players re-read from current settings.
    REBUILD_PLAYERS = "rebuild_players"
    #: Rebuild the display widgets under the same players.
    REBUILD_LAYOUT = "rebuild_layout"
    #: Apply a web settings change to the live display.
    RELOAD_SETTINGS = "reload_settings"
    #: Apply a board-control command pushed from the web.
    APPLY_BOARD_COMMAND = "apply_board_command"


@dataclass(frozen=True)
class GameStep:
    """The one action to perform, and what it needs to know.

    ``lichess_reason`` is why a remote game ended, carried only with
    :attr:`GameAction.REBUILD_PLAYERS` because only that action's prompt reports
    it -- the difference between offering a new seek and explaining an abort.
    """

    action: GameAction
    lichess_reason: Optional[str] = None


# Highest priority first. A new game outranks the repairs because they all fix up a
# game that is about to be replaced, so servicing one first spends a full e-paper
# refresh on state that is immediately discarded. Among the repairs, the broadest
# comes first: rebuilding the players also rebuilds the layout, and rebuilding the
# layout subsumes a display refresh, so the narrower work would be redundant.
_PRIORITY = (
    ("switch_to_normal_game", GameAction.SWITCH_TO_NORMAL_GAME),
    ("player_rebuild", GameAction.REBUILD_PLAYERS),
    ("layout_rebuild", GameAction.REBUILD_LAYOUT),
    ("settings_reload", GameAction.RELOAD_SETTINGS),
    ("board_command", GameAction.APPLY_BOARD_COMMAND),
)

# Slots the claim only peeks at, because the code that performs the work clears
# them itself. The board command is read again while being applied (it decides
# whether to set up a position or abort), so claiming it here would leave the
# handler with nothing to act on.
_PEEKED = frozenset({"board_command"})


def claim_game_step(pending: PendingWork) -> GameStep:
    """Claim the highest-priority pending game work, leaving the rest.

    Exactly one request is consumed: work that loses this pass is still pending on
    the next one. Claiming everything while acting on one item would silently drop
    a settings change that arrived in the same moment as a rebuild.

    Args:
        pending: The board's deferred work. Mutated -- the chosen request is
            claimed from it (see :data:`_PEEKED` for the one exception).

    Returns:
        The action to perform, or :attr:`GameAction.IDLE` when nothing is waiting.
    """
    for slot_name, action in _PRIORITY:
        slot = getattr(pending, slot_name)
        if slot_name in _PEEKED:
            if not slot.requested():
                continue
        elif slot.take() is None:
            continue

        # Only a player rebuild reports a Lichess termination, and it consumes the
        # reason with itself so the game after next is not labelled with it.
        reason = None
        if action is GameAction.REBUILD_PLAYERS:
            request = pending.lichess_next.take()
            reason = request.payload if request is not None else None
        return GameStep(action=action, lichess_reason=reason)

    return GameStep(action=GameAction.IDLE)
