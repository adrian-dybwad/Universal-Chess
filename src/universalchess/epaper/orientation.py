"""Which way the e-paper chess diagram and panel face.

Player 1 Color is the physical setup: which colour sits at the e-paper end.
The diagram matches the pieces at that end unless a solo seated player is
on the far side, in which case the diagram is from their view and the panel
turns 180 so they can read it.

These are independent. Black on player 1 does not turn the panel around;
a human playing the far-side colour does.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

_SEATED_SLOT_TYPES = frozenset({"human", "hand_brain"})


class EpaperOrientation(NamedTuple):
    """Square remapping and panel turn for one game.

    ``board_from_black``: Black at the bottom of the chess diagram (and the
    matching clock rows). ``face_far_end``: rotate the whole framebuffer 180
    so menus face player 2.
    """

    board_from_black: bool
    face_far_end: bool


def color_is_black(color: str) -> bool:
    """True when ``color`` names Black. Empty or unknown is White."""
    return (color or "white").strip().lower() == "black"


def slot_is_seated(player_type: str) -> bool:
    """True when this slot is a person at the physical board.

    Hand+Brain sits at the board the same way Human does. Engine and remote
    (Lichess) slots do not.
    """
    return (player_type or "").strip().lower() in _SEATED_SLOT_TYPES


def seated_human_color(
    player1_type: str, player1_color: str, player2_type: str
) -> Optional[str]:
    """Colour the solo seated player plays, or None if both or neither sit.

    Player 1 Color is which colour is at the e-paper. The far slot plays the
    other colour. Two humans or two engines leave the panel facing player 1.
    """
    p1_sits = slot_is_seated(player1_type)
    p2_sits = slot_is_seated(player2_type)
    if p1_sits == p2_sits:
        return None
    p1_black = color_is_black(player1_color)
    if p1_sits:
        return "black" if p1_black else "white"
    return "white" if p1_black else "black"


def epaper_orientation(
    player1_color: str, seated_color: Optional[str] = None
) -> EpaperOrientation:
    """Diagram and panel turn from Player 1 Color and who is sitting.

    ``seated_color`` is the solo human's playing colour (assigned colour on
    Lichess, slot colour locally). None means the diagram follows Player 1
    Color and the panel stays facing the e-paper end: waiting splash, two
    humans, or engine vs engine.
    """
    p1_black = color_is_black(player1_color)
    if seated_color is None:
        return EpaperOrientation(board_from_black=p1_black, face_far_end=False)
    seated_black = color_is_black(seated_color)
    return EpaperOrientation(
        board_from_black=seated_black,
        face_far_end=seated_black != p1_black,
    )
