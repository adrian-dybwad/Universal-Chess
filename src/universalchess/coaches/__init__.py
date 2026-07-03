# Coaches Framework
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""AI coaches framework.

A coach is a named persona (with a target Elo and character type) that supplies
the tone, focus, and instructions the AI adopts when explaining a move. Coaches
are Python plugins: built-ins ship in :mod:`universalchess.coaches.builtin` and
users add their own by dropping modules into the user coaches folder. See
:mod:`universalchess.coaches.registry` for discovery and selection.
"""

from universalchess.coaches.base import Coach, CoachingSituation, MoveContext
from universalchess.coaches.registry import (
    AUTO,
    discover_coaches,
    get_coach,
    list_coaches,
    refresh,
    resolve_coach,
    resolve_coach_info,
    resolve_human_color,
    resolve_opponent_elo,
    resolve_persona,
    select_move_context,
    user_coaches_dir,
)

__all__ = [
    "Coach",
    "CoachingSituation",
    "MoveContext",
    "AUTO",
    "discover_coaches",
    "get_coach",
    "list_coaches",
    "refresh",
    "resolve_coach",
    "resolve_coach_info",
    "resolve_human_color",
    "resolve_opponent_elo",
    "resolve_persona",
    "select_move_context",
    "user_coaches_dir",
]
