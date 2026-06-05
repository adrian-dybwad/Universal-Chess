#!/usr/bin/env python3
"""Tests for SetupTracker - identity-preserving board setup for occupancy-only hardware.

Why this exists
---------------
The Centaur board senses occupancy only (1 bit/square); it cannot detect piece
identity. The Chessnut app, however, computes its puzzle diff from piece TYPES
in the streamed FEN (proven empirically: it distinguishes "replace" squares from
"correct" ones). So to drive the board to an arbitrary target via the app, we
must report a typed FEN we cannot sense directly.

SetupTracker recovers identity by starting from the standard start position
(where every piece's identity is known) and tracking physical manipulation as
RELOCATIONS rather than chess moves:

- lift(sq): pick up the piece currently on sq (identity known from tracker state).
  A lift while already holding a piece means the previously held piece was taken
  off the board (lifted, never placed) -> it is REMOVED. This is the only way to
  delete a piece. Lifting an empty square holds nothing.
- place(sq): deposit the held piece onto an empty square. Two anomalies are
  handled defensively because real hardware cannot produce them via a normal
  empty->occupied transition:
    * place onto an occupied square: ignored (hand kept), logged.
    * place with an empty hand: the piece came from off-board with unknown
      identity. The square is flagged in unknown_squares so the integration layer
      can fast-flash it ("remove this"); lifting that square clears the flag.

Pieces that belong in the target are RELOCATED, never removed and re-added,
because a piece placed with nothing held has unknown identity.

These tests pin that contract so the state machine cannot silently regress.
"""

import chess

from universalchess.board.setup_tracker import SetupTracker


START_PLACEMENT = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"

# Chessnut puzzle #2293323 placement (read off the app screenshot). Used to prove
# an arbitrary, real target is reachable from the start by relocations + removals
# while preserving piece identity.
PUZZLE_2293323_PLACEMENT = "2r2r2/p4p1k/5R1p/1p1p4/4q2p/7P/P2Q2P1/7K"


def _apply(tracker, events):
    """Apply a sequence of ("lift"|"place", square_name) events in order."""
    for action, name in events:
        square = chess.parse_square(name)
        if action == "lift":
            tracker.lift(square)
        elif action == "place":
            tracker.place(square)
        else:
            raise ValueError(f"unknown action {action!r}")


def _board_from(tracker):
    """Build a chess.Board from the tracker's placement for identity assertions."""
    return chess.Board(tracker.board_fen() + " w - - 0 1")


def test_new_tracker_starts_at_standard_start_position():
    """A fresh tracker must represent the standard start position.

    If init regresses (wrong/empty board), board_fen() diverges from the known
    32-piece start and every downstream relocation builds on a wrong baseline.
    """
    tracker = SetupTracker()

    assert tracker.board_fen() == START_PLACEMENT
    assert tracker.in_hand is None
    assert tracker.unknown_squares == set()


def test_tracker_accepts_custom_start_placement():
    """Tracker must honor a provided starting placement.

    Guards seeding from a non-standard position. If ignored, board_fen() would be
    the default start instead of the supplied placement.
    """
    custom = "8/8/8/8/8/8/8/4K2k"
    tracker = SetupTracker(fen=custom)

    assert tracker.board_fen() == custom


def test_lift_then_place_relocates_piece_preserving_identity():
    """A lift+place must move the exact piece (type AND color) to the new square.

    Regression target: identity loss on relocation. If place deposited a generic
    or wrong piece, the destination would not read back as the original white pawn
    and the source would not be empty.
    """
    tracker = SetupTracker()

    _apply(tracker, [("lift", "e2"), ("place", "e4")])

    board = _board_from(tracker)
    assert board.piece_at(chess.E4) == chess.Piece(chess.PAWN, chess.WHITE)
    assert board.piece_at(chess.E2) is None
    assert board.piece_at(chess.E3) is None  # nothing spurious in between
    assert tracker.in_hand is None


