#!/usr/bin/env python3
"""Tests for recorded games started from a non-standard (non-960) position.

Why these tests exist
---------------------
"Play Game from here" on the review page sets the board up from a mid-game FEN
and records the resulting game (``_start_from_position(..., record=True)``). For
that game to resume correctly after a restart, two things must hold:

1. The game record must persist the start FEN even though it is NOT a Chess960
   game. Previously ``start_fen`` was stored only for chess960, so a standard-
   variant game begun from a mid-game position stored NULL and resume assumed the
   standard opening -- replaying its moves there would fail. The persistence now
   stores ``start_fen`` for any non-standard start.
2. Replaying the stored moves must be done from that start. On the standard
   opening the moves are illegal, so the resume path must configure_start(start)
   first (main.py resume, generalized from chess960-only to any non-standard
   start). This mirrors ``test_960_replay_requires_configured_board``.

A game begun from the standard opening must still store a NULL start_fen so the
common case is unchanged.
"""

import chess
import pytest

from universalchess.managers.game.move_persistence import (
    persist_move_and_maybe_create_game,
)
from universalchess.state.chess_game import ChessGameState

# A mid-game, standard-variant position: king-and-pawn endgame. It is a valid
# board but not the standard opening, so a game begun here is "non-standard".
CUSTOM_START = "4k3/8/8/8/8/8/8/4K3 w - - 0 1"
# A king move ("e1e2") that is legal from CUSTOM_START but illegal from the
# standard opening (e2 holds a pawn there), so replay onto the standard opening
# provably fails -- exactly the resume divergence the fix prevents.
CUSTOM_MOVE_UCI = "e1e2"


@pytest.fixture
def session(monkeypatch):
    """In-memory DB session, mirroring test_chess960_persistence's pattern."""
    import universalchess.db.uri as uri

    monkeypatch.setattr(uri, "get_database_uri", lambda: "sqlite:///:memory:")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from universalchess.db import models

    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.models = models

    # persist_move_and_maybe_create_game resolves models via its module-global
    # `_get_models`; patch that binding so it returns this fixture's models (the
    # real deferred import waits on a background thread that never completes here).
    import universalchess.managers.game.move_persistence as mp

    monkeypatch.setattr(mp, "_get_models", lambda: models)
    yield session
    session.close()


def _persist_first_move(session, *, start_fen: str):
    """Create a standard-variant game whose first move begins at ``start_fen``."""
    board = chess.Board(start_fen)
    first_move = next(iter(board.legal_moves))
    fen_before = board.fen()
    board.push(first_move)
    game_db_id, committed = persist_move_and_maybe_create_game(
        session=session,
        is_first_move=True,
        current_game_db_id=-1,
        source_file="test",
        game_info={},
        fen_before_move=fen_before,
        move_uci=first_move.uci(),
        fen_after_move=board.fen(),
        white_clock=None,
        black_clock=None,
        chess960=False,
    )
    assert committed is True
    return session.query(session.models.Game).filter_by(id=game_db_id).first()


def test_persist_non_standard_start_stores_start_fen(session):
    """A non-960 game begun from a mid-game position records its start FEN.

    Regression: storing NULL (the old chess960-only rule) makes resume replay the
    game's moves from the standard opening, where they are illegal and diverge.
    The flag stays False because the variant is standard chess.
    """
    game = _persist_first_move(session, start_fen=CUSTOM_START)
    assert bool(game.chess960) is False
    assert game.start_fen == CUSTOM_START


def test_persist_standard_start_leaves_start_fen_null(session):
    """A game begun from the standard opening still stores a NULL start_fen.

    Pins the common case so the broadened persistence rule does not start writing
    a start_fen for ordinary games (which would trigger a needless configure_start
    on resume).
    """
    game = _persist_first_move(session, start_fen=chess.STARTING_FEN)
    assert bool(game.chess960) is False
    assert game.start_fen is None


def test_replay_requires_configured_start():
    """Stored moves reach the target only when replayed from the stored start.

    Reproduces the resume core for a non-standard start: on a state configured
    with the custom start the move applies and the FEN matches; on a default
    (standard-opening) state the same UCI is illegal and push_uci raises, so the
    position never reaches the target. This is why resume must configure_start the
    non-standard start before replaying, not only for chess960.
    """
    board = chess.Board(CUSTOM_START)
    board.push(chess.Move.from_uci(CUSTOM_MOVE_UCI))
    target_fen = board.fen()

    configured = ChessGameState()
    configured.configure_start(CUSTOM_START, chess960=False)
    configured.push_uci(CUSTOM_MOVE_UCI)
    assert configured.fen == target_fen

    # Default state starts from the standard opening; the custom move is illegal
    # there (e2 is occupied by a pawn), so replay raises and diverges.
    standard = ChessGameState()
    with pytest.raises(ValueError):
        standard.push_uci(CUSTOM_MOVE_UCI)
    assert standard.fen != target_fen
