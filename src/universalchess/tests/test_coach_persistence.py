"""Tests for per-move coach statement persistence (coach_persistence.py).

Why these tests exist
---------------------
Coach statements are stored on the GameMove row for a played ply so the AI
service is queried at most once per move. These tests pin the 1-based ply ->
GameMove mapping (which skips the initial-position row), the save/read round
trip, and the out-of-range / uninitialized-game guards, using an in-memory
SQLite session. A regression would attach a statement to the wrong move (so the
board coaches move N with move M's text) or silently drop writes.
"""

import pytest

from universalchess.managers.game.coach_persistence import (
    get_coach_statement,
    get_move_context,
    get_move_evals,
    save_coach_statement,
    save_coach_statement_if_absent,
)


def _set_evals(session, gid, evals_by_uci):
    """Write eval_score onto the given played-move rows and commit."""
    models = session.models
    for uci, score in evals_by_uci.items():
        row = session.query(models.GameMove).filter_by(gameid=gid, move=uci).first()
        row.eval_score = score
    session.commit()


@pytest.fixture
def session(monkeypatch):
    """In-memory DB session with one game: initial position + three plies.

    Redirects the DB URI to in-memory before importing models (whose import
    builds an engine), mirroring the shared test pattern, then binds a fresh
    in-memory engine so the mapping is exercised in isolation.
    """
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

    # Initial-position row (move == "") followed by three played plies. The mapping
    # must skip the initial row so ply 1 addresses the first real move.
    session.add(models.GameMove(gameid=gid, move="", fen="startpos"))
    for index, uci in enumerate(["e2e4", "e7e5", "g1f3"], start=1):
        session.add(models.GameMove(gameid=gid, move=uci, fen=f"fen{index}"))
    session.commit()

    session.game_db_id = gid
    yield session
    session.close()


def test_save_then_get_round_trips(session):
    # A saved statement must read back verbatim for the same ply -- the core
    # "fetch once, reuse forever" contract.
    gid = session.game_db_id
    assert save_coach_statement(gid, 2, "Black mirrors in the center.", session=session) is True
    assert get_coach_statement(gid, 2, session=session) == "Black mirrors in the center."


def test_get_is_none_before_any_save(session):
    # A ply with no stored statement must return None so the coordinator knows to
    # fetch it (rather than showing empty text as if it were coached).
    assert get_coach_statement(session.game_db_id, 1, session=session) is None


def test_ply_mapping_skips_initial_row(session):
    # ply 1 must map to the first *played* move (e2e4), not the initial-position
    # row. Regression: an off-by-one would coach the wrong move and leave the
    # initial row carrying a statement it should never have.
    gid = session.game_db_id
    save_coach_statement(gid, 1, "Grabs the center.", session=session)

    models = session.models
    first_move = session.query(models.GameMove).filter_by(gameid=gid, move="e2e4").first()
    initial_row = session.query(models.GameMove).filter_by(gameid=gid, move="").first()
    assert first_move.coach_statement == "Grabs the center."
    assert initial_row.coach_statement is None


def test_out_of_range_ply_is_rejected(session):
    # A ply beyond the played moves must not create/attach anything and read back
    # as None, guarding against writing past the move list.
    gid = session.game_db_id
    assert save_coach_statement(gid, 4, "phantom", session=session) is False
    assert get_coach_statement(gid, 4, session=session) is None


def test_uninitialized_game_is_rejected(session):
    # Before a game exists game_db_id is < 0; both helpers must no-op (never query)
    # so a coach fetch during the very first move can't corrupt or crash.
    assert get_coach_statement(-1, 1, session=session) is None
    assert save_coach_statement(-1, 1, "x", session=session) is False


def test_save_overwrites_existing_statement(session):
    # Saving again must replace the stored text so a deliberate refetch can update
    # a stale statement rather than appending or being ignored.
    gid = session.game_db_id
    save_coach_statement(gid, 3, "first", session=session)
    save_coach_statement(gid, 3, "second", session=session)
    assert get_coach_statement(gid, 3, session=session) == "second"


def test_save_if_absent_writes_and_returns_when_empty(session):
    # On an empty row, save-if-absent must store the text and return it as the
    # canonical value -- the first-writer path for a never-before-coached move.
    gid = session.game_db_id
    result = save_coach_statement_if_absent(gid, 2, "First writer.", session=session)
    assert result == "First writer."
    assert get_coach_statement(gid, 2, session=session) == "First writer."