def test_holding_a_piece_then_lifting_again_removes_the_held_piece():
    """lift-without-place-before-next-lift must delete the first piece.

    This is the ONLY removal mechanism. If the held piece were not discarded on
    the next lift, removed pieces would reappear and the target piece count would
    be too high (puzzle never matches).
    """
    tracker = SetupTracker()

    # Lift the b1 knight (held), then lift the g1 knight without placing b1.
    _apply(tracker, [("lift", "b1"), ("lift", "g1")])

    board = _board_from(tracker)
    assert board.piece_at(chess.B1) is None  # discarded on the second lift
    assert board.piece_at(chess.G1) is None  # now in hand, off the board
    assert tracker.in_hand == chess.Piece(chess.KNIGHT, chess.WHITE)


def test_place_with_empty_hand_flags_square_for_removal():
    """Placing with nothing held cannot be identified; flag it, do not fabricate.

    On occupancy-only hardware a piece returned from off-board has unknown
    identity. The tracker must not invent a piece (the FEN would carry a wrong
    type and the app diff would never resolve). Instead it flags the square so the
    integration layer can fast-flash "remove this".
    """
    tracker = SetupTracker()
    before = tracker.board_fen()

    _apply(tracker, [("place", "e4")])  # e4 empty, hand empty

    assert tracker.board_fen() == before          # no fabricated piece
    assert tracker.in_hand is None
    assert tracker.unknown_squares == {chess.E4}   # flagged for removal


def test_lifting_an_unknown_placed_square_clears_the_flag():
    """Removing the unidentified piece must clear its removal flag.

    After the user obeys the fast-flash and lifts the unknown piece, the square is
    empty again and must no longer be flagged; otherwise it would flash forever.
    """
    tracker = SetupTracker()
    _apply(tracker, [("place", "e4")])  # flags e4
    assert tracker.unknown_squares == {chess.E4}

    _apply(tracker, [("lift", "e4")])   # user removes it

    assert tracker.unknown_squares == set()
    assert tracker.in_hand is None
    assert tracker.board_fen() == START_PLACEMENT


def test_place_onto_occupied_square_is_ignored_and_keeps_hand():
    """A place onto an occupied square is an anomaly; ignore it and keep the hand.

    Real hardware only fires place on an empty->occupied transition, so this
    cannot happen normally. If it ever does (noise), overwriting would destroy the
    occupant's identity. The tracker must leave the square untouched and keep the
    held piece so the operator can place it somewhere valid.
    """
    tracker = SetupTracker()

    # Hold the e2 pawn, then (anomalously) place onto d1 which still holds the queen.
    _apply(tracker, [("lift", "e2"), ("place", "d1")])

    board = _board_from(tracker)
    assert board.piece_at(chess.D1) == chess.Piece(chess.QUEEN, chess.WHITE)  # untouched
    assert board.piece_at(chess.E2) is None                                   # was lifted
    assert tracker.in_hand == chess.Piece(chess.PAWN, chess.WHITE)            # still held


def test_relocate_into_square_emptied_earlier_preserves_identity():
    """Relocating onto a square that was emptied earlier deposits the held piece.

    The c8 bishop is removed first (lift c8, then lift a8 discards it), leaving c8
    empty; placing the a8 rook there must land a black rook on c8 with a8 empty.
    This is the "replace" pattern done correctly (empty first, then place).
    """
    tracker = SetupTracker()

    _apply(tracker, [("lift", "c8"), ("lift", "a8"), ("place", "c8")])

    board = _board_from(tracker)
    assert board.piece_at(chess.C8) == chess.Piece(chess.ROOK, chess.BLACK)
    assert board.piece_at(chess.A8) is None
    assert tracker.in_hand is None


def test_lift_from_empty_square_holds_nothing():
    """Lifting an empty square yields an empty hand (no phantom piece).

    Sensor noise or a mistaken lift on an empty square must not put a piece in
    hand. If it did, the next place would deposit a phantom piece.
    """
    tracker = SetupTracker()

    _apply(tracker, [("lift", "e4")])  # e4 empty at start

    assert tracker.in_hand is None
    assert tracker.board_fen() == START_PLACEMENT


