"""Tests for the game id carried in broadcast game state.

Why these tests exist
---------------------
The web live board resolves per-move AI coach statements via the current game's
database id, which travels in each broadcast. These tests pin that the id set on
the side channel reaches the broadcast payload, that a reset (None) does not leak
a stale id into a new game, and that the field round-trips through JSON and stays
backward compatible with payloads that predate it. A regression would leave the
live coach panel unable to identify the game (permanently hidden) or, worse, point
it at the previous game's moves.
"""

import pytest

import universalchess.services.game_broadcast as gb
from universalchess.services.game_broadcast import (
    GameState,
    broadcast_game_state,
    get_current_game_id,
    set_current_game_id,
)

FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class _CapturingBroadcaster:
    """Captures the last GameState instead of sending it over a socket."""

    def __init__(self):
        self.last = None

    def broadcast(self, state):
        self.last = state
        return True


@pytest.fixture
def captured(monkeypatch):
    """Redirect broadcasts to a capturer and reset the id side channel each test."""
    cap = _CapturingBroadcaster()
    monkeypatch.setattr(gb, "get_broadcaster", lambda: cap)
    set_current_game_id(None)
    yield cap
    set_current_game_id(None)


def test_side_channel_game_id_reaches_broadcast(captured):
    # The id set on the side channel (mirrored from GameManager) must appear in the
    # broadcast so the live board can address this game's coach statements.
    set_current_game_id(42)
    assert get_current_game_id() == 42
    broadcast_game_state(fen=FEN)
    assert captured.last.game_id == 42


def test_explicit_game_id_overrides_side_channel(captured):
    # An explicitly passed id wins over the side-channel fallback, matching how
    # pending_move behaves, so a caller that knows the id is authoritative.
    set_current_game_id(1)
    broadcast_game_state(fen=FEN, game_id=99)
    assert captured.last.game_id == 99


def test_reset_clears_game_id_so_new_game_does_not_inherit_old(captured):
    # After a reset (None) the broadcast must carry no id, so a new game never
    # inherits the previous game's id (which would coach new moves against old
    # rows). Regression guard for the cross-game leak this field is meant to avoid.
    set_current_game_id(7)
    broadcast_game_state(fen=FEN)
    assert captured.last.game_id == 7

    set_current_game_id(None)
    broadcast_game_state(fen=FEN)
    assert captured.last.game_id is None


def test_game_id_round_trips_through_json():
    # The id must survive serialization to reach the browser unchanged.
    state = GameState(fen=FEN, game_id=13)
    assert GameState.from_json(state.to_json()).game_id == 13


def test_missing_game_id_in_json_defaults_to_none():
    # A payload predating the field (no game_id key) must deserialize with game_id
    # None rather than raising, keeping older/newer components interoperable.
    import json

    legacy = json.dumps({"type": "game_state", "fen": FEN, "pgn": ""})
    assert GameState.from_json(legacy).game_id is None
