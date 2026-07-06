"""Read/write helpers for a per-move AI coach statement.

A coach statement is stored on the ``GameMove`` row for a played ply so the AI
service is queried at most once per move: the first time a user reviews a move
the statement is fetched and saved here, and every later review reads it back.

Ply addressing
--------------
Moves for a game are ``GameMove`` rows ordered by ``id``. The first row is the
initial position (``move == ""``); the actual plies follow. ``ply_index`` is
1-based over the played moves (ply 1 = the first move), so it maps to the
``ply_index``-th row whose ``move`` is non-empty. The initial-position row is
skipped, which keeps the mapping correct for games started from a custom FEN.

Sessions
--------
These helpers may run on a coach worker thread, separate from the game thread
that owns ``GameManager``'s SQLAlchemy session. To avoid cross-thread use of a
single session they open their own short-lived session (SQLite is opened with
``check_same_thread=False``) unless a caller injects one (used by tests). The
injected-session path lets tests exercise the mapping against an in-memory DB
without touching the on-disk database.
"""

from __future__ import annotations

from typing import Optional

from universalchess.db.uri import get_database_uri


def _get_models():
    """Import the DB models module.

    Imported lazily (not at module load) so importing this helper module never
    triggers the models module's import-time engine creation; by runtime the
    models module is already loaded by the app.
    """
    from universalchess.db import models
    return models