def test_full_setup_reaches_puzzle_2293323_from_start():
    """End-to-end: relocations + removals from start must reproduce the puzzle FEN.

    Highest-level guard: proves the principle works for a real, arbitrary target
    while preserving identity (e.g. d8 queen ends on e4 as a black queen, e1 king
    ends on h1 as a white king, a8 rook ends on c8). If any operation loses
    identity, mis-removes, or mis-counts, the final board_fen() will not equal the
    known puzzle placement.

    Target #2293323:
        black: r c8, r f8, k h7, q e4, p a7,f7,h6,b5,d5,h4
        white: R f6, Q d2, K h1, P h3,a2,g2
    """
    tracker = SetupTracker()

    _apply(tracker, _puzzle_2293323_events())

    assert tracker.board_fen() == PUZZLE_2293323_PLACEMENT
    assert tracker.in_hand is None
    assert tracker.unknown_squares == set()

    # Spot-check identities that prove tracking (occupancy alone cannot distinguish
    # these from the pieces that originally sat on those squares).
    board = _board_from(tracker)
    assert board.piece_at(chess.E4) == chess.Piece(chess.QUEEN, chess.BLACK)
    assert board.piece_at(chess.H1) == chess.Piece(chess.KING, chess.WHITE)
    assert board.piece_at(chess.C8) == chess.Piece(chess.ROOK, chess.BLACK)
    assert board.piece_at(chess.F6) == chess.Piece(chess.ROOK, chess.WHITE)
    assert board.piece_at(chess.D2) == chess.Piece(chess.QUEEN, chess.WHITE)


def _puzzle_2293323_events():
    """A safe, self-consistent event sequence that builds puzzle #2293323 from start.

    Ordering rules used to avoid disturbing already-placed pieces and to never
    place onto an occupied square:
    - Relocate a piece only when its destination is empty.
    - To REMOVE a piece, lift it and discard it via the NEXT lift.
    - End a removal chain with a lift on an already-empty square so the hand ends
      empty.
    """
    return [
        # Black rooks into place; the bishops they replace are removed via the
        # double-lift discard (lift bishop, lift rook -> bishop removed; the rook's
        # destination square is now empty, so place is valid).
        ("lift", "c8"), ("lift", "a8"), ("place", "c8"),
        ("lift", "f8"), ("lift", "h8"), ("place", "f8"),
        # Remove both black knights, then relocate the queen: lift b8 (hold), lift
        # g8 (b8 knight removed, hold g8), lift d8 (g8 knight removed, hold queen),
        # place queen on e4.
        ("lift", "b8"), ("lift", "g8"), ("lift", "d8"), ("place", "e4"),
        # Remove the surplus h7 pawn AND relocate the king there: lift h7 (hold
        # pawn), lift e8 (h7 pawn removed, hold king), place king on the now-empty h7.
        ("lift", "h7"), ("lift", "e8"), ("place", "h7"),
        # Black pawns: a7 and f7 stay; relocate c7->h6, b7->b5, d7->d5, e7->h4.
        ("lift", "c7"), ("place", "h6"),
        ("lift", "b7"), ("place", "b5"),
        ("lift", "d7"), ("place", "d5"),
        ("lift", "e7"), ("place", "h4"),
        # White relocations: h1 rook -> f6; remove d2 pawn then d1 queen -> d2;
        # e1 king -> h1 (now empty); h2 pawn -> h3.
        ("lift", "h1"), ("place", "f6"),
        ("lift", "d2"), ("lift", "d1"), ("place", "d2"),
        ("lift", "e1"), ("place", "h1"),
        ("lift", "h2"), ("place", "h3"),
        # Remove all surplus pieces via a double-lift discard chain:
        # g7 (black pawn), a1, b1, c1, f1, g1, b2, c2, e2, f2. The final lift on the
        # now-empty f2 discards the last held piece so the hand ends empty.
        ("lift", "g7"),
        ("lift", "a1"), ("lift", "b1"), ("lift", "c1"), ("lift", "f1"), ("lift", "g1"),
        ("lift", "b2"), ("lift", "c2"), ("lift", "e2"), ("lift", "f2"),
        ("lift", "f2"),
    ]
