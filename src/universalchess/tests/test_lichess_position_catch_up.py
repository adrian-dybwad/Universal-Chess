"""Applying a remote move list must set up the physical board.

Why these tests exist
---------------------
Joining a Lichess game in progress replays the server's UCIs onto the logical
board. Resume already does this for a saved game and then enters correction
when the pieces still show the opening. Catch-up must do the same, or the
e-paper shows the live position while the LEDs never ask for a setup.

How a regression manifests
--------------------------
``apply_uci_history`` returns 0, the FEN stays at start, or correction mode
is not entered when the sensors still read the opening.
"""

from unittest.mock import MagicMock

import chess
import pytest

pytest.importorskip("chess")

from universalchess.managers.game.game_manager import GameManager
from universalchess.state.chess_game import ChessGameState, reset_chess_game
from universalchess.utils.led import LedCallbacks

AFTER_NF3 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"


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
    """A GameManager on a fresh standard game, with LEDs stubbed."""
    reset_chess_game()
    manager = GameManager(save_to_database=False)
    manager.set_led_callbacks(_noop_led())
    yield manager
    manager._stop_event.set()


def test_apply_uci_history_replays_onto_the_logical_board_and_enters_correction(
    gm, monkeypatch
):
    """A mid-game list from the opening occupancy must enter correction.

    How the regression manifests: FEN stays at start, or correction_mode is
    inactive so the LEDs never guide the setup.
    """
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.board.getChessState",
        lambda: bytearray(ChessGameState.STARTING_POSITION_STATE),
    )
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.board.beep",
        lambda *a, **k: None,
    )
    gm._provide_correction_guidance = MagicMock()
    gm._clock_service = MagicMock()

    applied = gm.apply_uci_history(["e2e4", "e7e5", "g1f3"])

    assert applied == 3
    assert gm.chess_board.fen() == AFTER_NF3
    assert gm.correction_mode.is_active
    gm._provide_correction_guidance.assert_called_once()
    gm._clock_service.sync_move_counters_to_position.assert_called_once()


def test_apply_uci_history_skips_correction_when_the_pieces_already_match(
    gm, monkeypatch
):
    """If the sensors already show the live position, play continues.

    How the regression manifests: correction is entered anyway and the side
    to move is never prompted.
    """
    turns = []
    gm.event_callback = lambda event, *a, **k: turns.append(event)
    gm._clock_service = MagicMock()

    def occupancy_after_replay():
        return gm._chess_board_to_state(gm.chess_board)

    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.board.getChessState",
        occupancy_after_replay,
    )
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.board.beep",
        lambda *a, **k: None,
    )

    gm.apply_uci_history(["e2e4", "e7e5"])

    assert gm.correction_mode.is_active is False
    assert turns, "turn event must fire when the pieces already match"
