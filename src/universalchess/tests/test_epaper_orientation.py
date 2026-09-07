"""E-paper diagram and panel turn are independent.

Why these tests exist
---------------------
Player 1 Color is which colour is set up at the e-paper. The panel turns 180
only when a solo human plays the far-side colour. One flag used to do both,
so Black on player 1 rotated the display away from a human sitting there,
and a human on player 2 with White on player 1 never got the panel turned.

How a regression manifests
--------------------------
board_from_black follows the far-end seat instead of the seated view (or
P1 when no one sits); face_far_end is True for Black on player 1 with the
human there, or False for White on player 1 with the human on player 2.
"""

import pytest

from universalchess.epaper.orientation import (
    EpaperOrientation,
    color_is_black,
    epaper_orientation,
    seated_human_color,
    slot_is_seated,
)


@pytest.mark.parametrize(
    "player1_color,seated_color,expect",
    [
        # No seated player: diagram follows Player 1 Color, panel stays put.
        ("white", None, EpaperOrientation(False, False)),
        ("black", None, EpaperOrientation(True, False)),
        ("", None, EpaperOrientation(False, False)),
        # Solo human at player 1 (their colour is Player 1 Color): no panel turn.
        ("white", "white", EpaperOrientation(False, False)),
        ("black", "black", EpaperOrientation(True, False)),
        # Solo human on the far side: diagram from their view, panel turns.
        ("white", "black", EpaperOrientation(True, True)),
        ("black", "white", EpaperOrientation(False, True)),
        ("BLACK", "WHITE", EpaperOrientation(False, True)),
    ],
)
def test_epaper_orientation_splits_diagram_and_panel_turn(
    player1_color, seated_color, expect
):
    """Black at the e-paper is not a panel turn; a far-side human is.

    Failure: face_far_end tracks Player 1 Color, or board_from_black ignores
    the seated colour, so a Black setup rotates the menu or a Black-at-P2
    human keeps seeing an unrotated panel.
    """
    assert epaper_orientation(player1_color, seated_color) == expect


@pytest.mark.parametrize(
    "p1_type,p1_color,p2_type,expect",
    [
        ("human", "white", "engine", "white"),
        ("human", "black", "engine", "black"),
        ("hand_brain", "black", "engine", "black"),
        ("engine", "white", "human", "black"),
        ("engine", "black", "human", "white"),
        ("engine", "white", "hand_brain", "black"),
        ("lichess", "white", "human", "black"),
        ("human", "white", "human", None),
        ("engine", "white", "engine", None),
        ("human", "white", "lichess", "white"),
    ],
)
def test_seated_human_color_is_the_solo_person_at_the_board(
    p1_type, p1_color, p2_type, expect
):
    """One seated slot yields that slot's colour; two or none yield None.

    Why: local panel turn uses this. Treating Lichess or Engine as seated
    would rotate for the remote side, or skip a Hand+Brain player on slot 2.

    Failure: engine vs human White-on-P1 returns white (the human plays
    Black on P2), or two humans return a colour so the panel turns.
    """
    assert seated_human_color(p1_type, p1_color, p2_type) == expect


def test_slot_is_seated_is_human_or_hand_brain():
    """Only a person at the physical board counts as seated.

    Failure: engine or lichess is treated as seated, or hand_brain is not.
    """
    assert slot_is_seated("human") is True
    assert slot_is_seated("hand_brain") is True
    assert slot_is_seated("engine") is False
    assert slot_is_seated("lichess") is False
    assert slot_is_seated("") is False


def test_color_is_black_treats_empty_as_white():
    """Player 1 Color defaults to White when unset.

    Failure: empty string is Black, so a missing setting rotates the diagram.
    """
    assert color_is_black("black") is True
    assert color_is_black("BLACK") is True
    assert color_is_black("white") is False
    assert color_is_black("") is False
    assert color_is_black("random") is False
