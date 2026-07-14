"""Tests for the derived-engine move-selection policies (Worstfish, Drawfish).

Background / why these tests exist
----------------------------------
Worstfish and Drawfish are not chess engines in their own right; they are pure
move-selection policies layered on top of Stockfish's per-move evaluations.
``select_worst_move`` plays the move Stockfish rates worst for the mover;
``select_drawfish_move`` is inspired by the chess.com "Zach" beginner bot, which
refuses to win: it never willingly delivers checkmate, avoids captures, and
steers the evaluation toward equality rather than pressing an advantage.

These are the highest-level pure entry points for the two behaviours, so they
are tested directly with hand-built ``Candidate`` lists (mover-POV scores, plus
an ``is_capture`` flag). No Stockfish process is involved -- the policies operate
solely on their arguments, so the tests are deterministic (a seeded RNG is
injected for the randomised paths) and need no engine binary.
"""

import random

import chess
import chess.engine
from chess.engine import Cp, Mate

from universalchess.services.derived_engines.policies import (
    OPTION_AVOID_CAPTURES,
    OPTION_RANDOMNESS,
    Candidate,
    SelectionContext,
    select_drawfish_move,
    select_worst_move,
)


def _cand(uci: str, score: chess.engine.Score, is_capture: bool = False) -> Candidate:
    """Build a Candidate for a move (given as UCI) with a mover-POV score."""
    return Candidate(move=chess.Move.from_uci(uci), score=score, is_capture=is_capture)


def _ctx(randomness: int = 0, avoid_captures: bool = True, seed: int = 0) -> SelectionContext:
    """Build a SelectionContext with a seeded RNG for deterministic randomness."""
    return SelectionContext(
        options={
            OPTION_RANDOMNESS: randomness,
            OPTION_AVOID_CAPTURES: 1 if avoid_captures else 0,
        },
        rng=random.Random(seed),
    )


# ---------------------------------------------------------------------------
# select_worst_move
# ---------------------------------------------------------------------------

def test_worst_move_picks_lowest_scoring_move():
    """Worstfish plays the move rated worst (lowest) for the side to move.

    Why: this is the entire point of Worstfish. How a regression manifests: if
    the comparison is inverted (or it returns the best move), this returns the
    +1.00 move instead of the -0.50 move.
    """
    candidates = [
        _cand("e2e4", Cp(100)),
        _cand("d2d4", Cp(-50)),
        _cand("g1f3", Cp(20)),
    ]
    assert select_worst_move(candidates) == chess.Move.from_uci("d2d4")


def test_worst_move_prefers_getting_mated_over_merely_bad():
    """A move that gets the mover mated is worse than any centipawn loss.

    Why: mate scores must order below any finite eval so Worstfish walks into
    mate. How it manifests: if Mate(-1) were treated as a large positive/uncomparable
    value, the -5.00 move would be chosen and Worstfish would dodge forced mate.
    """
    candidates = [
        _cand("a2a3", Cp(-500)),
        _cand("h2h3", Mate(-1)),  # mover gets mated next move
    ]
    assert select_worst_move(candidates) == chess.Move.from_uci("h2h3")


def test_worst_move_tie_breaks_deterministically_by_uci():
    """Equal-scored worst moves resolve to the lexicographically smallest UCI.

    Why: a stable tie-break keeps the engine reproducible (needed for the UCI
    tests and any golden output). How it manifests: without the tie-break the
    winner depends on dict/list iteration order and this assertion flakes.
    """
    candidates = [
        _cand("g1f3", Cp(-50)),
        _cand("b1c3", Cp(-50)),
        _cand("e2e4", Cp(200)),
    ]
    assert select_worst_move(candidates) == chess.Move.from_uci("b1c3")


def test_worst_move_randomness_zero_plays_single_worst():
    """Randomness 0 (Worstfish's default) always plays the single worst move.

    Why: R=0 must preserve Worstfish's identity (the one worst move), so the
    knob is opt-in. How it manifests: any randomisation at R=0 could return the
    -0.30 move or the +2.00 move instead of the worst -0.50 move d2d4.
    """
    candidates = [
        _cand("d2d4", Cp(-50)),
        _cand("a2a3", Cp(-30)),
        _cand("e2e4", Cp(200)),
    ]
    ctx = _ctx(randomness=0)
    assert select_worst_move(candidates, ctx) == chess.Move.from_uci("d2d4")


def test_worst_move_randomness_stays_within_the_worst_pool():
    """Randomness R only ever picks among the R+1 worst-rated moves.

    Why: the knob must add unpredictability without letting Worstfish play a
    clearly non-terrible move. How it manifests: with R=1 the pool is the two
    worst moves {d2d4(-0.50), a2a3(-0.30)}; if the window were miscomputed, the
    good +2.00 move e2e4 could leak in. Checked across seeds so it is not a lucky
    single draw.
    """
    candidates = [
        _cand("d2d4", Cp(-50)),
        _cand("a2a3", Cp(-30)),
        _cand("e2e4", Cp(200)),
    ]
    allowed = {chess.Move.from_uci("d2d4"), chess.Move.from_uci("a2a3")}
    for seed in range(25):
        ctx = _ctx(randomness=1, seed=seed)
        assert select_worst_move(candidates, ctx) in allowed


