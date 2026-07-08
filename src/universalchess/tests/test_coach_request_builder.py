"""Tests for coach_request_builder.build_coach_request.

Why these tests exist
---------------------
The builder is the single source of the move-text/side/move-number context shared
by the board's live coach, the web coach endpoints, and the hint (tip) generator.
A regression here would feed the AI the wrong side to move, the wrong move text (or
the wrong notation), or a fabricated position -- silently producing misleading
coaching for every caller.
"""

import chess
import chess.engine
import pytest

from universalchess.managers.game.coach_request_builder import (
    build_coach_request,
    describe_placement,
    format_candidate_lines,
)

STARTPOS = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _info(uci, score):
    """Build a minimal analyse() InfoDict with a single-move pv and a PovScore."""
    return {"pv": [chess.Move.from_uci(uci)], "score": score}


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


def test_build_coach_request_sets_board_placement_of_position_after_the_move():
    # The request must carry an authoritative piece placement of the position AFTER
    # the move, since LLMs read FEN poorly and invent pieces. After 1.e4 the pawn is
    # on e4, not e2. Regression: an empty or pre-move placement would reopen the
    # "pawn on d4 that isn't there" class of hallucination.
    request = build_coach_request(STARTPOS, "e2e4")
    assert request is not None
    # White's pawn list shows e4 (moved) and not e2 (vacated) in the resulting board.
    assert "pawns a2, b2, c2, d2, e4, f2, g2, h2" in request.board_after_text


def test_describe_placement_lists_both_colors_officers_and_pawns():
    # describe_placement is the ground truth the coach relies on, so it must name
    # each side's pieces by square. Regression: a missing color or piece would let
    # the coach "fill in" the gap with an invented piece.
    text = describe_placement(chess.Board())
    assert text.startswith("White: ")
    assert "Ke1" in text and "Qd1" in text
    assert "pawns a2, b2, c2, d2, e2, f2, g2, h2" in text
    assert "Black: " in text
    assert "Ke8" in text


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


def test_language_defaults_to_english_and_propagates_when_set():
    # The response language must default to English (no directive downstream) and
    # reach the request when a caller supplies one, so the coach's language
    # selection actually shapes the prompt. Regression: dropping the language here
    # would ignore the user's Coach Language setting for every caller.
    default = build_coach_request(STARTPOS, "e2e4")
    assert default is not None
    assert default.language == "English"

    localized = build_coach_request(STARTPOS, "e2e4", language="German")
    assert localized is not None
    assert localized.language == "German"


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


def test_candidate_lines_format_move_and_white_eval_best_first():
    # MultiPV output must become "<move in notation> (<white-perspective eval>)"
    # strings preserving the engine's order (best first). Regression: wrong order,
    # dropped eval, or wrong notation would misrepresent the engine's preferences.
    infos = [
        _info("e2e4", chess.engine.PovScore(chess.engine.Cp(30), chess.WHITE)),
        _info("d2d4", chess.engine.PovScore(chess.engine.Cp(25), chess.WHITE)),
        _info("g1f3", chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE)),
    ]
    lines = format_candidate_lines(STARTPOS, infos, notation="san")
    assert lines == ("e4 (+0.30)", "d4 (+0.25)", "Nf3 (+0.20)")


def test_candidate_line_eval_is_white_perspective_for_black_to_move():
    # The eval must be reported from white's perspective (matching the coach's
    # convention) regardless of side to move. For black to move, a PovScore from
    # black's POV of +40cp is -0.40 for white. Regression: reporting the raw POV
    # score would flip the sign and mislead the coach about who stands better.
    after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
    infos = [_info("c7c5", chess.engine.PovScore(chess.engine.Cp(40), chess.BLACK))]
    lines = format_candidate_lines(after_e4, infos, notation="san")
    assert lines == ("c5 (-0.40)",)


def test_candidate_line_formats_mate_score():
    # A forced mate must render as "#N" (white) so the coach can call it out. A
    # mate for white in 2 is "#2". Regression: treating mate as a centipawn number
    # would print a nonsense pawn value.
    infos = [_info("e2e4", chess.engine.PovScore(chess.engine.Mate(2), chess.WHITE))]
    lines = format_candidate_lines(STARTPOS, infos, notation="san")
    assert lines == ("e4 (#2)",)


def test_candidate_lines_skip_info_without_pv():
    # An info with no principal variation (no move) must be skipped rather than
    # crashing or emitting an empty entry. Regression: indexing pv[0] on an empty
    # list would raise and abort the whole enrichment.
    infos = [
        {"pv": [], "score": chess.engine.PovScore(chess.engine.Cp(10), chess.WHITE)},
        _info("e2e4", chess.engine.PovScore(chess.engine.Cp(30), chess.WHITE)),
    ]
    lines = format_candidate_lines(STARTPOS, infos, notation="san")
    assert lines == ("e4 (+0.30)",)


def test_candidate_lines_empty_when_no_infos():
    # No analysis results must yield an empty tuple so the caller simply omits the
    # alternatives block (no header, no fabricated content).
    assert format_candidate_lines(STARTPOS, [], notation="san") == ()


def test_candidate_lines_empty_on_invalid_fen():
    # An unparseable FEN must yield () rather than raising, so a corrupt position
    # degrades to no alternatives instead of breaking coaching.
    infos = [_info("e2e4", chess.engine.PovScore(chess.engine.Cp(30), chess.WHITE))]
    assert format_candidate_lines("not a fen", infos, notation="san") == ()


# --- Chess960 -------------------------------------------------------------

# A minimal 960-style position where the king (f1) can castle with the h1 rook.
# In Chess960 the castling move is encoded king-onto-rook: f1->h1.
_C960_CASTLE_FEN = "4k3/8/8/8/8/8/8/5K1R w K - 0 1"


def test_build_coach_request_chess960_castle_renders_san_and_facts():
    # A 960 castling move (king-onto-rook f1h1) is only legal on a chess960 board.
    # Regression: without threading chess960 into the builder, the move is illegal
    # on the default standard board, so move_text falls back to raw UCI "f1h1" and
    # the "Castles kingside." fact is dropped -- the coach then describes a real
    # castle as an anonymous king shuffle for every 960 game.
    request = build_coach_request(_C960_CASTLE_FEN, "f1h1", notation="san", chess960=True)
    assert request is not None
    assert request.move_text == "O-O"
    assert request.facts == ("Castles kingside.",)
    assert request.chess960 is True


def test_build_coach_request_defaults_chess960_off():
    # The flag must default off so standard games are unchanged and requests are not
    # silently marked 960. Regression: a default of True would misconfigure every
    # standard-chess coach request.
    request = build_coach_request(STARTPOS, "e2e4")
    assert request is not None
    assert request.chess960 is False


def test_candidate_lines_chess960_castle_move():
    # A MultiPV candidate that is a 960 castle (f1h1) must render as "O-O" when the
    # board is built chess960-aware. Regression: without the flag the move is illegal
    # for san(), so it falls back to the raw UCI "f1h1", presenting the engine's top
    # move as a non-castle to the coach.
    infos = [_info("f1h1", chess.engine.PovScore(chess.engine.Cp(30), chess.WHITE))]
    lines = format_candidate_lines(_C960_CASTLE_FEN, infos, notation="san", chess960=True)
    assert lines == ("O-O (+0.30)",)
