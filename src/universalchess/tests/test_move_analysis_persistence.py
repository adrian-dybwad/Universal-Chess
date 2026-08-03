"""Tests for persisting a position's evaluation and best move onto its move row.

Why these tests exist
---------------------
Two bugs made the persisted ``eval_score`` untrustworthy, and both are invisible
without a test because neither raises:

1. The value was read from the live ``AnalysisState`` singleton, whose score
   defaults to 0.0. With ``analysis_mode`` off nothing ever analysed anything,
   so every row was written with a literal 0 -- indistinguishable from a
   genuinely equal position, and charted as one.

2. ``push_move`` only *enqueues* analysis, while the database task ran straight
   after. The row for ply N was therefore written with the score of ply N-1.
   ``coach_persistence.get_move_evals`` documents that a row's ``eval_score`` is
   the eval *after* that ply, so every eval_before/eval_after pair was skewed by
   one move.

The fix inverts the direction: the move row is written with NULL, and the
analysis result backfills the row matching its own FEN when the search finishes.
Because the two can complete in either order, the insert also picks up a result
that is already available.

How a regression manifests
--------------------------
Reintroducing (1) fills the column with zeros, so the eval chart is a flat line
at 0.0 and accuracy is computed from nothing. Reintroducing (2) shifts every
evaluation one ply, so a blunder is attributed to the move before it.
"""

import chess
import chess.engine
import pytest

from universalchess.managers.game.move_persistence import (
    persist_move_and_maybe_create_game,
    update_move_analysis,
)
from universalchess.services.analysis import MATE_SCORE_CP, PositionAnalysis


FEN_AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
FEN_AFTER_E5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


@pytest.fixture
def session(monkeypatch):
    """In-memory DB session with one game: initial position plus two plies."""
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

    game = models.Game(source="test")
    session.add(game)
    session.flush()
    gid = game.id

    # move_persistence resolves the ORM models through its own module-global
    # `_get_models` (bound at import from deferred_imports). Patch that binding,
    # not deferred_imports, so it returns this fixture's models; the real
    # deferred import waits on a background thread that never completes under
    # the test harness and would hang the call.
    import universalchess.managers.game.move_persistence as mp

    monkeypatch.setattr(mp, "_get_models", lambda: models)

    session.add(models.GameMove(gameid=gid, move="", fen=chess.STARTING_FEN))
    session.add(models.GameMove(gameid=gid, move="e2e4", fen=FEN_AFTER_E4))
    session.add(models.GameMove(gameid=gid, move="e7e5", fen=FEN_AFTER_E5))
    session.commit()

    session.game_db_id = gid
    yield session
    session.close()


def _row(session, uci):
    models = session.models
    return session.query(models.GameMove).filter_by(
        gameid=session.game_db_id, move=uci).first()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_best_move_column_round_trips(session):
    """The new column stores and reads back a UCI move.

    Regression: without a dedicated column the arrow cannot be shown when
    reviewing a past game, because nothing persisted the engine's choice.
    """
    row = _row(session, "e2e4")
    row.best_move = "g1f3"
    session.commit()

    assert _row(session, "e2e4").best_move == "g1f3"


def test_best_move_defaults_to_null(session):
    """A row written without analysis reports absence, not a placeholder move.

    Regression: defaulting to a move (or empty string) would draw an arrow
    pointing at a move no engine ever recommended.
    """
    assert _row(session, "e2e4").best_move is None


def test_migration_adds_best_move_to_an_existing_database(tmp_path, monkeypatch):
    """An existing database gains the column, and re-running is harmless.

    Regression manifests on upgrade rather than on a fresh install: every write
    of best_move raises OperationalError ("no such column") on a database
    created before this change, so move persistence fails for existing users.
    The second run guards the idempotence of the column-existence check.
    """
    from sqlalchemy import create_engine, inspect, text

    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_path}")

    # A gameMove table shaped like the pre-change schema.
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE gameMove ("
            "id INTEGER PRIMARY KEY, gameid INTEGER, move_at DATETIME, "
            "move VARCHAR(10), fen VARCHAR(255), white_clock INTEGER, "
            "black_clock INTEGER, eval_score INTEGER, coach_statement TEXT)"
        ))
        conn.commit()

    from universalchess.db.models import apply_pending_migrations

    apply_pending_migrations(engine)
    assert "best_move" in [c["name"] for c in inspect(engine).get_columns("gameMove")]

    apply_pending_migrations(engine)  # must not raise on an already-migrated DB
    assert "best_move" in [c["name"] for c in inspect(engine).get_columns("gameMove")]


