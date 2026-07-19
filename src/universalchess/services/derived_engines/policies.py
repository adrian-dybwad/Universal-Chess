# Derived-engine move-selection policies
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Pure move-selection policies for the derived engines. Each policy takes the
# list of legal moves already scored by Stockfish (from the side-to-move's
# point of view) plus an optional selection context (resolved UCI options and an
# injected RNG) and returns the move to play. No I/O and no engine process --
# the policies operate solely on their arguments, which keeps them deterministic
# (for a fixed RNG) and directly unit-testable.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

import chess
import chess.engine

# Sentinel used to flatten a Score to a comparable integer for the "closest to
# equality" metric. It must exceed any realistic centipawn evaluation so that a
# mate score is always treated as more extreme than any finite advantage; a
# won/lost position must never masquerade as "near equal" for Drawfish.
MATE_FLATTEN_SCORE = 1_000_000

# Centipawn window that bounds the ``Randomness`` shuffle by move *quality*, not
# just rank. Only moves whose objective value is within this many centipawns of
# the best candidate's objective are eligible to be shuffled among. Rank alone
# is unsafe: in a forced/check position with few legal moves (or any position
# where only a couple of moves are near the objective), an ``R+1`` slice by rank
# sweeps in outright blunders. That regressed on dgt-64 (game 54): in check with
# four legal replies and R=3, the pool was all four moves, so a queen-hanging
# reply (~5 pawns from equality) was a 1-in-4 random pick and Drawfish threw the
# queen. ~0.75 pawns keeps genuine variety among near-equal moves while a
# material-losing move can never enter the pool.
RANDOMNESS_TOLERANCE_CP = 75

# UCI option names shared between the engine spec (which advertises them) and the
# policy (which reads their resolved values from the SelectionContext). Kept as
# constants so the handshake, the setoption parser, and the policy cannot drift.
OPTION_RANDOMNESS = "Randomness"
OPTION_AVOID_CAPTURES = "AvoidCaptures"


@dataclass(frozen=True)
class Candidate:
    """A legal move and Stockfish's evaluation of it, from the mover's POV.

    ``score`` is a :class:`chess.engine.Score` already taken from the point of
    view of the side to move (positive is good for the mover). ``is_capture``
    records whether the move captures on the board it was generated from; it is
    stored here (rather than recomputed in a policy) so the policies stay pure
    and independent of any board object. Storing the POV-adjusted score keeps
    the policies independent of whose turn it is.
    """

    move: chess.Move
    score: chess.engine.Score
    is_capture: bool = False


@dataclass(frozen=True)
class SelectionContext:
    """Runtime inputs a policy may consult: resolved UCI options and an RNG.

    ``options`` maps a UCI option name to its current integer value (a ``check``
    option is 0/1). ``rng`` is injected so randomised policies stay deterministic
    under test; when it is ``None`` a policy must fall back to deterministic
    behaviour rather than reach for a global RNG.
    """

    options: Mapping[str, int]
    rng: Optional[random.Random] = None


def _delivers_mate(score: chess.engine.Score) -> bool:
    """Whether ``score`` means the mover checkmates the opponent.

    True only for a positive mate distance (mover gives mate). A negative mate
    distance (mover gets mated) is not "delivering" mate and is left in the
    pool so Drawfish can still be forced into a losing line when nothing else is
    legal.
    """
    return score.is_mate() and (score.mate() or 0) > 0


def _distance_from_equality(candidate: Candidate) -> int:
    """Absolute centipawn distance from 0.00, with mates flattened to an extreme.

    Used to rank moves for Drawfish: the smaller the value the closer the move
    keeps the game to equality. Mate scores collapse to ``MATE_FLATTEN_SCORE`` so
    a won/lost position never looks "near equal".
    """
    return abs(candidate.score.score(mate_score=MATE_FLATTEN_SCORE))


def _flatten_score(candidate: Candidate) -> int:
    """Candidate score as a comparable centipawn int (mates flattened).

    The Worstfish objective: lower is "better" (worse for the mover), so a mate
    against the mover collapses to a large negative value and orders below any
    finite loss.
    """
    return candidate.score.score(mate_score=MATE_FLATTEN_SCORE)


