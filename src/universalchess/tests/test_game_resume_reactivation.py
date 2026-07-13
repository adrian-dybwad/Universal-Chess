"""Tests for reactivate_game_for_resume (web "Resume" DB policy).

The web Games screen can resume a stored game. The policy that decides whether a
game may be resumed -- and that turns an abandoned game back into a live one --
lives in reactivate_game_for_resume so it is testable without importing the
hardware-heavy application entrypoint. These tests guard that policy directly
against an in-memory database:

- abandoned ("*") games are reactivated (result cleared to NULL) and resumable,
- in-progress (NULL) games are already live and untouched,
- finished games are review-only and rejected with their result preserved,
- missing rows / uninitialized ids / no session are rejected as not resumable.
"""

import pytest

pytest.importorskip("sqlalchemy")

# db.models builds an engine from the database URI and opens it at import time;
# point it at an in-memory SQLite so a checkout without /opt can import it.
import universalchess.db.uri as _uri  # noqa: E402

_uri.get_database_uri = lambda: "sqlite:///:memory:"

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from universalchess.db import models  # noqa: E402
from universalchess.managers.game.database import reactivate_game_for_resume  # noqa: E402


@pytest.fixture
def db():
    """Fresh in-memory DB with a session factory and a game-seeding helper."""
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def add_game(result):
        session = Session()
        try:
            game = models.Game(white="W", black="B", result=result, source="test")
            session.add(game)
            session.commit()
            return game.id
        finally:
            session.close()

    return Session, add_game


def test_abandoned_game_is_reactivated_to_null_and_resumable(db):
    # The core feature: an abandoned game ("*") must become live again so play can
    # continue and later moves persist to the same record. Regression: if the
    # result were left as "*", resume would treat it as a finished/abandoned game
    # (reproducing a game-over state) and the Games screen would still show it as
    # abandoned. Verified via the committed value read in a fresh session.
    Session, add_game = db
    game_id = add_game("*")

    session = Session()
    try:
        assert reactivate_game_for_resume(session, game_id) is True
    finally:
        session.close()

    verify = Session()
    try:
        assert verify.get(models.Game, game_id).result is None
    finally:
        verify.close()


def test_in_progress_game_is_resumable_and_left_null(db):
    # A NULL-result game is already live; reactivation must accept it without
    # changing anything. Regression: an over-eager mutation could stamp a result
    # onto a live game, and returning False would wrongly block resuming it.
    Session, add_game = db
    game_id = add_game(None)

    session = Session()
    try:
        assert reactivate_game_for_resume(session, game_id) is True
    finally:
        session.close()

    verify = Session()
    try:
        assert verify.get(models.Game, game_id).result is None
    finally:
        verify.close()


@pytest.mark.parametrize("finished_result", ["1-0", "0-1", "1/2-1/2"])
def test_finished_game_is_rejected_and_result_preserved(db, finished_result):
    # A finished game is review-only: it must be rejected (False) and its recorded
    # outcome must survive untouched. Regression: clearing a finished game's result
    # would silently erase the game's outcome and let it be resumed as if live.
    Session, add_game = db
    game_id = add_game(finished_result)

    session = Session()
    try:
        assert reactivate_game_for_resume(session, game_id) is False
    finally:
        session.close()

    verify = Session()
    try:
        assert verify.get(models.Game, game_id).result == finished_result
    finally:
        verify.close()


def test_missing_game_is_not_resumable(db):
    # A resume request for a deleted/unknown id must report not-resumable rather
    # than error. Regression: an unguarded access on a missing row would raise
    # instead of returning False, turning a stale UI id into a crash.
    Session, _add_game = db
    session = Session()
    try:
        assert reactivate_game_for_resume(session, 999999) is False
    finally:
        session.close()


@pytest.mark.parametrize(
    "session_value,game_id",
    [
        (None, 1),   # no session (persistence disabled)
        ("SESSION", -1),  # uninitialized/negative id
    ],
)
def test_no_session_or_invalid_id_is_not_resumable(db, session_value, game_id):
    # Guard the two "uninitialized" inputs that mirror update_game_result /
    # clear_game_result: a None session (DB disabled) and a negative id (no game
    # created yet) must both be no-ops returning False, never a query/crash.
    Session, _add_game = db
    session = Session() if session_value == "SESSION" else None
    try:
        assert reactivate_game_for_resume(session, game_id) is False
    finally:
        if session is not None:
            session.close()
