#!/usr/bin/env python3
"""Tests for Chess960 start-position helpers and variant-aware ChessGameState.

Why this exists
---------------
Chess960 support hinges on two invariants that are easy to regress:

1. The ``chess960`` flag must survive every position mutation. python-chess only
   emits ``UCI_Chess960`` and applies 960 castling when the board it holds has
   ``chess960`` set, so if ``set_position``/``reset`` silently dropped the flag,
   engines would receive standard-chess castling for a 960 game.
2. A board-reset (the physical home-rank gesture) must return to the SAME
   generated 960 position, never regenerate one and never fall back to the
   standard start. Losing this would change the user's position mid-setup.

The board object identity must also be preserved across resets because
GameManager captures ``game_state.board`` once and uses ``is`` identity checks.
"""

import chess
import pytest

from universalchess.state.chess960 import (
    CHESS960_POSITION_COUNT,
    chess960_fen,
    random_chess960_fen,
    variant_change_requires_restart,
)
from universalchess.state.chess_game import ChessGameState

# Position 518 is the standard chess start in Scharnagl numbering; used as a
# known-answer anchor for chess960_fen().
STANDARD_SCHARNAGL = 518
# Position 0 has both rooks NOT on the a/h files (BBQNNRKR), so it exercises the
# case a standard-only implementation would get wrong.
NON_STANDARD_SCHARNAGL = 0


def test_chess960_fen_covers_full_range_and_is_valid():
    """Every Scharnagl number 0..959 must yield a valid, chess960 start FEN.

    Guards the generator's bounds and validity: an off-by-one in the range or a
    bad FEN would surface as a raised exception or a non-960 board here.
    """
    for number in range(CHESS960_POSITION_COUNT):
        fen = chess960_fen(number)
        board = chess.Board(fen, chess960=True)
        # A legal starting array: 32 pieces, white to move, full castling rights.
        assert board.is_valid()
        assert len(board.piece_map()) == 32
        assert board.turn == chess.WHITE


def test_chess960_fen_known_answers():
    """Scharnagl 518 is the standard start; 0 is BBQNNRKR.

    Pins two known positions so a change to the underlying numbering is caught
    rather than silently shifting which position a number maps to.
    """
    assert chess960_fen(STANDARD_SCHARNAGL).split()[0] == (
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
    )
    assert chess960_fen(NON_STANDARD_SCHARNAGL).split()[0] == (
        "bbqnnrkr/pppppppp/8/8/8/8/PPPPPPPP/BBQNNRKR"
    )


def test_chess960_fen_rejects_out_of_range():
    """Numbers outside 0..959 must raise, not wrap or clamp.

    A silent clamp would hand back a valid-looking but wrong position; the caller
    would never learn its input was bad.
    """
    for bad in (-1, CHESS960_POSITION_COUNT, 10_000):
        with pytest.raises(ValueError):
            chess960_fen(bad)


def test_random_chess960_fen_uses_injected_rng_and_returns_number():
    """random_chess960_fen returns (fen, number) consistent with the RNG.

    Injecting a deterministic RNG proves the number drives the FEN (so callers
    can persist the number/position) rather than the two being unrelated.
    """
    class _FixedRandom:
        def randint(self, low, high):
            assert (low, high) == (0, CHESS960_POSITION_COUNT - 1)
            return NON_STANDARD_SCHARNAGL

    fen, number = random_chess960_fen(rng=_FixedRandom())
    assert number == NON_STANDARD_SCHARNAGL
    assert fen == chess960_fen(NON_STANDARD_SCHARNAGL)


def test_configure_start_sets_flag_and_position():
    """configure_start(chess960=True) makes the state a 960 game at that FEN.

    If the flag were not set, board.copy() handed to an engine would produce
    standard castling; this asserts both the flag and the position land.
    """
    state = ChessGameState()
    fen = chess960_fen(NON_STANDARD_SCHARNAGL)
    state.configure_start(fen, chess960=True)
    assert state.chess960 is True
    assert state.board.chess960 is True
    assert state.fen == fen
    assert state.start_fen == fen


def test_configure_start_preserves_board_identity():
    """configure_start mutates the board in place, not reassigns it.

    GameManager captures game_state.board once and uses `is` identity checks. If
    configure_start reassigned _board, those checks would silently fail and move
    handling would compare against a stale board.
    """
    state = ChessGameState()
    original_board = state.board
    state.configure_start(chess960_fen(NON_STANDARD_SCHARNAGL), chess960=True)
    assert state.board is original_board


def test_reset_keeps_960_position_and_flag():
    """reset() returns to the generated 960 start, not the standard start.

    This is the keep-960 requirement: re-setting up the physical board triggers
    reset(), which must reproduce the same position. A regression to
    board.reset() would show up as the standard start FEN and chess960 False.
    """
    state = ChessGameState()
    fen = chess960_fen(NON_STANDARD_SCHARNAGL)
    state.configure_start(fen, chess960=True)
    # Play a move so reset() has something to undo.
    first_move = next(iter(state.board.legal_moves))
    state.board.push(first_move)
    state.reset()
    assert state.fen == fen
    assert state.chess960 is True
    assert state.board.chess960 is True


