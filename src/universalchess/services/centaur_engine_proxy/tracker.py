"""Reconstruct a game from Centaur's UCI ``position`` stream.

Centaur drives the engine by sending, before each search, the *entire* game so
far: ``position startpos moves e2e4 e7e5 ...`` (or ``position fen <fen> moves
...``). The proxy reconstructs the game from this stream so it can record it in
UC's database, replacing the patched engine's built-in SQLite logging.

Pure logic (parsing + board replay via python-chess); no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import chess

# Standard starting position, used when Centaur sends ``position startpos``.
START_FEN = chess.STARTING_FEN


def parse_position_command(line: str) -> Optional[Tuple[Optional[str], List[str]]]:
    """Parse a UCI ``position`` command into (start_fen, moves).

    Returns ``(None, moves)`` for ``startpos`` (None meaning the standard start)
    and ``(fen, moves)`` for ``position fen <6-field-fen> [moves ...]``. Returns
    None for any non-position line so callers can ignore it.
    """
    tokens = line.split()
    if not tokens or tokens[0].lower() != "position":
        return None
    if len(tokens) < 2:
        return None

    if tokens[1].lower() == "startpos":
        start_fen = None
        rest = tokens[2:]
    elif tokens[1].lower() == "fen":
        # A FEN is exactly six space-separated fields.
        fen_fields = tokens[2:8]
        if len(fen_fields) < 6:
            return None
        start_fen = " ".join(fen_fields)
        rest = tokens[8:]
    else:
        return None

    moves: List[str] = []
    if rest:
        if rest[0].lower() != "moves":
            return None
        moves = rest[1:]
    return start_fen, moves


@dataclass(frozen=True)
class GameUpdate:
    """The change a single ``position`` command represents.

    ``is_new_game`` is True when the command starts a fresh game (different start
    position, or a move list that is not an extension of the prior one).
    ``moves_added`` pairs each newly appended move with the FEN *after* it.
    ``result``/``termination`` are set once the replayed board reaches a natural
    game over (checkmate/stalemate/draw), else None.
    """

    is_new_game: bool
    start_fen: str
    moves_added: List[Tuple[str, str]] = field(default_factory=list)
    total_moves: int = 0
    result: Optional[str] = None
    termination: Optional[str] = None


class PositionTracker:
    """Stateful tracker turning a sequence of ``position`` commands into updates.

    Holds the current game's start FEN, the moves seen so far, and a replayed
    board so FENs for new moves are computed incrementally. A command whose start
    differs, or whose move list is not a prefix-extension of the current one, is
    treated as a new game (Centaur also sends ``ucinewgame``, but relying on the
    move stream alone makes detection robust even if that is missed).
    """

    def __init__(self) -> None:
        self._start_fen: Optional[str] = None
        self._moves: List[str] = []
        self._board: Optional[chess.Board] = None
        self._started = False

    @staticmethod
    def _is_extension(prefix: List[str], full: List[str]) -> bool:
        """Whether ``full`` is ``prefix`` followed by zero or more extra moves."""
        return len(full) >= len(prefix) and full[: len(prefix)] == prefix

    def update(self, start_fen: Optional[str], moves: List[str]) -> GameUpdate:
        """Fold one parsed ``position`` into the tracker and return the delta.

        Raises ValueError if a move in the stream is illegal for the
        reconstructed board -- that indicates the stream and our replay have
        diverged, which the caller should surface rather than silently mis-record.
        """
        normalized_start = start_fen if start_fen is not None else START_FEN
        is_new_game = (
            not self._started
            or normalized_start != self._start_fen
            or not self._is_extension(self._moves, moves)
        )

        if is_new_game:
            self._start_fen = normalized_start
            self._moves = []
            self._board = chess.Board(normalized_start)
            self._started = True

        added: List[Tuple[str, str]] = []
        for uci in moves[len(self._moves):]:
            move = chess.Move.from_uci(uci)
            if move not in self._board.legal_moves:
                raise ValueError(f"Illegal move {uci} for position {self._board.fen()}")
            self._board.push(move)
            self._moves.append(uci)
            added.append((uci, self._board.fen()))

        result: Optional[str] = None
        termination: Optional[str] = None
        outcome = self._board.outcome(claim_draw=True)
        if outcome is not None:
            result = outcome.result()
            termination = outcome.termination.name.lower()

        return GameUpdate(
            is_new_game=is_new_game,
            start_fen=self._start_fen,
            moves_added=added,
            total_moves=len(self._moves),
            result=result,
            termination=termination,
        )