def test_worst_move_randomness_actually_varies_the_choice():
    """A positive Randomness produces more than one distinct worst-pool move.

    Why: proves the RNG is wired into Worstfish, not merely that the pool is
    bounded. How it manifests: if randomness were ignored (always the single
    worst), every seed yields d2d4 and the distinct set has size 1.
    """
    candidates = [
        _cand("d2d4", Cp(-50)),
        _cand("a2a3", Cp(-30)),
        _cand("g1f3", Cp(-10)),
    ]
    chosen = {
        select_worst_move(candidates, _ctx(randomness=2, seed=seed)).uci()
        for seed in range(25)
    }
    assert len(chosen) > 1


# ---------------------------------------------------------------------------
# select_drawfish_move -- equality steering
# ---------------------------------------------------------------------------

def test_drawfish_never_delivers_checkmate_when_alternatives_exist():
    """Drawfish refuses to play a checkmating move if any non-mating move exists.

    Why: the defining trait is that it never willingly checkmates. How it
    manifests: if mate-delivering moves are not excluded, the Mate(1) move (best
    by raw eval) would be chosen and Drawfish would happily mate.
    """
    candidates = [
        _cand("d1h5", Mate(1)),   # mover delivers mate
        _cand("g1f3", Cp(30)),
    ]
    assert select_drawfish_move(candidates) == chess.Move.from_uci("g1f3")


def test_drawfish_picks_move_closest_to_equality():
    """Among non-mating moves Drawfish picks the one nearest 0.00 (never presses).

    Why: it "avoids ever making a good move" and shuffles toward equality rather
    than converting an advantage. How it manifests: if it maximised eval it would
    pick +3.00; if it minimised eval (Worstfish-style) it would pick -2.00.
    Closest-to-zero must pick +0.10.
    """
    candidates = [
        _cand("e2e4", Cp(300)),
        _cand("a2a3", Cp(10)),
        _cand("d2d4", Cp(-200)),
    ]
    assert select_drawfish_move(candidates) == chess.Move.from_uci("a2a3")


def test_drawfish_avoids_getting_mated():
    """A move that gets Drawfish mated is far from equality, so it is not chosen.

    Why: closest-to-zero must treat Mate(-1) as an extreme (huge magnitude)
    value, not fold it to a small number. How it manifests: if a mate score
    flattened to ~0, the losing move would look "equal" and be preferred over a
    genuinely near-equal -0.50 move.
    """
    candidates = [
        _cand("f2f3", Mate(-1)),  # mover gets mated
        _cand("g1f3", Cp(-50)),
    ]
    assert select_drawfish_move(candidates) == chess.Move.from_uci("g1f3")


def test_drawfish_defends_when_losing_choosing_least_bad_move():
    """When every move loses, Drawfish plays the least-bad (closest to 0) one.

    Why: it does not resign or lose on purpose -- forcing a loss requires leaving
    it only a mating move (later test). How it manifests: if it played the lowest
    eval it would pick -9.00; closest-to-zero must pick -1.00.
    """
    candidates = [
        _cand("a2a3", Cp(-500)),
        _cand("b2b3", Cp(-100)),
        _cand("c2c3", Cp(-900)),
    ]
    assert select_drawfish_move(candidates) == chess.Move.from_uci("b2b3")


def test_drawfish_forced_to_mate_when_every_move_is_mate():
    """If all legal moves deliver mate, Drawfish must play one (this is how you win).

    Why: the documented way to beat it is to leave it only a checkmating move.
    The mate exclusion must fall back to the full pool rather than crash or
    return None. How it manifests: an empty non-mating pool would raise
    ValueError from min()/sorted-then-index if the fallback were missing.
    """
    candidates = [
        _cand("d1h5", Mate(1)),
        _cand("a1a2", Mate(2)),
    ]
    # A move is returned (deterministic uci tie-break), not an exception/None.
    assert select_drawfish_move(candidates) == chess.Move.from_uci("a1a2")


# ---------------------------------------------------------------------------
# select_drawfish_move -- capture avoidance
# ---------------------------------------------------------------------------

def test_drawfish_avoids_captures_even_when_a_capture_is_more_equal():
    """With AvoidCaptures on (default), a non-capture is chosen over a capture.

    Why: capture-avoidance is a layer above the equality metric, mirroring the
    Zach bot ignoring hanging pieces. How it manifests: without the exclusion the
    capture (+0.05, nearest 0.00) would win over the non-capture (+0.30); the
    exclusion must flip the choice to the non-capture.
    """
    candidates = [
        _cand("e5d6", Cp(5), is_capture=True),   # nearest equality, but a capture
        _cand("a2a3", Cp(30), is_capture=False),
    ]
    # ctx=None -> defaults (AvoidCaptures on, Randomness 0).
    assert select_drawfish_move(candidates) == chess.Move.from_uci("a2a3")


