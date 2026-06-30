"""Mirror proxy-reconstructed Centaur game state to Universal Chess's web UI.

During Original Centaur play the game runs entirely inside the Centaur process,
so UC's GameManager/ChessGameService never sees the moves and the web control
page would stay frozen on the last UC game. The proxy is the only UC component
in the loop -- it sits on Centaur's UCI ``position`` stream -- so it mirrors each
reconstructed position to the same two channels UC's own service publishes to:

- ``fen.log`` (read by the ``/video`` board renderer and the Chromecast feed), and
- the game-state broadcast socket (fanned out to the web's SSE ``/events``).

Both side effects are injected so the logic stays pure and unit-testable. Every
publish is best-effort: a web/IO failure must never disturb engine play, so the
whole computation is guarded and a failure is logged, not raised.
"""

from __future__ import annotations

from typing import Callable, Optional

import chess
import chess.pgn


def _board_to_pgn(board: chess.Board) -> str:
    """Render the board's move stack as a PGN string (with a FEN header if the
    game did not start from the standard position)."""
    game = chess.pgn.Game.from_board(board)
    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=False)
    return game.accept(exporter)


class CentaurStatePublisher:
    """Push one reconstructed board to UC's fen.log + game-state broadcast.

    The two side effects are injected (``write_fen_log`` and ``broadcast``) so
    tests can assert the exact payload without a socket or filesystem, and so
    this class carries no UC import coupling of its own.
    """

    def __init__(
        self,
        write_fen_log: Callable[[str], object],
        broadcast: Callable[..., object],
        *,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._write_fen_log = write_fen_log
        self._broadcast = broadcast
        self._log_fn = log_fn

    def publish(self, board: Optional[chess.Board]) -> None:
        """Mirror ``board`` to fen.log and the broadcast socket.

        A no-op when ``board`` is None (no position seen yet). Derives turn,
        move number, last move and result from the board so the web shows the
        same data UC's own ChessGameService would. The full FEN is passed to
        both sinks; the broadcast layer splits it into placement-only for
        chessboard.js itself.
        """
        if board is None:
            return
        try:
            fen_full = board.fen()
            last_move = board.peek().uci() if board.move_stack else None
            outcome = board.outcome(claim_draw=True)
            self._write_fen_log(fen_full)
            self._broadcast(
                fen=fen_full,
                pgn=_board_to_pgn(board),
                turn="w" if board.turn == chess.WHITE else "b",
                move_number=board.fullmove_number,
                last_move=last_move,
                game_over=outcome is not None,
                result=outcome.result() if outcome is not None else None,
                termination=outcome.termination.name.lower() if outcome is not None else None,
            )
        except Exception as exc:  # noqa: BLE001 - web mirroring must never break play
            if self._log_fn:
                self._log_fn(f"centaur-proxy: web state publish error: {exc}")