def test_save_if_absent_does_not_overwrite_and_returns_existing(session):
    # The core convergence guarantee: once a statement exists, a second writer must
    # NOT overwrite it and must receive the existing text back, so board and web
    # both adopt whoever committed first. Regression: overwriting here is exactly
    # the bug where board and web show different text for the same move.
    gid = session.game_db_id
    save_coach_statement_if_absent(gid, 2, "Winner.", session=session)
    loser = save_coach_statement_if_absent(gid, 2, "Loser (later).", session=session)
    assert loser == "Winner."
    assert get_coach_statement(gid, 2, session=session) == "Winner."


def test_save_if_absent_out_of_range_returns_none(session):
    # A ply past the move list has no row to claim, so it must return None (the
    # caller then shows its own text) and store nothing.
    gid = session.game_db_id
    assert save_coach_statement_if_absent(gid, 99, "phantom", session=session) is None


def test_save_if_absent_uninitialized_game_returns_none(session):
    # Before a game exists (game_db_id < 0) it must no-op and return None rather
    # than querying, matching the other helpers' guard.
    assert save_coach_statement_if_absent(-1, 1, "x", session=session) is None


def test_move_evals_returns_before_and_after(session):
    # For a middle ply, before-eval is the *previous* ply's score and after-eval is
    # this ply's score. A regression that returns the same row for both, or shifts
    # the index, would feed the coach a swing of zero (or the wrong move's swing).
    gid = session.game_db_id
    _set_evals(session, gid, {"e2e4": 30, "e7e5": 20, "g1f3": 40})
    assert get_move_evals(gid, 2, session=session) == (30, 20)


def test_move_evals_first_ply_has_no_before(session):
    # Ply 1's predecessor is the initial position, whose row stores no analysis
    # score, so before-eval must be None (not 0, which would imply an even position
    # was actually evaluated). After-eval is still the first move's score.
    gid = session.game_db_id
    _set_evals(session, gid, {"e2e4": 30})
    assert get_move_evals(gid, 1, session=session) == (None, 30)


def test_move_evals_unanalysed_move_is_none(session):
    # Moves default to eval_score=None until analysed; the lookup must surface that
    # as None so the coach prompt omits eval context rather than fabricating a 0.
    assert get_move_evals(session.game_db_id, 3, session=session) == (None, None)


def test_move_evals_out_of_range_ply(session):
    # A ply beyond the played moves must return (None, None) without indexing past
    # the list, guarding against an IndexError on the coach worker thread.
    assert get_move_evals(session.game_db_id, 4, session=session) == (None, None)


def test_move_evals_uninitialized_game(session):
    # Before a game exists game_db_id is < 0; the lookup must no-op to (None, None)
    # so enrichment during the first move can't query a non-existent game.
    assert get_move_evals(-1, 1, session=session) == (None, None)


def test_move_context_first_ply_uses_initial_row_fen(session):
    # Ply 1's before-position is the initial-position row's fen (not a played
    # row), so a game started from a custom FEN still coaches move 1 against the
    # real starting position. Regression: indexing played rows for ply 1 would
    # raise/return the wrong fen and coach the opening move from a later position.
    assert get_move_context(session.game_db_id, 1, session=session) == ("startpos", "e2e4")


def test_move_context_middle_ply_uses_previous_played_fen(session):
    # For ply N>1 the before-position is the *previous* played ply's stored fen and
    # the move is this ply's uci. A regression that reused this ply's own fen (the
    # position after the move) would coach the move from the wrong side to move.
    assert get_move_context(session.game_db_id, 2, session=session) == ("fen1", "e7e5")
    assert get_move_context(session.game_db_id, 3, session=session) == ("fen2", "g1f3")


def test_move_context_out_of_range_ply(session):
    # A ply beyond the played moves must return None (not index past the list) so
    # the endpoint responds with an out-of-range error rather than crashing.
    assert get_move_context(session.game_db_id, 4, session=session) is None


def test_move_context_uninitialized_game(session):
    # Before a game exists game_db_id is < 0; the lookup must no-op to None so a
    # coach request for a not-yet-created game produces no statement.
    assert get_move_context(-1, 1, session=session) is None