def test_drawfish_allows_captures_when_every_move_is_a_capture():
    """Capture-avoidance falls back to captures rather than returning nothing.

    Why: in a position where every legal move captures, the engine must still
    move. How it manifests: without the fallback the non-capture pool is empty
    and sorted()[0] would raise IndexError; with it, the most-equal capture
    (+0.05) is played.
    """
    candidates = [
        _cand("e5d6", Cp(5), is_capture=True),
        _cand("e5f6", Cp(200), is_capture=True),
    ]
    assert select_drawfish_move(candidates) == chess.Move.from_uci("e5d6")


def test_drawfish_takes_capture_when_avoidance_disabled():
    """With AvoidCaptures off, the capture nearest equality is chosen.

    Why: the toggle must actually change behaviour. How it manifests: if the
    option were ignored, this returns the non-capture a2a3 (as when avoidance is
    on) instead of the more-equal capture e5d6.
    """
    candidates = [
        _cand("e5d6", Cp(5), is_capture=True),
        _cand("a2a3", Cp(30), is_capture=False),
    ]
    ctx = _ctx(randomness=0, avoid_captures=False)
    assert select_drawfish_move(candidates, ctx) == chess.Move.from_uci("e5d6")


def test_drawfish_never_mates_via_capture_even_with_avoidance_disabled():
    """Mate exclusion outranks the capture setting: a mating capture is skipped.

    Why: "never willingly checkmate" must hold regardless of AvoidCaptures. How
    it manifests: with avoidance off, a naive implementation could let the
    mating capture d1h5 through as the most-equal *capture*; the mate layer must
    still exclude it, leaving the non-capture a2a3.
    """
    candidates = [
        _cand("d1h5", Mate(1), is_capture=True),  # capturing mate
        _cand("a2a3", Cp(30), is_capture=False),
    ]
    ctx = _ctx(randomness=0, avoid_captures=False)
    assert select_drawfish_move(candidates, ctx) == chess.Move.from_uci("a2a3")


# ---------------------------------------------------------------------------
# select_drawfish_move -- randomness knob
# ---------------------------------------------------------------------------

def test_drawfish_randomness_zero_is_deterministic_most_equal_move():
    """Randomness 0 always plays the single move closest to equality.

    Why: R=0 is the reproducible baseline the UCI tests rely on. How it
    manifests: any randomisation at R=0 would let a farther move (e.g. -0.20 or
    +3.00) be chosen instead of the +0.10 move.
    """
    candidates = [
        _cand("a2a3", Cp(10)),
        _cand("b2b3", Cp(-20)),
        _cand("e2e4", Cp(300)),
    ]
    ctx = _ctx(randomness=0)
    assert select_drawfish_move(candidates, ctx) == chess.Move.from_uci("a2a3")


def test_drawfish_randomness_stays_within_the_most_equal_pool():
    """Randomness R only ever picks among the R+1 moves closest to equality.

    Why: shuffling must add variety without throwing the game -- a clearly worse
    move must never be selected. How it manifests: with R=1 the pool is the two
    most-equal moves {a2a3(+0.10), b2b3(-0.20)}; if the window were miscomputed,
    the far +3.00 move e2e4 could leak in. Checked across seeds so it is not a
    lucky single draw.
    """
    candidates = [
        _cand("a2a3", Cp(10)),
        _cand("b2b3", Cp(-20)),
        _cand("e2e4", Cp(300)),
    ]
    allowed = {chess.Move.from_uci("a2a3"), chess.Move.from_uci("b2b3")}
    for seed in range(25):
        ctx = _ctx(randomness=1, seed=seed)
        assert select_drawfish_move(candidates, ctx) in allowed


def test_drawfish_randomness_actually_varies_the_choice():
    """A positive Randomness produces more than one distinct move across seeds.

    Why: proves the RNG is wired in, not that the pool is merely bounded. How it
    manifests: if randomness were ignored (always the most-equal move), every
    seed yields a2a3 and the distinct set has size 1.
    """
    candidates = [
        _cand("a2a3", Cp(10)),
        _cand("b2b3", Cp(-20)),
        _cand("g1f3", Cp(40)),
    ]
    chosen = {
        select_drawfish_move(candidates, _ctx(randomness=2, seed=seed)).uci()
        for seed in range(25)
    }
    assert len(chosen) > 1


def test_drawfish_without_rng_is_deterministic_even_with_randomness_set():
    """Randomness > 0 but no RNG falls back to the deterministic most-equal move.

    Why: a policy must never reach for a global RNG; absent an injected one it
    stays reproducible. How it manifests: if it created its own RNG, this call
    could return b2b3 or g1f3 instead of the most-equal a2a3.
    """
    candidates = [
        _cand("a2a3", Cp(10)),
        _cand("b2b3", Cp(-20)),
        _cand("g1f3", Cp(40)),
    ]
    ctx = SelectionContext(options={OPTION_RANDOMNESS: 5, OPTION_AVOID_CAPTURES: 1}, rng=None)
    assert select_drawfish_move(candidates, ctx) == chess.Move.from_uci("a2a3")
