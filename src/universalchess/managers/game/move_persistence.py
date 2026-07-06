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
        chess960: True for a Chess960 game. Persisted on the game record (with the
            start FEN, which is ``fen_before_move`` of the first move) so the
            variant and exact generated start can be restored on resume. Standard
            games store False and NULL start_fen.

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
        # start_fen is the position before the first move; store it only for
        # Chess960 so standard games keep NULL (implicitly the standard start) and
        # resume can distinguish "replay from a non-standard start" from the norm.
        game = models.Game(
            source=source_file,
            event=game_info.get("event", ""),
            site=game_info.get("site", ""),
            round=game_info.get("round", ""),
            white=game_info.get("white", ""),
            black=game_info.get("black", ""),
            chess960=chess960,
            start_fen=fen_before_move if chess960 else None,
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


__all__ = ["persist_move_and_maybe_create_game"]