def test_reset_to_standard_clears_960():
    """reset_to_standard() drops the variant so a new standard game is clean.

    Without this, a fresh standard game started after a 960 game would inherit
    the 960 start FEN (reset() is variant-aware), corrupting the new game.
    """
    state = ChessGameState()
    state.configure_start(chess960_fen(NON_STANDARD_SCHARNAGL), chess960=True)
    state.reset_to_standard()
    assert state.chess960 is False
    assert state.board.chess960 is False
    assert state.fen == chess.STARTING_FEN
    assert state.start_fen == chess.STARTING_FEN


def test_board_copy_preserves_chess960_flag():
    """board_copy() carries the chess960 flag to the engine/analysis copy.

    Engines only receive UCI_Chess960 when the board python-chess is given has
    chess960 set. If board_copy dropped the flag, analysis of a 960 game would
    use standard castling. This is the seam the engine/analysis paths rely on.
    """
    state = ChessGameState()
    state.configure_start(chess960_fen(NON_STANDARD_SCHARNAGL), chess960=True)
    copy = state.board_copy()
    assert copy.chess960 is True
    assert copy is not state.board
    assert copy.fen() == state.fen


def test_history_positions_are_authoritative_for_960_castling():
    """history_positions() yields correct per-ply FENs for a 960 castle.

    The web live board navigates history by these FENs instead of replaying the
    PGN in the browser, because chess.js mis-computes 960 castling (it places the
    king on the wrong square). This is the authoritative source: the first entry
    is the start (no move), and each later entry carries the true post-move FEN,
    the SAN, and the UCI. Regression: if the root board were built without the
    chess960 flag, the king-onto-rook castle would be illegal and this would raise
    or diverge from the real position -- exactly the browser bug this replaces.
    """
    state = ChessGameState()
    # Cleared-rank 960 position where the king (f1) can castle with the h1 rook.
    start = "4k3/8/8/8/8/8/8/5K1R w K - 0 1"
    state.configure_start(start, chess960=True)
    castle = chess.Move.from_uci("f1h1")  # king-onto-rook kingside
    assert castle in state.board.legal_moves
    state.push_move(castle)

    positions = state.history_positions()
    # Start entry + one move entry.
    assert len(positions) == 2
    assert positions[0] == {"fen": start, "san": None, "uci": None}
    assert positions[1]["uci"] == "f1h1"
    assert positions[1]["san"] == "O-O"
    # The true post-castle FEN has the king on g1 and rook on f1 (960 kingside),
    # which is what the physical/authoritative board records.
    assert positions[1]["fen"] == state.fen
    assert positions[1]["fen"].startswith("4k3/8/8/8/8/8/8/5RK1")


def test_history_positions_standard_game_matches_replay():
    """For a standard game the authoritative FENs equal a normal replay.

    Guards that the helper is correct for the common case too (so the same web
    code path can consume it): after 1.e4 e5 the start plus two move FENs are
    returned in order with SAN e4/e5.
    """
    state = ChessGameState()
    state.push_uci("e2e4")
    state.push_uci("e7e5")
    positions = state.history_positions()
    assert len(positions) == 3
    assert positions[0]["uci"] is None
    assert [p["san"] for p in positions[1:]] == ["e4", "e5"]
    assert positions[-1]["fen"] == state.fen


@pytest.mark.parametrize(
    "current_is_chess960, desired_chess960, game_has_moves, expected",
    [
        # No moves + variant differs -> restart so the board reflects the toggle.
        (False, True, False, True),   # standard game, 960 switched on
        (True, False, False, True),   # 960 game, switched back to standard
        # No moves + variant already matches -> nothing to do.
        (False, False, False, False),
        (True, True, False, False),
        # Moves played -> never restart, regardless of the toggle (defer to next
        # new game so a game in progress is never silently abandoned).
        (False, True, True, False),
        (True, False, True, False),
        (False, False, True, False),
        (True, True, True, False),
    ],
)
def test_variant_change_requires_restart(
    current_is_chess960, desired_chess960, game_has_moves, expected
):
    """The 960-toggle restart predicate gates on both variant-mismatch and no-moves.

    Why this exists: toggling the Chess960 switch must only reset the current
    game (regenerating its start position) when it is safe -- i.e. no moves have
    been played -- and only when the variant actually changed. This guards the
    two regressions that would matter to a user:
      1. If the ``game_has_moves`` guard were dropped, a mid-game toggle would
         return True and the caller would abandon a game in progress (the rows
         with ``game_has_moves=True`` would flip to True and fail here).
      2. If the mismatch check were dropped, a no-op toggle (variant already
         matching) would needlessly restart the game (the matching-variant rows
         would flip to True and fail here).
    """
    assert (
        variant_change_requires_restart(
            current_is_chess960, desired_chess960, game_has_moves
        )
        is expected
    )


def test_set_position_preserves_chess960_flag():
    """set_position keeps the chess960 flag (only the position changes).

    _start_game_mode applies the generated FEN via set_position after
    configure/init; if set_position reset the flag, the game would revert to
    standard chess castling despite being a 960 game.
    """
    state = ChessGameState()
    state.configure_start(chess960_fen(NON_STANDARD_SCHARNAGL), chess960=True)
    other = chess960_fen(NON_STANDARD_SCHARNAGL + 1)
    state.set_position(other)
    assert state.board.chess960 is True
    assert state.fen == other
