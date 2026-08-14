"""Move persistence helpers for GameManager.

This module contains the "create game on first move + insert initial position + insert move"
transactional sequence that previously lived inline in GameManager's async post-move tasks.

Keeping this logic here:
- reduces GameManager size and nesting
- centralizes commit/rollback behavior
- preserves the thread-local SQLAlchemy session contract (caller owns the session)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

import chess

from universalchess.board.logging import log

from .deferred_imports import _get_models

if TYPE_CHECKING:
    from universalchess.services.analysis import PositionAnalysis


def update_move_analysis(session, *, game_db_id: int,
                         result: "PositionAnalysis") -> bool:
    """Write a completed analysis onto the move row for the position it analysed.

    The move row is inserted before its position has been evaluated (the search
    runs on a separate thread and takes at least the analysis time limit), so
    the evaluation is backfilled here when it finishes. Matching on the FEN --
    not on "the most recent row" -- is what keeps the value on the ply it
    actually describes; reading the live analysis state at insert time is what
    previously attributed every eval to the preceding move.

    The FEN is matched within ``game_db_id`` only. Opening positions and short
    transpositions recur across games, so an unscoped match would overwrite an
    unrelated game's evaluation.

    Args:
        session: SQLAlchemy session (must be used on the owning thread).
        game_db_id: Game whose rows may be updated.
        result: The completed analysis, carrying the FEN it applies to.

    Returns:
        True when a row was updated, False when the position has no row in this
        game. False is an ordinary outcome, not an error: analysis legitimately
        completes for positions that were taken back, or that belong to a review
        gap-fill for a different game.
    """
    if session is None or game_db_id is None or game_db_id < 0:
        return False

    models = _get_models()
    if models is None:
        return False

    row = (
        session.query(models.GameMove)
        .filter_by(gameid=game_db_id, fen=result.fen)
        .order_by(models.GameMove.id.desc())
        .first()
    )
    if row is None:
        return False

    row.eval_score = result.eval_score_cp
    row.best_move = result.best_move
    session.commit()
    return True


def persist_move_and_maybe_create_game(
    *,
    session,
    is_first_move: bool,
    current_game_db_id: int,
    source_file: str,
    game_info: Dict[str, str],
    fen_before_move: str,
    move_uci: str,
    fen_after_move: str,
    white_clock: Optional[int],
    black_clock: Optional[int],
    move_duration_ms: Optional[int] = None,
    time_control: Optional[str] = None,
    analysis: Optional["PositionAnalysis"] = None,
    chess960: bool = False,
) -> Tuple[int, bool]:
    """Persist a move, creating the game record if needed.

    Args:
        session: SQLAlchemy session (must be used on the owning thread)
        is_first_move: Whether this move is the first in the game
        current_game_db_id: Current game id (may be <0)
        source_file: Source file path stored with the game record
        game_info: Dict containing 'event', 'site', 'round', 'white', 'black'
        fen_before_move: FEN recorded for the initial position row
        move_uci: UCI move string to persist
        fen_after_move: FEN recorded for the move row
        white_clock: White clock seconds (or None)
        black_clock: Black clock seconds (or None)
        move_duration_ms: Milliseconds this move took, measured from the previous
            move's confirmation on a monotonic clock. None means the move was not
            timed -- a game resumed from the database or built by "play from
            here" has no measured start -- and is stored as NULL rather than a 0,
            which would be indistinguishable from a genuinely instant move.
        time_control: The game's control in PGN TimeControl format, stored on the
            game record when it is created. None for an unknown control.
        analysis: Analysis of the position after this move, when one has already
            completed. Usually None -- the search normally finishes after the
            row is written and backfills it via :func:`update_move_analysis` --
            but the two run on different threads and either can win, so a result
            that is already available is written with the insert. Leaving the
            columns NULL is the correct representation of "not analysed"; a 0
            there is a real evaluation meaning the position is dead equal.
        chess960: True for a Chess960 game. Persisted on the game record so the
            variant can be restored on resume. The start FEN
            (``fen_before_move`` of the first move) is persisted whenever the
            start is non-standard -- for a Chess960 game OR a game set up from a
            mid-game position ("Play Game from here") -- so resume replays from
            that exact start instead of the standard opening. Only a game that
            begins from the standard opening stores a NULL start_fen.

    Returns:
        Tuple of (new_game_db_id, committed) where:
        - new_game_db_id: updated game id (unchanged if already created)
        - committed: True if a DB commit occurred for the move insert
    """
    if session is None:
        return current_game_db_id, False

    models = _get_models()
    if models is None:
        return current_game_db_id, False

    game_db_id = current_game_db_id

    # Create new game if first move
    if is_first_move:
        # start_fen is the position before the first move. Persist it whenever the
        # start is non-standard -- Chess960 OR a game set up from a mid-game
        # position -- so resume replays from that exact start. A game that begins
        # from the standard opening keeps NULL (implicitly the standard start), so
        # the common case stays unchanged and resume can tell the two apart.
        non_standard_start = chess960 or fen_before_move != chess.STARTING_FEN
        game = models.Game(
            source=source_file,
            event=game_info.get("event", ""),
            site=game_info.get("site", ""),
            round=game_info.get("round", ""),
            white=game_info.get("white", ""),
            black=game_info.get("black", ""),
            chess960=chess960,
            start_fen=fen_before_move if non_standard_start else None,
            time_control=time_control,
        )
        session.add(game)
        session.flush()

        if hasattr(game, "id") and game.id is not None:
            game_db_id = game.id
            log.info(f"[GameManager.async] New game created (id={game_db_id})")

            # Initial position record (no clock times for initial position)
            initial_move = models.GameMove(
                gameid=game_db_id,
                move="",
                fen=fen_before_move,
            )
            session.add(initial_move)

    # Add this move
    if game_db_id >= 0:
        game_move = models.GameMove(
            gameid=game_db_id,
            move=move_uci,
            fen=fen_after_move,
            white_clock=white_clock,
            black_clock=black_clock,
            move_duration_ms=move_duration_ms,
            eval_score=analysis.eval_score_cp if analysis is not None else None,
            best_move=analysis.best_move if analysis is not None else None,
        )
        session.add(game_move)
        session.commit()
        log.debug(f"[GameManager.async] Move {move_uci} committed to database")
        return game_db_id, True

    return game_db_id, False


def create_game_from_moves(
    session,
    *,
    start_fen: str,
    moves_uci: list,
    game_info: Dict[str, str],
    chess960: bool = False,
    source_file: str = "web-play-from-here",
    analysis_for_fen: Optional[Callable[[str], Optional["PositionAnalysis"]]] = None,
) -> Optional[int]:
    """Persist a new in-progress game whose history is a given move sequence.

    Used by "Play Game from here" on the web review page and by "New game from
    this position" on the board: the reviewed game's moves up to the viewed ply
    are transferred into a fresh recorded game so the live board continues from
    that point with the full history/PGN intact, rather than starting cold from
    a bare FEN. The created game has no result (in progress), so the normal
    resume path (:func:`main._resume_game`) will replay these moves and hand
    control to the current player to continue.

    The moves are validated in full on a variant-aware board built from
    ``start_fen`` *before* anything is written, and the per-ply authoritative FENs
    are recomputed here (python-chess) rather than trusted from the client. This
    is deliberately atomic: ``persist_move_and_maybe_create_game`` commits per
    move, so persisting during validation would leave a committed partial game if
    a later move were illegal -- and a game whose stored moves don't replay fails
    to resume. Validating first means an illegal/malformed move aborts with no row
    written and a tampered request cannot store a FEN that disagrees with a move.

    Args:
        session: SQLAlchemy session (owned by the caller; committed per move).
        start_fen: FEN the sequence starts from (standard opening or otherwise).
        moves_uci: UCI move strings in order. Must be non-empty; an empty list
            has no first move to create the game from.
        game_info: Passed through to the game record (``white``/``black`` etc.).
        chess960: True to build the board (and persist the record) as Chess960,
            so king-onto-rook castling UCIs replay correctly.
        source_file: Stored on the game record's ``source`` column.
        analysis_for_fen: Optional lookup of a completed analysis by FEN. Resume
            restores the eval graph from ``GameMove.eval_score`` after resetting
            the live analysis cache, so a fork that omits this leaves the new
            game's graph empty. Unanalysed plies stay NULL (not a fabricated 0).

    Returns:
        The new game's database id, or ``None`` if ``start_fen`` is invalid,
        ``moves_uci`` is empty, or any move is illegal from the running position.
    """
    if not moves_uci:
        return None

    try:
        board = chess.Board(start_fen, chess960=chess960)
    except ValueError as exc:
        log.error(f"[PlayFromHistory] Invalid start FEN {start_fen!r}: {exc}")
        return None

    # First pass: validate every move and capture the (before, uci, after) chain.
    # Nothing is persisted until the whole sequence is known-legal, so a bad move
    # cannot leave a committed partial game behind (persistence commits per move).
    chain = []
    for index, move_uci in enumerate(moves_uci):
        try:
            move = chess.Move.from_uci(move_uci)
        except ValueError:
            log.error(f"[PlayFromHistory] Malformed move UCI {move_uci!r}; aborting")
            return None
        if move not in board.legal_moves:
            log.error(
                f"[PlayFromHistory] Illegal move {move_uci!r} at ply {index}; aborting"
            )
            return None
        fen_before = board.fen()
        board.push(move)
        chain.append((fen_before, move_uci, board.fen()))

    # Second pass: persist the validated sequence as one game.
    game_db_id = -1
    for index, (fen_before, move_uci, fen_after) in enumerate(chain):
        game_db_id, committed = persist_move_and_maybe_create_game(
            session=session,
            is_first_move=(index == 0),
            current_game_db_id=game_db_id,
            source_file=source_file,
            game_info=game_info,
            fen_before_move=fen_before,
            move_uci=move_uci,
            fen_after_move=fen_after,
            white_clock=None,
            black_clock=None,
            analysis=(
                analysis_for_fen(fen_after) if analysis_for_fen is not None else None
            ),
            chess960=chess960,
        )
        if not committed:
            log.error(
                f"[PlayFromHistory] Move {move_uci!r} failed to persist; aborting"
            )
            return None

    if analysis_for_fen is not None:
        start_analysis = analysis_for_fen(start_fen)
        if start_analysis is not None:
            update_move_analysis(
                session, game_db_id=game_db_id, result=start_analysis
            )

    return game_db_id if game_db_id > 0 else None


__all__ = ["persist_move_and_maybe_create_game", "create_game_from_moves"]


