"""On-demand analysis of a stored game's unanalysed plies.

The browser runs no engine of its own, so a game whose positions were never
evaluated by the board -- an imported PGN, a game played with ``analysis_mode``
off, or one recorded before evaluations were persisted -- has nothing to draw on
the review page's eval chart. This module hands those positions to the board's
existing analysis queue and writes each result back onto its own move row.

Kept separate from :mod:`universalchess.services.analysis`, which is deliberately
unaware of games and databases: it evaluates positions and publishes results by
FEN. The mapping from a result back to a particular stored game belongs here.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Sequence, Tuple

import chess

try:
    from universalchess.board.logging import log
except ImportError:  # pragma: no cover - board logging is absent off-device
    import logging
    log = logging.getLogger(__name__)

from universalchess.services.analysis import PositionAnalysis


# One stored move row as this module consumes it: (move_uci, fen, eval_score).
# The initial-position row has an empty move; a NULL eval_score means the
# position was never analysed.
MoveRow = Tuple[str, str, object]


def plies_needing_analysis(rows: Sequence[MoveRow], start_fen: str,
                           chess960: bool) -> List[chess.Board]:
    """Return a board for each played ply that has no stored evaluation.

    Pure. Replays the game from ``start_fen`` on a board carrying the variant
    flag, so a Chess960 king-onto-rook castle is legal and later plies are
    reached; on a standard board that move is rejected and everything after it
    would be silently skipped.

    A stored ``0`` counts as analysed -- it is a real evaluation meaning the
    position is dead equal. Only NULL means "never analysed".

    A move that cannot be applied (corrupt data in an imported game) ends the
    replay rather than raising, so the plies before it are still usable.
    """
    board = chess.Board(start_fen, chess960=chess960)
    needed: List[chess.Board] = []

    for move_uci, _fen, eval_score in rows:
        if not move_uci:
            continue  # the initial-position row is not a ply
        try:
            move = chess.Move.from_uci(move_uci)
        except ValueError:
            log.warning(f"[GapFill] Stopping replay at unparseable move {move_uci!r}")
            break
        if move not in board.legal_moves:
            log.warning(f"[GapFill] Stopping replay at illegal move {move_uci!r}")
            break
        board.push(move)
        if eval_score is None:
            needed.append(board.copy())

    return needed


class GameGapFiller:
    """Queues a stored game's unanalysed plies and persists their results.

    Dependencies are injected so the coordination logic is testable without an
    engine or a database:

    Args:
        analysis_service: Supplies ``analyze_position``, ``on_position_analysed``
            and ``remove_position_listener``.
        persist: Called as ``persist(game_db_id, PositionAnalysis)`` for each
            result belonging to a requested ply.
    """

    def __init__(self, analysis_service,
                 persist: Callable[[int, PositionAnalysis], None]):
        self._service = analysis_service
        self._persist = persist
        # FEN -> game id, for the plies this filler is still waiting on. Results
        # for anything else (the live game continues analysing throughout) are
        # ignored: matching on FEN alone would write one game's evaluation onto
        # another's row, since opening positions recur across games.
        self._awaiting: Dict[str, int] = {}
        self._listening = False

    def fill(self, game_db_id: int, rows: Sequence[MoveRow], start_fen: str,
             chess960: bool) -> int:
        """Queue every unanalysed ply of a stored game.

        Returns the number of positions queued. Zero means the game is already
        fully analysed, in which case no listener is registered and nothing is
        searched.
        """
        boards = plies_needing_analysis(rows, start_fen, chess960)
        if not boards:
            return 0

        for board in boards:
            self._awaiting[board.fen()] = game_db_id

        if not self._listening:
            self._service.on_position_analysed(self._on_result)
            self._listening = True

        for board in boards:
            self._service.analyze_position(board)

        log.info(f"[GapFill] Queued {len(boards)} unanalysed plies for game {game_db_id}")
        return len(boards)

    def _on_result(self, result: PositionAnalysis) -> None:
        """Persist a result for a ply this filler requested."""
        game_db_id = self._awaiting.pop(result.fen, None)
        if game_db_id is None:
            return

        self._persist(game_db_id, result)

        if not self._awaiting and self._listening:
            self._service.remove_position_listener(self._on_result)
            self._listening = False
