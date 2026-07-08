"""Tests for coach_move_check: catching fabricated moves named in a statement.

These guard the anti-hallucination check that lets the coach regenerate instead of
showing an impossible line (the reported bug: "cxd4 in response to d3" when d4 is
empty). Each test states the exact hallucination it guards and how a regression
would surface (a fabricated move slipping through, or a legitimate reference being
wrongly flagged, which would cause needless regeneration/fallback).
"""

from universalchess.managers.game.coach_move_check import (
    find_grounding_problems,
    find_unsupported_claims,
    find_unsupported_moves,
    has_unsupported_move,
)

# After 1.e4 c5, white to move. White has no pawn on d4; a black c5 pawn is present.
FEN_AFTER_1E4_C5 = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2"


def test_flags_impossible_capture_reply_to_a_quiet_pawn_push():
    # The exact reported bug: white plays d3 (d2d3), then the coach suggests the
    # opponent could reply "cxd4". After d3 the d4 square is empty, so cxd4 captures
    # nothing and is illegal both before and after the move. It must be flagged so
    # the statement is regenerated. Regression: returning [] would let the nonsense
    # line reach the panel again.
    unsupported = find_unsupported_moves(
        "What if your opponent plays cxd4 in response to d3?",
        FEN_AFTER_1E4_C5,
        "d2d3",
    )
    assert unsupported == ["cxd4"]


def test_does_not_flag_a_legal_opponent_reply_after_the_move():
    # When white instead plays d4 (d2d4), a black c5 pawn CAN answer cxd4. That is a
    # legal reply in the after-move position, so referencing it must not be flagged.
    # Regression: flagging it would trigger pointless regeneration/fallback and
    # strip a correct, useful observation.
    unsupported = find_unsupported_moves(
        "Watch out: after this, cxd4 wins your central pawn.",
        FEN_AFTER_1E4_C5,
        "d2d4",
    )
    assert unsupported == []


def test_does_not_flag_a_legal_move_in_the_before_position():
    # A legal developing move for the side to move (Nf3 = g1f3 for White) must pass;
    # the coach routinely names the played move or a legal alternative. Regression:
    # flagging legal moves would make almost every statement regenerate.
    unsupported = find_unsupported_moves(
        "Developing with Nf3 would have been more natural.",
        FEN_AFTER_1E4_C5,
        "d2d3",
    )
    assert unsupported == []


def test_skips_bare_pawn_destination_used_as_a_square_reference():
    # "the d4 square" is a square reference, not a move; a bare pawn destination is
    # indistinguishable from a move, so it is deliberately not validated. Regression:
    # validating it would flag legitimate positional commentary as a fabricated move.
    unsupported = find_unsupported_moves(
        "The d4 square is weak and invites a knight there.",
        FEN_AFTER_1E4_C5,
        "d2d3",
    )
    assert unsupported == []


def test_flags_an_invented_piece_move_illegal_for_both_sides():
    # "Ne4" is not reachable by any knight for either side here, so it is illegal in
    # both the before and after positions and must be flagged. Regression: an
    # invented piece move (a common hallucination) would otherwise be shown as a
    # concrete but impossible plan.
    assert has_unsupported_move(
        "You should have jumped in with Ne4 to dominate.",
        FEN_AFTER_1E4_C5,
        "d2d3",
    )


def test_no_moves_named_is_supported():
    # A purely descriptive remark names no move and must never be flagged; this is
    # the desired "describe the idea without naming a move" fallback behavior.
    assert not has_unsupported_move(
        "This grabs central space but leaves your king slightly airy.",
        FEN_AFTER_1E4_C5,
        "d2d3",
    )


def test_invalid_fen_validates_nothing():
    # An unparseable FEN (corrupt data) must not crash or block coaching; the check
    # returns [] so the statement is shown rather than endlessly regenerated.
    assert find_unsupported_moves("Consider Nf3.", "not a fen", "d2d3") == []


def test_flags_claim_of_a_piece_on_an_empty_square():
    # The reported occupancy hallucination: after d3, d4 is empty, but the coach
    # claimed "the pawn on d4". This is not a move so the move check cannot see it;
    # the occupancy check must flag it. Regression: returning [] would let "no pawn
    # on d4" nonsense reach the panel again.
    flagged = find_unsupported_claims(
        "The pawn on d4 becomes an isolated target.",
        FEN_AFTER_1E4_C5,
        "d2d3",
    )
    assert flagged == ["pawn on d4"]


def test_flags_reversed_square_piece_claim():
    # The same false claim phrased "d4 pawn" must also be caught, or the model could
    # evade the check by word order. Regression: only matching "on d4" would miss
    # the common "your d4 pawn" phrasing.
    flagged = find_unsupported_claims(
        "Your d4 pawn is weak.",
        FEN_AFTER_1E4_C5,
        "d2d3",
    )
    assert flagged == ["d4 pawn"]


def test_does_not_flag_a_piece_that_is_actually_present_after_the_move():
    # When white plays d4, a white pawn really is on d4, so referencing it must pass.
    # Regression: flagging a real piece would trigger needless regeneration and drop
    # correct commentary about the played pawn.
    flagged = find_unsupported_claims(
        "The pawn on d4 grabs the center.",
        FEN_AFTER_1E4_C5,
        "d2d4",
    )
    assert flagged == []


def test_does_not_flag_a_piece_present_before_the_move():
    # A pawn on e4 exists (from 1.e4) both before and after d3, so referencing it is
    # legitimate. Regression: only checking the after-position could wrongly flag a
    # piece that was captured or is otherwise only in the before-position.
    flagged = find_unsupported_claims(
        "The pawn on e4 controls the center.",
        FEN_AFTER_1E4_C5,
        "d2d3",
    )
    assert flagged == []


def test_ignores_a_square_reference_that_makes_no_occupancy_claim():
    # "the d4 square" names no piece, so it asserts nothing about occupancy and must
    # not be flagged. Regression: matching a bare square would flag legitimate
    # positional commentary about weak squares.
    flagged = find_unsupported_claims(
        "The d4 square is a strong outpost.",
        FEN_AFTER_1E4_C5,
        "d2d3",
    )
    assert flagged == []


def test_find_grounding_problems_combines_illegal_moves_and_false_claims():
    # The single entry point used before display must report both a fabricated move
    # and a false piece claim in one call. Regression: checking only one class would
    # let the other slip through to the panel.
    problems = find_grounding_problems(
        "Play cxd4 to hit the pawn on d4.",
        FEN_AFTER_1E4_C5,
        "d2d3",
    )
    assert "cxd4" in problems
    assert "pawn on d4" in problems
