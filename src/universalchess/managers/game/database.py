"""Game database session and persistence helpers.

GameManager keeps all SQLAlchemy operations on the game thread to avoid
cross-thread connection pool issues. This module isolates:
- engine/session lifecycle creation in the game thread
- safe close/dispose
- small helper operations for move/result persistence
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional, Tuple

from universalchess.board.logging import log
from universalchess.db.uri import get_database_uri

from .deferred_imports import _get_create_engine, _get_models, _get_sessionmaker


@dataclass
class GameDatabaseContext:
    """Holds SQLAlchemy engine + session for the current game thread."""

    engine: object
    session: object
    thread_id: int


def create_game_db_context_if_enabled(save_to_database: bool) -> Optional[GameDatabaseContext]:
    """Create DB engine+session in the current thread if enabled.

    This must be called from the game thread so the connection pool is created
    in the same thread that uses it.
    """
    if not save_to_database:
        return None

    database_uri = get_database_uri()
    create_engine = _get_create_engine()
    sessionmaker = _get_sessionmaker()

    if create_engine is None or sessionmaker is None:
        log.error("[GameDB] Deferred SQLAlchemy imports unavailable; database disabled for this session")
        return None

    # Configure SQLite with check_same_thread=False. Safe because we create and use the
    # engine entirely within the game thread.
    if database_uri.startswith("sqlite"):
        engine = create_engine(
            database_uri,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
    else:
        engine = create_engine(database_uri, pool_pre_ping=True)

    Session = sessionmaker(bind=engine)
    session = Session()
    thread_id = threading.get_ident()
    log.info(f"[GameDB] Engine+session created in thread {thread_id}")
    return GameDatabaseContext(engine=engine, session=session, thread_id=thread_id)


def close_game_db_context(ctx: Optional[GameDatabaseContext]) -> None:
    """Close session and dispose engine if present."""
    if ctx is None:
        return

    if ctx.session is not None:
        try:
            log.info(f"[GameDB] Closing database session in thread {ctx.thread_id}")
            ctx.session.close()
        except Exception as e:
            log.error(f"[GameDB] Error closing database session: {e}")

    if ctx.engine is not None:
        try:
            log.info(f"[GameDB] Disposing database engine in thread {ctx.thread_id}")
            ctx.engine.dispose()
        except Exception as e:
            log.error(f"[GameDB] Error disposing database engine: {e}")


def update_game_result(
    session, game_db_id: int, result_string: str, termination: Optional[str] = None
) -> bool:
    """Persist a game's result and how it ended, if the record exists.

    The termination reason is stored alongside the result so a *finished* game
    can be resumed with its exact game-over state after a restart. Manual
    endings (resignation, draw agreement, time forfeit) are not derivable from
    the final board position, so without the stored reason a resumed resigned
    game could not reproduce its game-over screen.

    Args:
        session: Active SQLAlchemy session, or None (no-op).
        game_db_id: Game row id; negative ids are treated as uninitialized.
        result_string: Result token ('1-0', '0-1', '1/2-1/2').
        termination: How the game ended (e.g. 'Termination.RESIGN'). When None
            the existing termination is left unchanged so callers that only know
            the result do not erase a previously stored reason.

    Returns:
        True if the record was found and updated, False otherwise.
    """
    if session is None or game_db_id < 0:
        return False

    models = _get_models()
    if models is None:
        return False

    game_record = session.query(models.Game).filter(models.Game.id == game_db_id).first()
    if game_record is None:
        return False

    game_record.result = result_string
    if termination is not None:
        game_record.termination = termination
    session.flush()
    session.commit()
    return True


def clear_game_result(session, game_db_id: int) -> bool:
    """Clear a game's result and termination (e.g. after a takeback).

    A takeback removes the game-ending move, so the game is no longer over. The
    result/termination must be cleared to keep the stored game state truthful --
    otherwise a resumed game whose deciding move was taken back would wrongly
    reappear as finished. Idempotent for a game that has no result.

    Args:
        session: Active SQLAlchemy session, or None (no-op).
        game_db_id: Game row id; negative ids are treated as uninitialized.

    Returns:
        True if the record was found and cleared, False otherwise.
    """
    if session is None or game_db_id < 0:
        return False

    models = _get_models()
    if models is None:
        return False

    game_record = session.query(models.Game).filter(models.Game.id == game_db_id).first()
    if game_record is None:
        return False

    game_record.result = None
    game_record.termination = None
    session.flush()
    session.commit()
    return True


def reactivate_game_for_resume(session, game_db_id: int) -> bool:
    """Reactivate a stored game so it can be resumed for continued play.

    Backs the web "Resume" action, which continues one specific game. An
    abandoned game ("*") is turned back into a genuine in-progress game by
    clearing its result to NULL (committed) so later moves persist to the same
    record and the Games screen no longer lists it as abandoned. An in-progress
    game (NULL result) is already live and left untouched. A finished game
    ("1-0"/"0-1"/"1/2-1/2") is review-only and must not be reactivated: its
    result is preserved and this reports it as not resumable, so a resumed game's
    outcome is never silently discarded.

    Args:
        session: Active SQLAlchemy session, or None (no-op -> False).
        game_db_id: Game row id; negative ids are treated as uninitialized.

    Returns:
        True when the game is resumable for continued play (any "*" result has
        been cleared to NULL), False when the game is missing or finished.
    """
    if session is None or game_db_id < 0:
        return False

    models = _get_models()
    if models is None:
        return False

    game_record = session.query(models.Game).filter(models.Game.id == game_db_id).first()
    if game_record is None:
        return False

    if game_record.result in ("1-0", "0-1", "1/2-1/2"):
        return False

    if game_record.result == "*":
        game_record.result = None
        session.flush()
        session.commit()

    return True


def delete_last_move(session) -> bool:
    """Delete the last move (globally) from GameMove table."""
    if session is None:
        return False

    models = _get_models()
    if models is None:
        return False

    db_last_move = session.query(models.GameMove).order_by(models.GameMove.id.desc()).first()
    if db_last_move is None:
        return False

    session.delete(db_last_move)
    session.commit()
    return True


__all__ = [
    "GameDatabaseContext",
    "create_game_db_context_if_enabled",
    "close_game_db_context",
    "update_game_result",
    "clear_game_result",
    "reactivate_game_for_resume",
    "delete_last_move",
]


