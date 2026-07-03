"""Tests for coach_request_builder.build_coach_request.

Why these tests exist
---------------------
The builder is the single source of the move-text/side/move-number context shared
by the board's live coach, the web coach endpoints, and the hint (tip) generator.
A regression here would feed the AI the wrong side to move, the wrong move text (or
the wrong notation), or a fabricated position -- silently producing misleading
coaching for every caller.
"""

import pytest

from universalchess.managers.game.coach_request_builder import build_coach_request

STARTPOS = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def test_builds_san_side_and_move_number_from_startpos():
    # e2e4 from the start must yield move text "e4" (default notation), white to
    # move, move number 1. A regression in side detection or move conversion would
    # misattribute the move.
    request = build_coach_request(STARTPOS, "e2e4")
    assert request is not None
    assert request.move_text == "e4"
    assert request.side_to_move == "white"
    assert request.move_number == 1
    assert request.fen_before == STARTPOS


def test_black_to_move_is_detected_from_fen():
    # After 1.e4, black is to move; the builder must read side-to-move from the
    # FEN (not assume white), so the coach addresses the correct player.
    after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
    request = build_coach_request(after_e4, "e7e5")
    assert request is not None
    assert request.move_text == "e5"
    assert request.side_to_move == "black"
    assert request.move_number == 1


@pytest.mark.parametrize(
    "notation, expected",
    [
        # A knight move renders differently per notation, so it distinguishes them:
        # SAN "Nf3", LAN "Ng1-f3", UCI "g1f3", figurine "\u2658f3". Guards that the
        # user's notation actually reaches the coached move text.
        ("san", "Nf3"),
        ("lan", "Ng1-f3"),
        ("uci", "g1f3"),
        ("figurine", "\u2658f3"),
    ],
)
def test_move_is_rendered_in_requested_notation(notation, expected):
    request = build_coach_request(STARTPOS, "g1f3", notation=notation)
    assert request is not None
    assert request.move_text == expected


def test_verified_facts_are_attached_and_exclude_false_pins():
    # The builder must attach the ground-truth move facts so the coach is grounded.
    # For the Ruy 3.Bb5 that means the real target (the c6 knight) is present and no
    # "pin" fact is fabricated. Regression: dropping facts removes the grounding, and
    # a pin fact here is the exact hallucination this work targets.
    after_nc6 = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
    request = build_coach_request(after_nc6, "f1b5")
    assert request is not None
    assert request.facts == ("The bishop attacks the black knight on c6.",)


def test_evals_are_passed_through():
    # Eval context provided by the caller must reach the request unchanged so the
    # prompt can describe the evaluation swing.
    request = build_coach_request(STARTPOS, "e2e4", eval_before_cp=20, eval_after_cp=35)
    assert request is not None
    assert request.eval_before_cp == 20
    assert request.eval_after_cp == 35


def test_potential_move_flag_defaults_off_and_propagates_when_set():
    # Played-move requests must default the potential-move flag off; a tip caller
    # passing is_potential_move must have it reach the request so the prompt frames
    # the move as a not-yet-played hint. Regression: dropping the flag here would
    # make tips read as critiques of an already-played move.
    played = build_coach_request(STARTPOS, "e2e4")
    assert played is not None
    assert played.is_potential_move is False

    tip = build_coach_request(STARTPOS, "e2e4", is_potential_move=True)
    assert tip is not None
    assert tip.is_potential_move is True


def test_opponent_move_flag_defaults_off_and_propagates_when_set():
    # The opponent-move flag must default off (a request is the player's own move
    # unless stated) and reach the request when set, so the prompt frames the
    # opponent's move as the opponent's. Regression: dropping the flag here would
    # let the coach address an opponent's move as if the player made it.
    own = build_coach_request(STARTPOS, "e2e4")
    assert own is not None
    assert own.is_opponent_move is False

    opponent = build_coach_request(STARTPOS, "e2e4", is_opponent_move=True)
    assert opponent is not None
    assert opponent.is_opponent_move is True


def test_invalid_fen_returns_none():
    # A malformed FEN must yield None (not raise) so the endpoint returns a clean
    # error instead of 500-ing on corrupt input.
    assert build_coach_request("not a fen", "e2e4") is None


def test_invalid_uci_returns_none():
    # A non-UCI move string must yield None rather than prompting the AI with
    # garbage; UCI is the stored move format, so a bad value signals bad data.
    assert build_coach_request(STARTPOS, "hello") is None


def test_illegal_move_falls_back_to_uci_text():
    # A syntactically valid UCI that is illegal in the position (here a move from
    # the empty e4 square, which makes python-chess' san() raise) must still build
    # a request, using the UCI string as the move text, so a single corrupt row
    # doesn't blank the coach for an otherwise valid FEN.
    request = build_coach_request(STARTPOS, "e4e5")
    assert request is not None
    assert request.move_text == "e4e5"
