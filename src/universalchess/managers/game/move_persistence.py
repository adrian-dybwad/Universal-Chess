"""Move persistence helpers for GameManager.

This module contains the "create game on first move + insert initial position + insert move"
transactional sequence that previously lived inline in GameManager's async post-move tasks.

Keeping this logic here:
- reduces GameManager size and nesting
- centralizes commit/rollback behavior
- preserves the thread-local SQLAlchemy session contract (caller owns the session)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import chess

from universalchess.board.logging import log

from .deferred_imports import _get_models


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
    eval_score: Optional[int],
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
        eval_score: Eval score in centipawns (or None)
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
            eval_score=eval_score,
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
) -> Optional[int]:
    """Persist a new in-progress game whose history is a given move sequence.

    Used by "Play Game from here" on the web review page: the reviewed game's
    moves up to the viewed ply are transferred into a fresh recorded game so the
    live board continues from that point with the full history/PGN intact, rather
    than starting cold from a bare FEN. The created game has no result (in
    progress), so the normal resume path (:func:`main._resume_game`) will replay
    these moves and hand control to the current player to continue.

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
            eval_score=None,
            chess960=chess960,
        )
        if not committed:
            log.error(
                f"[PlayFromHistory] Move {move_uci!r} failed to persist; aborting"
            )
            return None

    return game_db_id if game_db_id > 0 else None


__all__ = ["persist_move_and_maybe_create_game", "create_game_from_moves"]


