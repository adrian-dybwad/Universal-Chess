"""Tests that loading a position makes it the game's authoritative START.

Why this exists
---------------
The web live board does not analyse ``gameState.fen`` directly; it navigates and
analyses by the server-computed per-ply ``positions`` (``history_positions()``),
which are rebuilt from ``_start_fen``. So a position LOADER must establish the
loaded FEN as the game's start (``configure_start``, which sets ``_start_fen``),
not merely swap the live board (``set_position``, which leaves ``_start_fen``).

The regression this guards: the loaders used ``set_position``. The board then
rendered the loaded position correctly, but ``history_positions()``/``start_fen``
still described the previous start (the standard opening, no moves). The web
best-move indicator therefore analysed the opening and drew an opening
knight-development move instead of the loaded position -- e.g. after loading a
promotion test the arrow showed a knight developing rather than the promotion.
"""

import chess
import pytest

pytest.importorskip("chess")

from universalchess.managers.game.game_manager import GameManager
from universalchess.state import get_chess_game
from universalchess.state.chess_game import ChessGameState, reset_chess_game
from universalchess.utils.led import LedCallbacks

# The promotion_capture test position (positions.ini): White pawn on a7 to
# promote by capturing the rook on b8. Chosen because it is unmistakably not the
# opening, so a loader that left the start at the standard opening is obvious.
PROMOTION_FEN = "1r6/P7/8/8/8/8/8/4K2k w - - 0 1"


def _noop_led() -> LedCallbacks:
    return LedCallbacks(
        from_to=lambda *a, **k: None,
        array=lambda *a, **k: None,
        single=lambda *a, **k: None,
        off=lambda *a, **k: None,
        from_to_hint=lambda *a, **k: None,
        array_hint=lambda *a, **k: None,
        array_fast=lambda *a, **k: None,
        from_to_fast=lambda *a, **k: None,
        single_fast=lambda *a, **k: None,
    )


@pytest.fixture
def gm():
    """A GameManager on a fresh standard game, with LEDs stubbed.

    reset_chess_game() clears the shared game-state singleton so each test starts
    from the standard opening. The task worker thread is stopped on teardown so
    it does not leak across tests.
    """
    reset_chess_game()
    manager = GameManager(save_to_database=False)
    manager.set_led_callbacks(_noop_led())
    yield manager
    manager._stop_event.set()


def test_apply_setup_position_makes_it_the_authoritative_start(gm):
    """A position adopted as a fresh game must become the game's START.

    Guards the Chessnut "puzzle matched -> new game from this position" path.
    Regression manifestation: ``fen`` becomes the adopted position but
    ``start_fen`` and ``history_positions()[0]`` stay at the standard opening, so
    the web analyses the opening (opening knight move) instead of the position.
    The full history shape is asserted (single start entry, no moves) so a stray
    move entry or a wrong start FEN is also caught.
    """
    gm.apply_setup_position(PROMOTION_FEN)

    state = get_chess_game()
    assert state.fen == PROMOTION_FEN
    assert state.start_fen == PROMOTION_FEN
    assert state.history_positions() == [
        {"fen": PROMOTION_FEN, "san": None, "uci": None}
    ]


def test_set_position_leaves_start_unchanged_documenting_loader_requirement():
    """``set_position`` changes the board but deliberately NOT the game's start.

    This pins the contract that keeps ``set_position`` distinct from
    ``configure_start``: it is the mid-game position mutation (used during play,
    e.g. adopting a corrected position without redefining where the game began),
    so it must leave ``_start_fen`` untouched. A position loader must therefore
    use ``configure_start`` instead -- if one regressed back to ``set_position``,
    the board would look right while ``history_positions()``/``start_fen`` (the web
    analysis and best-move source) still described the previous start. Asserted
    directly: ``fen`` updates while ``start_fen`` and ``history_positions()[0]``
    remain the standard opening.
    """
    state = ChessGameState()

    state.set_position(PROMOTION_FEN)

    assert state.fen == PROMOTION_FEN
    assert state.start_fen == chess.STARTING_FEN
    assert state.history_positions()[0]["fen"] == chess.STARTING_FEN
