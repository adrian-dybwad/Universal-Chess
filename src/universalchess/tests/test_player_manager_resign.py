"""Tests for telling both players that a side resigned.

A resignation reaches the board through three gestures -- the BACK menu, the
kings-in-center gesture and the king-lift -- and every one of them has to record
the result AND tell both players, because a remote player (Lichess) only leaves
the server's game when it is told. The board had two byte-identical copies of
that notification nested inside its game-start function, one per gesture, so a
third gesture could easily record a resignation the remote opponent never saw.

The broadcast belongs to the player manager, beside ``on_takeback``, which
answers the same shape of question.
"""

import chess
import pytest

from universalchess.players.manager import PlayerManager


class _RecordingPlayer:
    """A player that records the resignations it is told about."""

    def __init__(self, name):
        self.name = name
        self.color = chess.WHITE
        self.player_type = type("PlayerType", (), {"name": "HUMAN"})()
        self.resignations = []

    def on_resign(self, color):
        self.resignations.append(color)

    def set_move_callback(self, callback):
        pass

    def set_pending_move_callback(self, callback):
        pass

    def set_status_callback(self, callback):
        pass

    def set_error_callback(self, callback):
        pass

    def set_ready_callback(self, callback):
        pass


@pytest.fixture
def players(monkeypatch):
    """A manager over two recording players, with state updates suppressed."""
    monkeypatch.setattr(PlayerManager, "_update_players_state", lambda self: None)
    white, black = _RecordingPlayer("White"), _RecordingPlayer("Black")
    return white, black, PlayerManager(white, black)


@pytest.mark.parametrize("resigning", [chess.WHITE, chess.BLACK])
def test_both_players_are_told_who_resigned(players, resigning):
    """The resignation reaches both players, naming the side that resigned.

    Why: the resigning side's own player object has to stop thinking, and the
    opponent's -- a Lichess player -- has to resign the server's game, or the
    game stays open on Lichess after the board shows it finished. Both colours
    are exercised because a copy that hard-coded one side would pass on the
    other. How a regression manifests: one player is skipped, so either an
    engine keeps searching or a Lichess game is left running.
    """
    white, black, manager = players

    manager.on_resign(resigning)

    assert white.resignations == [resigning]
    assert black.resignations == [resigning]
