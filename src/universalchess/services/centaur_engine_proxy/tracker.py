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

    ``is_new_game`` is True when the command starts a fresh game (an explicit
    ``ucinewgame``, the first command, or a position that does not rejoin the
    current game's mainline).
    ``moves_added`` pairs each newly appended move with the FEN *after* it.
    ``moves_removed`` is the number of trailing moves the command took back (a
    takeback shortens the line); 0 for a pure append or no-op. A single command
    can both remove and add (take back, then continue down a different line).
    ``total_moves`` is the move count after applying this update.
    ``result``/``termination`` are set once the replayed board reaches a natural
    game over (checkmate/stalemate/draw), else None.
    """

    is_new_game: bool
    start_fen: str
    moves_added: List[Tuple[str, str]] = field(default_factory=list)
    moves_removed: int = 0
    total_moves: int = 0
    result: Optional[str] = None
    termination: Optional[str] = None


class PositionTracker:
    """Stateful tracker turning a sequence of ``position`` commands into updates.

    Holds the game's start FEN, the move history from that start, and a replayed
    board so FENs for new moves are computed incrementally.

    Centaur does not use a single stable form. Within one game it mixes (all
    observed on real hardware):

    - ``position startpos moves <full history>`` -- the whole game from the start;
    - ``position fen <board> moves <delta>`` -- a rolling form whose origin FEN is
      some earlier board and whose move list is the tail from there. The origin
      may stay fixed across turns while the tail grows, e.g. ``fen <after e4 c5>
      moves c2c4`` then ``fen <after e4 c5> moves c2c4 b8c6``.
    - takebacks: a command can restate a position *behind* the current tip
      (``fen <after e4 e6> moves c2c4`` after the line had reached ...d5), which
      means the trailing moves were taken back and the line must shorten.

    So neither a changed ``start_fen`` nor a shorter move list implies a new game.
    The tracker holds the game's mainline (start FEN, the move history, and the
    position reached after each move) and reconciles every command against it:
    find where the command's origin FEN rejoins the mainline, keep the moves that
    still match from there, then *truncate* the moves past the divergence point
    (takeback) and *append* the command's new moves (extension). A command whose
    origin is not on the mainline -- or an explicit ``ucinewgame`` -- starts a
    fresh game.

    Reconciling on position (not on ``start_fen`` strings) is what keeps the
    recorded game and the live web view whole through the rolling form, and what
    makes takebacks shorten the line instead of being ignored or fragmenting the
    game.
    """

    def __init__(self) -> None:
        self._game_start_fen: Optional[str] = None
        self._history: List[str] = []
        self._board: Optional[chess.Board] = None
        # Position identity (see _key) after each mainline ply: index 0 is the
        # start position, index i the position after history[:i]. Lets a command's
        # origin be located on the mainline without re-replaying it every time.
        self._mainline_keys: List[str] = []
        self._started = False
        self._new_game_pending = False

    @property
    def board(self) -> Optional[chess.Board]:
        """The current reconstructed board, or None before the first position.

        Read-only by convention. Exposed so a consumer (the web-state publisher)
        can derive FEN/PGN/turn from the same replay the tracker already
        maintains, rather than re-parsing the move stream. The board's move stack
        spans the whole game (from ``_game_start_fen``), so PGN export is correct
        even for the rolling ``fen <board> moves <delta>`` form.
        """
        return self._board

    def mark_new_game(self) -> None:
        """Force the next ``update`` to start a fresh game.

        Called on Centaur's ``ucinewgame``. This is the only unambiguous new-game
        signal: a bare ``position startpos`` could equally be a takeback to the
        start of the current game, so the explicit marker is what distinguishes
        "new game" from "rewind to the opening".
        """
        self._new_game_pending = True

    @staticmethod
    def _key(board: chess.Board) -> str:
        """Position identity ignoring move clocks (placement+turn+castling+ep).

        ``epd()`` omits the half/full-move counters and normalizes the en-passant
        square to the legal form, so two spellings of the same position (e.g. a
        ``fen`` Centaur sends vs. the board reached by replaying moves) compare
        equal even when their clocks or ep notation differ.
        """
        return board.epd()

    def update(self, start_fen: Optional[str], moves: List[str]) -> GameUpdate:
        """Fold one parsed ``position`` into the tracker and return the delta.

        Raises ValueError if a move in the stream is illegal for the position it
        is applied to -- that indicates the stream and our replay have diverged,
        which the caller should surface rather than silently mis-record.
        """
        normalized_start = start_fen if start_fen is not None else START_FEN

        # Validate the command's moves against their own origin, collecting the
        # move objects (the resolved board is rebuilt during reconcile).
        origin = chess.Board(normalized_start)
        origin_key = self._key(origin)
        board = chess.Board(normalized_start)
        move_objs: List[chess.Move] = []
        for uci in moves:
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                raise ValueError(f"Illegal move {uci} for position {board.fen()}")
            board.push(move)
            move_objs.append(move)

        if self._new_game_pending or not self._started:
            return self._start_new_game(normalized_start, move_objs)

        # Locate where the command's origin rejoins our mainline. The largest
        # match minimizes how much of the line is disturbed (handles repeated
        # positions). If the origin is not on the mainline, the command describes
        # an unrelated line -> a new game.
        rejoin = None
        for j in range(len(self._mainline_keys) - 1, -1, -1):
            if self._mainline_keys[j] == origin_key:
                rejoin = j
                break
        if rejoin is None:
            return self._start_new_game(normalized_start, move_objs)

        return self._reconcile(rejoin, move_objs)

    def _reconcile(self, rejoin: int, move_objs: List[chess.Move]) -> GameUpdate:
        """Merge a command rejoining the mainline at ply ``rejoin`` into the game.

        Keeps the moves that still match from ``rejoin`` (so an unchanged prefix
        is not churned), truncates any trailing moves the command no longer
        contains (takeback), then appends the command's remaining moves
        (extension). Either side may be empty: a pure append, a pure takeback, a
        takeback-then-different-continuation, or a no-op re-query.
        """
        new_ucis = [m.uci() for m in move_objs]
        old_tail = self._history[rejoin:]
        common = 0
        while (
            common < len(old_tail)
            and common < len(new_ucis)
            and old_tail[common] == new_ucis[common]
        ):
            common += 1

        keep = rejoin + common
        removed = len(self._history) - keep
        for _ in range(removed):
            self._board.pop()
        del self._history[keep:]
        del self._mainline_keys[keep + 1:]

        added = self._push_all(move_objs[common:])
        return self._build_update(is_new_game=False, added=added, removed=removed)

    def _start_new_game(self, start_fen: str, move_objs: List[chess.Move]) -> GameUpdate:
        """Begin a fresh game from ``start_fen`` and apply ``move_objs``."""
        self._started = True
        self._new_game_pending = False
        self._game_start_fen = start_fen
        self._board = chess.Board(start_fen)
        self._history = []
        self._mainline_keys = [self._key(self._board)]
        added = self._push_all(move_objs)
        return self._build_update(is_new_game=True, added=added)

    def _push_all(self, move_objs: List[chess.Move]) -> List[Tuple[str, str]]:
        """Push moves onto the board, recording (uci, fen-after) and mainline key."""
        added: List[Tuple[str, str]] = []
        for move in move_objs:
            if move not in self._board.legal_moves:
                raise ValueError(f"Illegal move {move.uci()} for position {self._board.fen()}")
            self._board.push(move)
            self._history.append(move.uci())
            self._mainline_keys.append(self._key(self._board))
            added.append((move.uci(), self._board.fen()))
        return added

    def _build_update(
        self, *, is_new_game: bool, added: List[Tuple[str, str]], removed: int = 0
    ) -> GameUpdate:
        """Assemble a GameUpdate from current state plus the moves just changed."""
        outcome = self._board.outcome(claim_draw=True)
        return GameUpdate(
            is_new_game=is_new_game,
            start_fen=self._game_start_fen,
            moves_added=added,
            moves_removed=removed,
            total_moves=len(self._history),
            result=outcome.result() if outcome is not None else None,
            termination=outcome.termination.name.lower() if outcome is not None else None,
        )