# ---------------------------------------------------------------------------
# Backfill by FEN
# ---------------------------------------------------------------------------


def test_analysis_backfills_the_row_for_its_own_position(session):
    """A completed analysis updates the ply it actually analysed.

    This is the off-by-one regression test. The result for the position after
    e7e5 must land on the e7e5 row and must not touch e2e4.

    Regression manifests as the value appearing on the previous row, so a
    blunder is attributed to the move played before it.
    """
    result = PositionAnalysis(FEN_AFTER_E5, -35, None, "g1f3")

    assert update_move_analysis(session, game_db_id=session.game_db_id, result=result) is True

    assert _row(session, "e7e5").eval_score == -35
    assert _row(session, "e7e5").best_move == "g1f3"
    assert _row(session, "e2e4").eval_score is None
    assert _row(session, "e2e4").best_move is None


def test_backfill_stores_mate_as_the_shared_sentinel(session):
    """Mate is persisted as the +/-10000 both surfaces already interpret as "M".

    Regression: storing the raw mate distance (say 3) would be charted as a
    +0.03 pawn edge in a position that is actually forced mate.
    """
    result = PositionAnalysis(FEN_AFTER_E4, None, 3, "d1h5")

    update_move_analysis(session, game_db_id=session.game_db_id, result=result)

    assert _row(session, "e2e4").eval_score == MATE_SCORE_CP


def test_backfill_for_an_unknown_position_changes_nothing(session):
    """A result whose FEN has no row is reported as unapplied, not an error.

    Regression manifests during review gap-fill and after a takeback, where
    analysis legitimately completes for a position no longer in the game.
    Raising there would abort the analysis worker; writing it to some other row
    would corrupt a real ply.
    """
    result = PositionAnalysis("not-a-position-in-this-game", 100, None, "e2e4")

    assert update_move_analysis(session, game_db_id=session.game_db_id, result=result) is False

    assert _row(session, "e2e4").eval_score is None
    assert _row(session, "e7e5").eval_score is None


def test_backfill_is_scoped_to_its_own_game(session):
    """A position analysed in one game never writes to another game's row.

    Regression: the opening position and short transpositions recur across
    games, so an unscoped FEN match would overwrite an unrelated game's eval.
    """
    models = session.models
    other = models.Game(source="test")
    session.add(other)
    session.flush()
    session.add(models.GameMove(gameid=other.id, move="e2e4", fen=FEN_AFTER_E4))
    session.commit()

    update_move_analysis(
        session, game_db_id=session.game_db_id,
        result=PositionAnalysis(FEN_AFTER_E4, 25, None, "e7e5"))

    other_row = session.query(models.GameMove).filter_by(gameid=other.id).first()
    assert other_row.eval_score is None
    assert _row(session, "e2e4").eval_score == 25


# ---------------------------------------------------------------------------
# Insert-time behaviour
# ---------------------------------------------------------------------------


def _persist(session, *, fen_after, result=None, is_first_move=False, game_db_id=None):
    return persist_move_and_maybe_create_game(
        session=session,
        is_first_move=is_first_move,
        current_game_db_id=session.game_db_id if game_db_id is None else game_db_id,
        source_file="test",
        game_info={},
        fen_before_move=chess.STARTING_FEN,
        move_uci="g1f3",
        fen_after_move=fen_after,
        white_clock=None,
        black_clock=None,
        analysis=result,
    )


def test_move_row_is_written_null_when_no_analysis_exists(session):
    """With analysis off (or not yet finished) the column stays NULL.

    This is the "0 instead of NULL" regression. NULL says "not analysed"; 0
    says "dead equal", and only one of those is true.
    """
    fen = "rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 1"
    _persist(session, fen_after=fen)

    row = _row(session, "g1f3")
    assert row.eval_score is None
    assert row.best_move is None


def test_insert_picks_up_an_analysis_that_already_finished(session):
    """A result available before the row exists is written with the insert.

    The analysis worker and the database task run on different threads, so
    either can finish first. Regression manifests as a permanently NULL row
    whenever analysis wins the race, since the backfill already ran and found
    nothing to update.
    """
    fen = "rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 1"

    _persist(session, fen_after=fen, result=PositionAnalysis(fen, 18, None, "d7d5"))

    row = _row(session, "g1f3")
    assert row.eval_score == 18
    assert row.best_move == "d7d5"
