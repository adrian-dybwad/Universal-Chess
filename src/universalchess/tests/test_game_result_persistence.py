"""Tests for game result/termination persistence (database.py).

Why these tests exist
---------------------
To bring a *finished* game back exactly after a restart, the game's result and
how it ended must be stored, and a takeback that removes the deciding move must
clear them so the game is no longer recorded as over. These tests pin:

- ``update_game_result`` storing both result and termination;
- ``update_game_result`` leaving an existing termination untouched when a caller
  passes only a result (so a partial update never erases the reason);
- ``clear_game_result`` wiping both (the takeback path);

using an in-memory SQLite session. A regression here would make a resumed
resigned game either lose its game-over screen (missing termination) or a
taken-back mate wrongly reappear as finished (stale result).
"""

import pytest

from universalchess.managers.game.database import (
    clear_game_result,
    update_game_result,
)


@pytest.fixture
def session(monkeypatch):
    """In-memory DB session holding a single in-progress game.

    Redirects the DB URI to in-memory before importing models (whose import
    builds an engine), mirroring the shared test pattern, then binds a fresh
    in-memory engine so the helpers are exercised in isolation.
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

    # The helpers resolve the ORM models lazily via the game-manager deferred
    # import (_get_models), whose module-level state depends on background import
    # timing and can be None/stale under the full suite. Pin it to this fixture's
    # models so the helpers operate on the same mapped classes as the session.
    import universalchess.managers.game.database as db_module

    monkeypatch.setattr(db_module, "_get_models", lambda: models)

    game = models.Game(source="test")
    session.add(game)
    session.commit()
    session.game_db_id = game.id
    yield session
    session.close()


def _game(session):
    """Reload the fixture's single game row."""
    return session.query(session.models.Game).filter_by(id=session.game_db_id).first()


def test_update_stores_result_and_termination(session):
    """A finished game must persist both the result and the termination reason.

    Regression manifestation: dropping the termination write leaves a resumed
    resigned game unable to reproduce its game-over screen (result known, reason
    lost).
    """
    assert (
        update_game_result(
            session, session.game_db_id, "0-1", "Termination.RESIGN"
        )
        is True
    )

    game = _game(session)
    assert game.result == "0-1"
    assert game.termination == "Termination.RESIGN"


def test_update_without_termination_preserves_existing(session):
    """A result-only update must not erase a previously stored termination.

    Some callers know only the result. Passing termination=None must leave the
    stored reason intact. Regression manifestation: overwriting with NULL would
    lose the reason on any subsequent result-only write.
    """
    update_game_result(session, session.game_db_id, "1-0", "Termination.CHECKMATE")

    assert update_game_result(session, session.game_db_id, "1-0") is True

    game = _game(session)
    assert game.result == "1-0"
    assert game.termination == "Termination.CHECKMATE"


def test_clear_wipes_result_and_termination(session):
    """A takeback of the deciding move must clear both result and termination.

    Regression manifestation: leaving a stale result makes a resumed game whose
    mate was taken back reappear as finished, blocking further play.
    """
    update_game_result(session, session.game_db_id, "1-0", "Termination.CHECKMATE")

    assert clear_game_result(session, session.game_db_id) is True

    game = _game(session)
    assert game.result is None
    assert game.termination is None


def test_clear_is_idempotent_for_unfinished_game(session):
    """Clearing an already-unfinished game is a harmless no-op that succeeds.

    Takeback fires for any move, not only game-ending ones, so clearing must be
    safe when there is nothing to clear. Regression manifestation: raising or
    returning False here would make ordinary mid-game takebacks log spurious
    errors.
    """
    assert clear_game_result(session, session.game_db_id) is True

    game = _game(session)
    assert game.result is None
    assert game.termination is None


def test_update_missing_game_returns_false(session):
    """Updating a non-existent id must fail cleanly rather than raise.

    Uses an id guaranteed absent. Regression manifestation: an unguarded query
    result would raise AttributeError on None instead of reporting no-op.
    """
    assert update_game_result(session, 9999, "1-0", "Termination.CHECKMATE") is False
