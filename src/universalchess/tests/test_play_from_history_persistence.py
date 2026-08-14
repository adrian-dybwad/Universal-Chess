#!/usr/bin/env python3
"""Tests for building a recorded game from a transferred move history.

Why these tests exist
---------------------
"Play Game from here" transfers the reviewed game's moves (up to the viewed ply)
into a fresh in-progress game so the live board continues with the full history
and PGN, not from a bare FEN. ``create_game_from_moves`` is that builder. These
tests guard:

1. The whole sequence is persisted as one game: an initial-position row plus one
   row per move, positioned at the final FEN, with a NULL result so resume treats
   it as in progress (continued play) rather than a finished/abandoned game.
2. The start FEN is stored only when non-standard (so resume replays from the
   right start), matching the single-move persistence rule.
3. Illegal/empty input aborts cleanly (returns None) and leaves no game behind --
   a game whose stored moves cannot replay would silently fail to resume, which
   is worse than not creating it.
4. When a FEN -> analysis lookup is supplied, eval_score and best_move land on
   the matching rows so resume can restore the graph. Resume resets the live
   analysis cache; a fork that copies only UCIs leaves those columns NULL and
   the new game's graph empty. Unanalysed plies stay NULL, not a fabricated 0.
"""

import chess
import pytest

from universalchess.managers.game.move_persistence import create_game_from_moves

STANDARD = chess.STARTING_FEN
# A mid-game, standard-variant position (king-and-pawn endgame): valid but not the
# opening, so a game begun here is "non-standard" and must persist its start FEN.
CUSTOM_START = "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"


@pytest.fixture
def session(monkeypatch):
    """In-memory DB session, mirroring test_custom_start_persistence's pattern."""
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

    # create_game_from_moves -> persist_move_and_maybe_create_game resolves models
    # via the module-global `_get_models`; patch it to this fixture's models (the
    # real deferred import waits on a background thread that never completes here).
    import universalchess.managers.game.move_persistence as mp

    monkeypatch.setattr(mp, "_get_models", lambda: models)
    yield session
    session.close()


def _final_fen(start_fen, moves_uci, chess960=False):
    board = chess.Board(start_fen, chess960=chess960)
    for uci in moves_uci:
        board.push(chess.Move.from_uci(uci))
    return board.fen()


def test_transfers_full_history_as_in_progress_game(session):
    """The move list becomes one in-progress game with initial + per-move rows.

    Regression: if only the first (or last) move persisted, resume would replay a
    truncated history and land on the wrong position. Asserting the exact row set
    (initial + each UCI in order) and the final FEN catches drops, reordering and
    off-by-one. A non-NULL result would make resume show a finished game instead
    of continuing play, so it must be NULL.
    """
    moves = ["e2e4", "e7e5", "g1f3"]
    game_id = create_game_from_moves(
        session,
        start_fen=STANDARD,
        moves_uci=moves,
        game_info={"white": "Alice", "black": "Bob"},
    )
    assert game_id is not None

    models = session.models
    game = session.query(models.Game).filter_by(id=game_id).first()
    assert game.result is None
    assert game.white == "Alice"
    assert game.black == "Bob"
    # Standard opening -> NULL start_fen (common-case rule preserved).
    assert game.start_fen is None
    assert bool(game.chess960) is False

    rows = (
        session.query(models.GameMove)
        .filter_by(gameid=game_id)
        .order_by(models.GameMove.id)
        .all()
    )
    # Initial-position row (empty move) + one row per transferred move.
    assert [r.move for r in rows] == ["", *moves]
    assert rows[0].fen == STANDARD
    assert rows[-1].fen == _final_fen(STANDARD, moves)
    # No analysis was supplied, so eval columns stay NULL. Fabricating 0 would
    # draw a flat graph of "dead equal" instead of an empty one.
    assert [r.eval_score for r in rows] == [None, None, None, None]
    assert [r.best_move for r in rows] == [None, None, None, None]


def test_transfers_analysis_onto_the_matching_move_rows(session):
    """Eval and best-move of the source plies must land on the new game.

    Why: resume restores the analysis graph from GameMove.eval_score. A fork
    that copies only UCIs leaves those columns NULL, so the new game's graph
    is empty after the analysis service is reset. How a regression manifests:
    eval_score/best_move stay None on a ply the lookup analysed, or a score
    is written onto the wrong ply's row.
    """
    from universalchess.services.analysis import PositionAnalysis

    board = chess.Board()
    board.push_uci("e2e4")
    after_e4 = board.fen()
    board.push_uci("e7e5")
    after_e5 = board.fen()

    analysed = {
        STANDARD: PositionAnalysis(STANDARD, 15, None, "e2e4"),
        after_e4: PositionAnalysis(after_e4, 30, None, "e7e5"),
        after_e5: PositionAnalysis(after_e5, -12, None, "g1f3"),
    }

    game_id = create_game_from_moves(
        session,
        start_fen=STANDARD,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        game_info={},
        analysis_for_fen=analysed.get,
    )
    assert game_id is not None

    rows = (
        session.query(session.models.GameMove)
        .filter_by(gameid=game_id)
        .order_by(session.models.GameMove.id)
        .all()
    )
    by_move = {r.move: r for r in rows}
    assert by_move[""].eval_score == 15
    assert by_move[""].best_move == "e2e4"
    assert by_move["e2e4"].eval_score == 30
    assert by_move["e2e4"].best_move == "e7e5"
    assert by_move["e7e5"].eval_score == -12
    assert by_move["e7e5"].best_move == "g1f3"
    # g1f3 was not in the lookup: that ply stays unanalysed, not a fabricated 0.
    assert by_move["g1f3"].eval_score is None
    assert by_move["g1f3"].best_move is None


def test_non_standard_start_persists_start_fen(session):
    """A mid-game start records its FEN so resume replays from the right board.

    Regression: storing NULL would make resume assume the standard opening, where
    the transferred moves are illegal, so the game would not resume.
    """
    moves = ["e2e3"]
    game_id = create_game_from_moves(
        session,
        start_fen=CUSTOM_START,
        moves_uci=moves,
        game_info={},
    )
    assert game_id is not None
    game = session.query(session.models.Game).filter_by(id=game_id).first()
    assert game.start_fen == CUSTOM_START
    assert bool(game.chess960) is False


def test_illegal_move_aborts_without_creating_game(session):
    """An illegal move in the sequence returns None and leaves no game row.

    Regression: a partially written game (rows up to the bad move) would resume to
    a wrong/truncated position. Failing atomically forces Play Game to report a
    failure instead of corrupting history. "e2e5" is illegal from the opening
    (a pawn cannot jump to the 5th rank), so it must abort at ply 1.
    """
    game_id = create_game_from_moves(
        session,
        start_fen=STANDARD,
        moves_uci=["e2e4", "e2e5"],
        game_info={},
    )
    assert game_id is None
    # Validation happens fully before any write, so the first (legal) move must
    # NOT have been committed: no Game row exists at all. If persistence ran during
    # validation, a partial one-move game would remain and this count would be 1.
    assert session.query(session.models.Game).count() == 0
    assert session.query(session.models.GameMove).count() == 0


def test_empty_move_list_returns_none(session):
    """An empty history has no first move to anchor the game, so returns None.

    Guards the caller contract: Play Game at the start position (ply 0) must fall
    back to the plain position setup, not attempt a zero-move game.
    """
    assert (
        create_game_from_moves(session, start_fen=STANDARD, moves_uci=[], game_info={})
        is None
    )
