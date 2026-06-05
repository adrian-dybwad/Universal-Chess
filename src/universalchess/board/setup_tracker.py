# Setup Tracker
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""Identity-preserving board setup tracker for occupancy-only hardware.

The Centaur board senses occupancy only (a piece is present or absent); it cannot
detect piece identity. Some clients (e.g. the Chessnut app in puzzle mode) compute
their board diff from piece TYPES in the streamed FEN, so to drive the board to an
arbitrary target through such a client we must report a typed FEN we cannot sense.

SetupTracker recovers identity by starting from a position whose identities ARE
known (the standard start) and interpreting physical manipulation as RELOCATIONS
rather than chess moves:

- ``lift(square)``: pick up the piece currently on ``square``. Its identity is
  known from tracker state. A lift while already holding a piece means the held
  piece was taken off the board (lifted, never placed) and is therefore REMOVED -
  the only deletion mechanism.
- ``place(square)``: deposit the held piece onto an empty square.

Two situations cannot occur via a normal hardware empty->occupied transition and
are handled defensively:

- place onto an occupied square: ignored (the held piece is kept), logged. Never
  overwrites, so an occupant's identity is never destroyed.
- place with an empty hand: the piece came from off-board and its identity is
  unknown. No piece is fabricated; the square is recorded in :attr:`unknown_squares`
  so the integration layer can signal "remove this" (e.g. a fast LED flash).
  Lifting that square clears the flag.

The tracker is pure (no hardware or emulator dependencies) and exposes the result
as a placement-only FEN via :meth:`board_fen`.
"""

from typing import Optional, Set

import chess

from universalchess.board.logging import log

STANDARD_START_PLACEMENT = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"


class SetupTracker:
    """Tracks identity-preserving board setup from a known starting position.

    Assumes the physical board begins in the position described by the seed FEN
    (default: standard start), where every piece's identity is known. Relocations
    and removals are tracked from there so a typed placement FEN can be produced
    despite occupancy-only sensing.
    """

    def __init__(self, fen: str = STANDARD_START_PLACEMENT):
        """Initialize the tracker.

        Args:
            fen: Seed position. May be a placement-only FEN or a full FEN; only the
                placement field is used. Defaults to the standard start position.
        """
        placement = fen.split()[0] if fen else STANDARD_START_PLACEMENT
        self._board = chess.Board()
        self._board.set_board_fen(placement)
        self._in_hand: Optional[chess.Piece] = None
        self._unknown_squares: Set[int] = set()

    @property
    def in_hand(self) -> Optional[chess.Piece]:
        """The piece currently lifted off the board, or None if the hand is empty."""
        return self._in_hand

    @property
    def unknown_squares(self) -> Set[int]:
        """Squares holding an unidentified piece (placed with an empty hand).

        Returns a copy so callers cannot mutate internal state. The integration
        layer should signal these squares for removal (e.g. fast LED flash).
        """
        return set(self._unknown_squares)

    def board_fen(self) -> str:
        """Return the current position as a placement-only FEN."""
        return self._board.board_fen()

    def lift(self, square: int) -> None:
        """Pick up the piece on ``square``.

        If a piece is already held, it was lifted earlier and never placed, so it
        is treated as removed from the board and discarded. Lifting an empty square
        results in an empty hand.

        Args:
            square: Chess square index (0=a1 .. 63=h8).
        """
        if self._in_hand is not None:
            log.debug(
                f"[SetupTracker] Held {self._in_hand.symbol()} discarded (removed from board) "
                f"on lift of {chess.square_name(square)}"
            )
        self._in_hand = self._board.remove_piece_at(square)
        self._unknown_squares.discard(square)

    def place(self, square: int) -> None:
        """Deposit the held piece onto ``square``.

        Args:
            square: Chess square index (0=a1 .. 63=h8).
        """
        if self._in_hand is None:
            # Piece returned from off-board: identity is unknown on occupancy-only
            # hardware. Do not fabricate a piece; flag the square for removal.
            self._unknown_squares.add(square)
            log.warning(
                f"[SetupTracker] place on {chess.square_name(square)} with empty hand - "
                f"unknown piece, flagged for removal"
            )
            return

        if self._board.piece_at(square) is not None:
            # Cannot happen via a normal empty->occupied transition. Ignore rather
            # than overwrite so the occupant's identity is preserved.
            log.warning(
                f"[SetupTracker] place on occupied {chess.square_name(square)} ignored "
                f"(anomaly); keeping held {self._in_hand.symbol()}"
            )
            return

        self._board.set_piece_at(square, self._in_hand)
        self._in_hand = None
        self._unknown_squares.discard(square)

    def reset(self, fen: str = STANDARD_START_PLACEMENT) -> None:
        """Reset the tracker to a seed position and clear hand/flags.

        Args:
            fen: Seed position (placement-only or full FEN). Defaults to start.
        """
        placement = fen.split()[0] if fen else STANDARD_START_PLACEMENT
        self._board.set_board_fen(placement)
        self._in_hand = None
        self._unknown_squares.clear()


__all__ = ["SetupTracker", "STANDARD_START_PLACEMENT"]