def _select_from_ranked(
    ranked: Sequence[Candidate],
    objective: Callable[[Candidate], int],
    randomness: int,
    rng: Optional[random.Random],
) -> chess.Move:
    """Pick from ``ranked`` (best-first), shuffling only among near-best moves.

    ``ranked`` is already sorted best-first for the policy's objective, and
    ``objective`` maps a candidate to that objective's centipawn value (lower is
    better; ``ranked[0]`` therefore has the minimum). With ``randomness`` R > 0
    and an RNG present, the shuffle pool is the moves whose objective stays
    within :data:`RANDOMNESS_TOLERANCE_CP` of the best, then the R+1 closest of
    those. Bounding by centipawn distance -- not by rank alone -- is what stops a
    small or lopsided candidate set from shuffling a material blunder into play
    (see :data:`RANDOMNESS_TOLERANCE_CP`). R=0, no RNG, or a single candidate
    plays the single best move, preserving each engine's deterministic identity.

    Precondition: ``ranked`` is non-empty.
    """
    if randomness <= 0 or rng is None or len(ranked) <= 1:
        return ranked[0].move
    best_objective = objective(ranked[0])
    within_window = [
        c for c in ranked if objective(c) - best_objective <= RANDOMNESS_TOLERANCE_CP
    ]
    return rng.choice(within_window[: randomness + 1]).move


def select_worst_move(
    candidates: Sequence[Candidate], ctx: Optional[SelectionContext] = None
) -> chess.Move:
    """Worstfish: (near) the move Stockfish rates worst for the side to move.

    Candidates are ranked worst-first by score (mate-against-the-mover orders
    below any centipawn loss, so Worstfish walks into forced mate), ties broken
    by the lexicographically smallest UCI string for reproducibility.
    ``Randomness`` R>0 picks uniformly among the R+1 worst moves (via the
    injected RNG, and only those within :data:`RANDOMNESS_TOLERANCE_CP` of the
    single worst) so it is less predictable without letting a clearly non-terrible
    move slip in; R=0 (the default, or no RNG) always plays the single worst
    move, preserving Worstfish's identity. ``AvoidCaptures`` does not apply -- an
    engine trying to lose has no reason to avoid captures -- so it is not
    consulted.

    Precondition: ``candidates`` is non-empty (the caller only invokes a policy
    when there is at least one legal move).
    """
    randomness = 0
    rng: Optional[random.Random] = None
    if ctx is not None:
        randomness = ctx.options.get(OPTION_RANDOMNESS, 0)
        rng = ctx.rng

    ranked = sorted(candidates, key=lambda c: (c.score, c.move.uci()))
    return _select_from_ranked(ranked, _flatten_score, randomness, rng)


def select_drawfish_move(
    candidates: Sequence[Candidate], ctx: Optional[SelectionContext] = None
) -> chess.Move:
    """Drawfish: refuse to win -- never willingly checkmate, steer toward equality.

    Inspired by the behaviour of the chess.com "Zach" bot (refuse to win, never
    checkmate, avoid captures, shuffle toward equality), but backed by Stockfish
    so it actively holds a draw rather than playing weakly like Zach does.
    Selection proceeds in layers, each a "prefer, but fall back if forced":

    1. Exclude mate-delivering moves (Drawfish never willingly checkmates). If
       *every* legal move delivers mate -- the only way to force a win against
       Drawfish -- the exclusion is skipped so a move is still returned.
    2. If ``AvoidCaptures`` is enabled (off by default), exclude capturing moves
       so it shuffles rather than grabbing material. If every remaining move is a
       capture, captures are allowed back in rather than returning nothing. With
       it off (the default), captures compete on equality like any other move, so
       Drawfish will recapture to restore balance.
    3. Rank the survivors by closeness to equality (0.00). ``Randomness`` R>0
       picks uniformly among the R+1 most-equal moves (via the injected RNG) so
       it shuffles less predictably, but only among moves within
       :data:`RANDOMNESS_TOLERANCE_CP` of the most-equal move so a materially
       worse reply is never shuffled in; R=0 (or no RNG) plays the single
       most-equal move. Getting mated flattens to an extreme value, so it is
       never treated as "equal" and the game is not thrown.

    Ties in the equality ranking resolve to the lexicographically smallest UCI
    string, so the candidate pool is deterministic even before randomisation.

    Precondition: ``candidates`` is non-empty.
    """
    randomness = 0
    avoid_captures = False
    rng: Optional[random.Random] = None
    if ctx is not None:
        randomness = ctx.options.get(OPTION_RANDOMNESS, 0)
        avoid_captures = bool(ctx.options.get(OPTION_AVOID_CAPTURES, 0))
        rng = ctx.rng

    pool = [c for c in candidates if not _delivers_mate(c.score)] or list(candidates)
    if avoid_captures:
        pool = [c for c in pool if not c.is_capture] or pool

    ranked = sorted(pool, key=lambda c: (_distance_from_equality(c), c.move.uci()))
    return _select_from_ranked(ranked, _distance_from_equality, randomness, rng)
