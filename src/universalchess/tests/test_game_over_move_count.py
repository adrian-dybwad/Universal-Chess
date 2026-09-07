"""Game-over 'N moves' is the chess full-move count, not the ply count.

The result strip used len(move_stack), which is half-moves. A Bishop+Knight
mate delivered on White's 34th move is 67 plies and was shown as "67 moves".
"""

from unittest.mock import MagicMock

import pytest

from universalchess.epaper.game_over import GameOverWidget, fullmove_count_from_plies


@pytest.mark.parametrize(
    "plies, fullmoves",
    [
        (0, 0),
        (-1, 0),
        (1, 1),  # White's first move, game over before Black replies
        (2, 1),  # 1. e4 e5
        (3, 2),
        (4, 2),  # Fool's mate: 1. f3 e5 2. g4 Qh4#
        (67, 34),  # White mates on move 34 (the reported B+N case)
        (68, 34),  # Black mates on move 34
    ],
)
def test_fullmove_count_from_plies(plies, fullmoves):
    """ceil(plies / 2) is the last completed full-move number.

    Why: the panel labels the number "moves", which in chess is the full-move
    count (1. e4 e5 is one move, not two). How a regression manifests: 67
    plies become 67 instead of 34, or Black mating on move 34 becomes 35
    (FEN fullmove_number after Black's reply).
    """
    assert fullmove_count_from_plies(plies) == fullmoves


def test_game_over_widget_stores_fullmoves_not_plies():
    """The widget converts move_stack length before storing move_count.

    Why: conversion at the source is what the panel renders; a helper that is
    correct in isolation still leaves "67 moves" on the board if _on_game_over
    keeps passing the raw ply count. How a regression manifests: move_count
    is 67 and the rendered line is "67 moves".
    """
    game_state = MagicMock()
    game_state.move_stack = [None] * 67
    widget = GameOverWidget(
        0, 144, 128, 72,
        update_callback=lambda *a, **k: None,
        game_state=game_state,
        led_off_callback=lambda: None,
    )
    widget._on_game_over("1-0", "CHECKMATE")
    assert widget.move_count == 34

    from PIL import Image

    sprite = Image.new("1", (128, 72), 255)
    widget.render(sprite)
    assert widget._moves_text.text == "34 moves"
