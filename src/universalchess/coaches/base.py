# Coach Base Class
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Abstract base for AI coaches. A coach is a named persona (e.g. "Dave") with a
# target Elo and a character type. Given a coaching situation it returns the
# persona text (its work product) that is injected into the AI coach prompt.
#
# Designed to be extendable: users add coaches by dropping a Python module that
# subclasses Coach into the user coaches folder (see coaches/registry.py).
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""Coach persona framework.

A :class:`Coach` supplies the *persona* -- the tone, focus, and instructions the
AI should adopt -- for a move being coached. It does not talk to any network: the
returned persona string is composed with the fixed safety/brevity guardrails and
sent by :mod:`universalchess.services.coach`.

Each coach carries display metadata (``name``, ``elo``, ``character_type``,
``description``) shown in the selectable coach list, and two personas: one for the
human player's own moves and one for the opponent's moves. Simple coaches only set
the two persona strings; a coach that needs to vary its text by position can
override :meth:`Coach.persona` and read the richer fields on
:class:`CoachingSituation`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, Optional, Tuple


class MoveContext(Enum):
    """Whose move is being coached.

    - PLAYER_MOVE: the human player's own move, or a hint they are considering.
    - OPPONENT_MOVE: a move played by the opponent.
    """

    PLAYER_MOVE = auto()
    OPPONENT_MOVE = auto()


@dataclass
class CoachingSituation:
    """Everything a coach may use to produce its persona.

    Only ``move_context`` is required by the default attribute-based persona; the
    remaining fields let a custom coach vary its text by position/move. They are
    populated best-effort by the caller and may be unset.

    Attributes:
        move_context: Whether the player or the opponent made the move.
        is_potential_move: True when this is a hint the player is considering
            rather than a move already played.
        side_to_move: "white" or "black" -- the side that moved.
        human_color: "white"/"black" for the human player, or None when there is
            no single human (engine vs engine, or two humans).
        fen_before: FEN before the move, when available.
        move_text: The move in the user's notation, when available.
        facts: Verified move facts (captures/checks/pins), when available.
        eval_before_cp / eval_after_cp: Eval swing in centipawns (white's
            perspective), when available.
        move_number: Full-move number, when available.
    """

    move_context: MoveContext
    is_potential_move: bool = False
    side_to_move: str = ""
    human_color: Optional[str] = None
    fen_before: Optional[str] = None
    move_text: Optional[str] = None
    facts: Tuple[str, ...] = ()
    eval_before_cp: Optional[int] = None
    eval_after_cp: Optional[int] = None
    move_number: Optional[int] = None


class Coach:
    """Base class for AI coaches.

    Subclasses set the class attributes (``id``, ``name``, ``elo``,
    ``character_type``, ``description``) and provide the two persona strings
    (``player_move_persona``, ``opponent_move_persona``). The default
    :meth:`persona` selects between them by the situation's move context; override
    it for position-aware behavior.

    Extension point: any subclass discovered by :mod:`universalchess.coaches.registry`
    (built-in or user-provided) becomes selectable in the coach card. ``id`` must be
    a stable, unique, lowercase slug; the registry skips subclasses with a blank id.
    """

    #: Stable unique slug used for selection/persistence (e.g. "dave").
    id: str = ""
    #: Human-readable name shown in the selector (e.g. "Dave").
    name: str = ""
    #: Target Elo, used for Auto selection and shown in the selector.
    elo: int = 0
    #: Short character-type label shown next to the name (e.g. "Guarded Mentor").
    character_type: str = ""
    #: One-line description of the coaching style.
    description: str = ""
    #: Persona used when coaching the human player's own move (or a hint).
    player_move_persona: str = ""
    #: Persona used when coaching a move played by the opponent.
    opponent_move_persona: str = ""

    def persona(self, situation: CoachingSituation) -> str:
        """Return the persona text for a coaching situation.

        Default behavior selects the player- or opponent-move persona by
        ``situation.move_context``. Override to vary the text by position, eval, or
        move facts using the other fields on the situation.
        """
        if situation.move_context is MoveContext.PLAYER_MOVE:
            return self.player_move_persona
        return self.opponent_move_persona

    def get_info(self) -> Dict[str, object]:
        """Return display metadata for the selectable coach list."""
        return {
            "id": self.id,
            "name": self.name,
            "elo": self.elo,
            "character_type": self.character_type,
            "description": self.description,
        }


__all__ = ["Coach", "CoachingSituation", "MoveContext"]
