"""Tests for move_facts.summarize_move_facts (ground-truth move facts).

Why these tests exist
---------------------
These facts are the anti-hallucination grounding fed to the AI coach: the model is
told to base tactical claims only on them. So the facts must be exactly true --
every emitted fact real, and nothing invented. The motivating regression is the
Ruy Lopez ``3.Bb5``, which the model called a "pin to the king" though the d7 pawn
blocks the diagonal; the pin detector must NOT report a pin there while still
reporting the real target (the c6 knight). Each test pins one fact category and
states how a regression would surface (a missing true fact, or a fabricated one).
"""

from universalchess.managers.game.move_facts import summarize_move_facts

STARTPOS = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def test_ruy_bb5_reports_target_not_a_pin():
    # 3.Bb5 attacks the c6 knight but does NOT pin it to the king (the d7 pawn is
    # behind the knight on the b5-e8 diagonal). Regression: reporting a pin here is
    # exactly the false "pinned to the king" claim we are grounding against; missing
    # the attack fact would drop the move's real point.
    after_nc6 = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
    facts = summarize_move_facts(after_nc6, "f1b5")
    assert facts == ["The bishop attacks the black knight on c6."]
    assert not any("Pins" in f for f in facts)


def test_real_absolute_pin_is_reported():
    # A rook moving onto the same file as an enemy knight and its king, with nothing
    # between the knight and king, is a true absolute pin and must be reported (plus
    # the direct attack on the knight). Regression: failing to detect a genuine pin
    # would starve the coach of a real, describable tactic.
    before = "4k3/8/8/4n3/8/8/8/R4K2 w - - 0 1"
    facts = summarize_move_facts(before, "a1e1")
    assert "The rook attacks the black knight on e5." in facts
    assert "Pins the black knight on e5 to the king." in facts


def test_check_is_reported_and_king_is_not_listed_as_a_target():
    # Bb5+ on an open diagonal gives check; the king must be reported via "Gives
    # check", never as an attacked target. Regression: listing the king as a target
    # or missing the check would misdescribe a forcing move.
    before = "4k3/8/8/8/8/8/8/5BK1 w - - 0 1"
    assert summarize_move_facts(before, "f1b5") == ["Gives check."]


def test_checkmate_is_reported():
    # A back-rank Ra8# must be reported as checkmate (not merely check). Regression:
    # calling a mate "check" understates the most important fact about the move.
    before = "6k1/5ppp/8/8/8/8/8/R6K w - - 0 1"
    assert summarize_move_facts(before, "a1a8") == ["Delivers checkmate."]


def test_capture_names_the_taken_piece():
    # exd5 must report capturing the black pawn on d5, so the coach can speak to the
    # material change. Regression: a missing/incorrect capture fact would let the
    # model invent what was taken.
    before = "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 2"
    assert summarize_move_facts(before, "e4d5") == ["Captures the black pawn on d5."]


def test_en_passant_capture_is_reported():
    # An en-passant capture is still a capture and must be reported as such; the
    # target square is empty, so a naive "piece on to-square" check would miss it.
    before = "rnbqkbnr/pppp2pp/8/4Pp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3"
    assert summarize_move_facts(before, "e5f6") == ["Captures a pawn en passant."]


def test_castling_is_reported_with_side():
    # Kingside castling must be reported as such. Regression: omitting it would drop
    # a defining, non-tactical fact the coach should acknowledge.
    before = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"
    assert summarize_move_facts(before, "e1g1") == ["Castles kingside."]


def test_promotion_is_reported():
    # Promotion to a queen must be reported; the kings are placed so the new queen
    # gives no check, isolating the promotion fact. Regression: missing it would let
    # the model guess the promoted piece.
    before = "8/P7/8/8/8/8/8/4k1K1 w - - 0 1"
    assert summarize_move_facts(before, "a7a8q") == ["Promotes to a queen."]


def test_knight_fork_lists_both_targets():
    # A knight landing where it attacks both the queen and the rook (and not the
    # king) must list both targets -- the factual basis for the coach to call it a
    # fork. Regression: dropping a target would hide half the fork.
    before = "3q1rk1/8/8/8/3N4/8/8/6K1 w - - 0 1"
    facts = summarize_move_facts(before, "d4e6")
    assert facts == [
        "The knight attacks the black queen on d8.",
        "The knight attacks the black rook on f8.",
    ]


def test_chess960_king_onto_rook_castle_is_reported():
    # In Chess960, castling is encoded as a king-onto-rook move (here f1->h1) and is
    # only legal on a board built with chess960=True. Regression: without threading
    # the chess960 flag into the fact extractor, the move is illegal on the default
    # standard board, summarize_move_facts returns [], and the coach silently loses
    # the "Castles" fact for every 960 castling move it reviews.
    before = "4k3/8/8/8/8/8/8/5K1R w K - 0 1"
    assert summarize_move_facts(before, "f1h1", chess960=True) == ["Castles kingside."]


def test_chess960_castle_ignored_without_flag():
    # The same king-onto-rook move on a standard board is illegal, so no facts. This
    # guards that the default (chess960=False) is unchanged and the flag is what
    # enables 960 interpretation, not a silent always-on behavior change.
    before = "4k3/8/8/8/8/8/8/5K1R w K - 0 1"
    assert summarize_move_facts(before, "f1h1") == []


def test_illegal_move_yields_no_facts():
    # An illegal move has no well-defined resulting position; the extractor must
    # return [] (never a fabricated fact) so the coach prompts without assertions.
    assert summarize_move_facts(STARTPOS, "e2e5") == []


def test_invalid_fen_yields_no_facts():
    # A malformed FEN must yield [] rather than raising, so a corrupt row degrades
    # to a fact-free prompt instead of crashing the coach path.
    assert summarize_move_facts("not a fen", "e2e4") == []
