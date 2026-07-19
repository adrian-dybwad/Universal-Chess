"""Startup resume-target selection policy.

Pure and hardware-free so the decision can be unit-tested without importing the
application entrypoint (``main.py``), which pulls in the display/board stack.
This mirrors why the web "Resume" DB policy lives in
:mod:`universalchess.managers.game.database` rather than in ``main.py``.
"""

from __future__ import annotations

from typing import Optional

# Non-NULL, non-"*" result tokens: a game carrying one of these has genuinely
# finished and is review-only, never a live in-progress game.
FINISHED_RESULTS = frozenset({"1-0", "0-1", "1/2-1/2"})


def choose_resume_target(
    recorded: Optional[dict], incomplete: Optional[dict]
) -> Optional[dict]:
    """Pick which game a restart should resume.

    Args:
        recorded: Resume payload for the game id the session snapshot recorded
            (what the user was last looking at -- may be a finished game restored
            for review), or None when the snapshot has no usable id or that game
            is gone. Must carry ``id`` and ``result`` keys when present.
        incomplete: Resume payload for the newest in-progress (NULL-result) game,
            or None when there is none. Must carry ``id`` when present.

    Returns:
        The resume payload to load, or None when neither is resumable.

    A newer in-progress game supersedes a recorded *finished* game. The session
    snapshot records the last-viewed game id but is only reset to "no game" on
    some new-game paths; when a fresh game is started in place on the board right
    after one finished, the snapshot still points at the finished game. Blindly
    honouring it would reload that finished game's game-over screen and strand
    the real, live game (the reported bug: an in-progress game did not load after
    a restart -- the previous drawn game did). A NULL-result game that is *newer*
    (higher id) than a recorded finished game can only be that live game, so it
    wins. A recorded in-progress game, or a finished game with no newer live game,
    is honoured as-is so reviewing a finished position still works.
    """
    if recorded is None:
        return incomplete
    if incomplete is None:
        return recorded
    recorded_finished = recorded.get("result") in FINISHED_RESULTS
    if recorded_finished and incomplete["id"] > recorded["id"]:
        return incomplete
    return recorded


__all__ = ["FINISHED_RESULTS", "choose_resume_target"]
