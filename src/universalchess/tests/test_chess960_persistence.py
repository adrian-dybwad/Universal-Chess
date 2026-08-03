#!/usr/bin/env python3
"""Tests for Chess960 game persistence and resume reconstruction.

Why these tests exist
---------------------
Resuming a Chess960 game replays its stored moves. The stored castling moves use
the king-onto-rook encoding (e.g. "a1d1"/"e1h1"), which python-chess only accepts
on a board created with the chess960 flag. Two things must therefore survive a
persist -> resume round-trip:

1. The game record must store chess960=True and the generated start FEN, so the
   resume path can rebuild the correct variant/start (persist_move_and_maybe_
   create_game + _build_resume_data).
2. Replaying the stored moves must reproduce the last stored FEN ONLY when the
   board is configured as chess960 first; on a standard board a 960 castling move
   is illegal and replay diverges.

A standard game must keep chess960=False and a NULL start_fen (so the common case
is unchanged), which the parametrized round-trip pins alongside the 960 case.
"""

import chess
import pytest

from universalchess.managers.game.coach_persistence import get_game_chess960
from universalchess.managers.game.move_persistence import (
    persist_move_and_maybe_create_game,
)
from universalchess.state.chess960 import chess960_fen
from universalchess.state.chess_game import ChessGameState

# A Chess960 start with non-corner rooks so castling is king-onto-rook and the
# standard-board replay genuinely fails. Position with back rank BBQNNRKR.
FRC_START = chess960_fen(0)


@pytest.fixture
def session(monkeypatch):
    """In-memory DB session, mirroring test_game_result_persistence's pattern."""
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

    # persist_move_and_maybe_create_game resolves the ORM models via its own
    # module-global `_get_models` (bound at import from deferred_imports). Patch
    # that binding -- not deferred_imports -- so it returns this fixture's models;
    # the real deferred import waits on a background thread that never completes
    # under the test harness and would hang the call.
    import universalchess.managers.game.move_persistence as mp

    monkeypatch.setattr(mp, "_get_models", lambda: models)
    yield session
    session.close()


def _persist_first_move(session, *, chess960: bool, start_fen: str):
    """Create a game via the first-move path and return its Game row.

    Builds the first move from the given start so fen_before_move is the true
    start FEN (what the initial-position row and, for 960, the game record store).
    """
    board = chess.Board(start_fen, chess960=chess960)
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
        chess960=chess960,
    )
    assert committed is True
    return session.query(session.models.Game).filter_by(id=game_db_id).first()


def test_persist_960_stores_flag_and_start_fen(session):
    """A 960 game records chess960=True and its generated start FEN.

    Without these, resume cannot know the variant or rebuild the exact start;
    the game would replay onto a standard board and its castling moves would be
    rejected.
    """
    game = _persist_first_move(session, chess960=True, start_fen=FRC_START)
    assert game.chess960 is True
    assert game.start_fen == FRC_START


def test_persist_standard_leaves_flag_off_and_start_fen_null(session):
    """A standard game keeps chess960 falsey and a NULL start_fen.

    Pins the common case: storing a start_fen or a truthy flag for every game
    would change existing behavior and mislead the resume path into a needless
    configure_start.
    """
    game = _persist_first_move(
        session, chess960=False, start_fen=chess.STARTING_FEN
    )
    assert bool(game.chess960) is False
    assert game.start_fen is None


def test_get_game_chess960_reads_stored_flag(session):
    """The coach's variant lookup reflects the game's stored chess960 flag.

    The web coach statement endpoint uses this to build a reviewed move's board
    960-aware. Regression: reading False for a 960 game makes a king-onto-rook
    castle illegal on the rebuilt board, blanking the coached move text.
    """
    game_960 = _persist_first_move(session, chess960=True, start_fen=FRC_START)
    game_std = _persist_first_move(
        session, chess960=False, start_fen=chess.STARTING_FEN
    )
    assert get_game_chess960(game_960.id, session=session) is True
    assert get_game_chess960(game_std.id, session=session) is False


def test_get_game_chess960_defaults_false_for_unknown_game(session):
    """A missing/uninitialized game id yields False, not an error.

    Guards the safe default so a corrupt or not-yet-created game degrades to
    standard-chess coaching instead of raising on the review path.
    """
    assert get_game_chess960(-1, session=session) is False
    assert get_game_chess960(9999, session=session) is False


def test_960_replay_requires_configured_board():
    """Replaying stored 960 moves reaches the target only on a chess960 board.

    Reproduces the resume core: build a legal 960 castling sequence, then replay
    the UCI strings both ways. On a chess960-configured state the final FEN
    matches; on a standard board the castling move is illegal and the position
    diverges. This is exactly why _resume_game must configure_start(chess960=True)
    before replaying.
    """
    # Construct a short line that includes a 960 king-onto-rook castle. Use a
    # cleared-back-rank 960-style position where queenside castling is legal.
    setup_fen = "1k6/8/8/8/8/8/8/RK5R w KQ - 0 1"
    board = chess.Board(setup_fen, chess960=True)
    castle = chess.Move(chess.B1, chess.A1)  # king-onto-rook queenside
    assert board.is_castling(castle)
    board.push(castle)
    target_fen = board.fen()
    moves_uci = [castle.uci()]

    # Configured (chess960) replay reaches the target.
    configured = ChessGameState()
    configured.configure_start(setup_fen, chess960=True)
    for uci in moves_uci:
        configured.push_uci(uci)
    assert configured.fen == target_fen

    # Standard-board replay cannot apply the king-onto-rook castle: the move is
    # illegal there, so push_uci raises and the position stays at the start.
    standard = ChessGameState()
    standard.set_position(setup_fen)  # chess960 flag stays False
    with pytest.raises(ValueError):
        standard.push_uci(moves_uci[0])
    assert standard.fen != target_fen