def _open_session():
    """Open a new engine+session for a coach read/write.

    Uses a dedicated engine (not the shared ``models.engine``) because these
    helpers may run on a coach worker thread. Returns ``(engine, session)``; the
    caller must close the session and dispose the engine.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    database_uri = get_database_uri()
    if database_uri.startswith("sqlite"):
        engine = create_engine(
            database_uri,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
    else:
        engine = create_engine(database_uri, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    return engine, Session()


def _played_move_rows(session, models, game_db_id: int):
    """Return the game's ``GameMove`` rows for played moves, ordered by id.

    Skips the initial-position row (empty ``move``) so callers can index by a
    1-based ply. Shared by the statement and eval lookups so their ply->row
    mapping cannot drift.
    """
    rows = (
        session.query(models.GameMove)
        .filter(models.GameMove.gameid == game_db_id)
        .order_by(models.GameMove.id)
        .all()
    )
    return [row for row in rows if row.move]


def _move_row_for_ply(session, models, game_db_id: int, ply_index: int):
    """Return the ``GameMove`` row for a 1-based ply, or None if out of range."""
    move_rows = _played_move_rows(session, models, game_db_id)
    if ply_index < 1 or ply_index > len(move_rows):
        return None
    return move_rows[ply_index - 1]


def get_coach_statement(
    game_db_id: int, ply_index: int, *, session=None
) -> Optional[str]:
    """Return the stored coach statement for a ply, or None if absent.

    None is returned for an unsaved statement, an out-of-range ply, an
    uninitialized game (``game_db_id < 0``), or when the DB layer is unavailable
    -- the caller treats all of these the same: nothing stored yet.
    """
    if game_db_id is None or game_db_id < 0 or ply_index is None or ply_index < 1:
        return None

    models = _get_models()
    if models is None:
        return None

    own_session = session is None
    engine = None
    if own_session:
        engine, session = _open_session()
        if session is None:
            return None
    try:
        row = _move_row_for_ply(session, models, game_db_id, ply_index)
        return row.coach_statement if row is not None else None
    finally:
        if own_session:
            session.close()
            if engine is not None:
                engine.dispose()


def save_coach_statement(
    game_db_id: int, ply_index: int, statement: str, *, session=None
) -> bool:
    """Persist a coach statement onto a ply's ``GameMove`` row.

    Returns True when the row was found and updated, False otherwise (unknown
    game/ply or unavailable DB layer). Overwrites any existing statement so a
    caller that deliberately refetches can replace stale text.
    """
    if game_db_id is None or game_db_id < 0 or ply_index is None or ply_index < 1:
        return False

    models = _get_models()
    if models is None:
        return False

    own_session = session is None
    engine = None
    if own_session:
        engine, session = _open_session()
        if session is None:
            return False
    try:
        row = _move_row_for_ply(session, models, game_db_id, ply_index)
        if row is None:
            return False
        row.coach_statement = statement
        session.commit()
        return True
    finally:
        if own_session:
            session.close()
            if engine is not None:
                engine.dispose()


def save_coach_statement_if_absent(
    game_db_id: int, ply_index: int, statement: str, *, session=None
) -> Optional[str]:
    """Persist ``statement`` for a ply only if none is stored yet; return canonical.

    First-writer-wins: the board and the web each read-through the database and
    generate on a miss, so the same never-before-seen move can be generated on both
    surfaces at once. Because the model is non-deterministic, two generations differ.
    Overwriting would then leave the database holding the last writer while each
    surface's in-memory cache showed its own text -- the same move coached
    differently on board vs web.

    This writes atomically only when the row's ``coach_statement`` is empty
    (``UPDATE ... WHERE coach_statement IS NULL OR = ''``) and then re-reads the row,
    so every caller converges on whichever statement committed first. The losing
    caller discards its own generation and adopts the winner's, guaranteeing board
    and web display identical text for a move.

    Returns the canonical stored statement (the winner's), or None for an unknown
    game/ply, an uninitialized game (``game_db_id < 0``), or an unavailable DB layer
    -- the caller then falls back to showing its own generated text.
    """
    if game_db_id is None or game_db_id < 0 or ply_index is None or ply_index < 1:
        return None

    models = _get_models()
    if models is None:
        return None

    from sqlalchemy import or_

    own_session = session is None
    engine = None
    if own_session:
        engine, session = _open_session()
        if session is None:
            return None
    try:
        row = _move_row_for_ply(session, models, game_db_id, ply_index)
        if row is None:
            return None
        row_id = row.id
        # Claim the row only if still unset. synchronize_session=False keeps this a
        # single atomic UPDATE; the ORM identity map is refreshed by the re-read.
        session.query(models.GameMove).filter(
            models.GameMove.id == row_id,
            or_(
                models.GameMove.coach_statement.is_(None),
                models.GameMove.coach_statement == "",
            ),
        ).update(
            {models.GameMove.coach_statement: statement},
            synchronize_session=False,
        )
        session.commit()
        canonical = (
            session.query(models.GameMove.coach_statement)
            .filter(models.GameMove.id == row_id)
            .scalar()
        )
        return canonical
    finally:
        if own_session:
            session.close()
            if engine is not None:
                engine.dispose()


def get_move_context(
    game_db_id: int, ply_index: int, *, session=None
) -> Optional[tuple[str, str]]:
    """Return ``(fen_before, move_uci)`` for a 1-based ply, or None if unavailable.

    ``fen_before`` is the FEN of the position the mover faced: the previous played
    ply's stored ``fen``, or the initial-position row's ``fen`` for ply 1 (which
    also handles games started from a custom FEN). ``move_uci`` is the ply's
    stored UCI move.

    Returns None for an out-of-range ply, a missing initial-position row, an
    uninitialized game (``game_db_id < 0``), or an unavailable DB layer -- the
    caller then produces no coach statement rather than fabricating a position.

    Used by the web coach endpoint to reconstruct a move's coaching prompt purely
    from stored rows, without replaying the game, so the live board and analysis
    views can resolve any ply's statement server-side.
    """
    if game_db_id is None or game_db_id < 0 or ply_index is None or ply_index < 1:
        return None

    models = _get_models()
    if models is None:
        return None

    own_session = session is None
    engine = None
    if own_session:
        engine, session = _open_session()
        if session is None:
            return None
    try:
        move_rows = _played_move_rows(session, models, game_db_id)
        if ply_index < 1 or ply_index > len(move_rows):
            return None
        move_uci = move_rows[ply_index - 1].move
        if ply_index > 1:
            fen_before = move_rows[ply_index - 2].fen
        else:
            initial_row = (
                session.query(models.GameMove)
                .filter(
                    models.GameMove.gameid == game_db_id,
                    models.GameMove.move == "",
                )
                .order_by(models.GameMove.id)
                .first()
            )
            if initial_row is None:
                return None
            fen_before = initial_row.fen
        if not fen_before or not move_uci:
            return None
        return (fen_before, move_uci)
    finally:
        if own_session:
            session.close()
            if engine is not None:
                engine.dispose()


def get_game_chess960(game_db_id: int, *, session=None) -> bool:
    """Return whether a stored game is Chess960 (Fischer Random).

    Read from the ``Game.chess960`` column so the web coach endpoint can build the
    move's board 960-aware (960 castling is a king-onto-rook move that is illegal
    on a standard board, which would otherwise blank the move text and drop the
    "Castles" fact for every reviewed 960 castle).

    Returns False for an uninitialized game (``game_db_id < 0``), a missing row, a
    NULL column (games created before the column existed), or an unavailable DB
    layer -- the safe default that leaves standard games untouched.
    """
    if game_db_id is None or game_db_id < 0:
        return False

    models = _get_models()
    if models is None:
        return False

    own_session = session is None
    engine = None
    if own_session:
        engine, session = _open_session()
        if session is None:
            return False
    try:
        row = (
            session.query(models.Game.chess960)
            .filter(models.Game.id == game_db_id)
            .first()
        )
        return bool(row[0]) if row is not None else False
    finally:
        if own_session:
            session.close()
            if engine is not None:
                engine.dispose()


def get_move_evals(
    game_db_id: int, ply_index: int, *, session=None
) -> tuple[Optional[int], Optional[int]]:
    """Return ``(eval_before_cp, eval_after_cp)`` for a 1-based ply.

    Both values are engine evaluations in centipawns from White's perspective
    (matching ``GameMove.eval_score``):

    - ``eval_after_cp`` is the eval of the position *after* the ply's move -- the
      ply's own ``eval_score``.
    - ``eval_before_cp`` is the eval of the position the mover faced, i.e. the
      *previous* played ply's ``eval_score``. Ply 1 has no stored predecessor
      (the initial-position row carries no analysis score), so its before-eval is
      None.

    Either element is None when that eval was never analysed. ``(None, None)`` is
    returned for an out-of-range ply, an uninitialized game (``game_db_id < 0``),
    or an unavailable DB layer -- the coach prompt simply omits eval context then.

    Runs off the display thread (on the coach worker) so the game-review keypress
    that selects a ply never blocks on this database read.
    """
    if game_db_id is None or game_db_id < 0 or ply_index is None or ply_index < 1:
        return (None, None)

    models = _get_models()
    if models is None:
        return (None, None)

    own_session = session is None
    engine = None
    if own_session:
        engine, session = _open_session()
        if session is None:
            return (None, None)
    try:
        move_rows = _played_move_rows(session, models, game_db_id)
        if ply_index < 1 or ply_index > len(move_rows):
            return (None, None)
        eval_after = move_rows[ply_index - 1].eval_score
        has_previous_ply = ply_index > 1
        eval_before = move_rows[ply_index - 2].eval_score if has_previous_ply else None
        return (eval_before, eval_after)
    finally:
        if own_session:
            session.close()
            if engine is not None:
                engine.dispose()


__all__ = [
    "get_coach_statement",
    "get_move_context",
    "get_move_evals",
    "save_coach_statement",
    "save_coach_statement_if_absent",
]
