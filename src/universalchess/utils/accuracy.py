"""Chess move-accuracy metrics derived from engine evaluations.

Pure, dependency-free functions that turn a sequence of engine evaluations
(always expressed in pawns from White's perspective) into:

- a win probability for a position,
- an accuracy percentage for a single move,
- per-colour game-accuracy averages, and
- a human-readable quality label ("Blunder", "Brilliant", ...) for the last
  move played.

The win-probability and per-move accuracy formulas follow the model Lichess
publishes for its accuracy metric. Keeping this module free of any board,
engine, or rendering imports means the maths is unit-testable in isolation and
reusable by any surface (the e-paper analysis widget is the first consumer).

Evaluation convention
---------------------
Every eval passed in is in pawns from *White's* perspective: positive favours
White, negative favours Black. A per-move ``mover_white`` flag selects whose
perspective a move is scored from, so a strong Black move (which drives the
White-perspective eval down) is credited to Black rather than penalised.
"""

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# Logistic win-probability model over centipawns and the accuracy curve fitted
# to it (Lichess' published constants). ``win_percent`` maps a centipawn eval to
# a 0..100 win probability; ``move_accuracy`` maps the win-probability the mover
# gave up to a 0..100 accuracy.
_WIN_LOGISTIC_K = -0.00368208
_ACCURACY_SCALE = 103.1668
_ACCURACY_DECAY = -0.04354
_ACCURACY_OFFSET = -3.1669

# Move-quality word thresholds, expressed as the mover's eval swing in pawns
# (positive = the position improved for the mover). These mirror the pawn-delta
# bands the annotation logic in AnalysisState has always used, but evaluated
# from the mover's perspective so the word agrees with the move's accuracy and
# the widget's mover-coloured quality bar.
_BLUNDER_SWING = -2.0
_MISTAKE_SWING = -1.0
_INACCURACY_SWING = -0.5
_GOOD_SWING = 0.5
_BRILLIANT_SWING = 2.0
# A big improving move only reads as "Brilliant" when the mover was clearly
# worse beforehand (a hard resource found while losing), matching the original
# "!!" rule; otherwise a large swing is just the opponent's error.
_BRILLIANT_LOSING_THRESHOLD = -1.0


@dataclass(frozen=True)
class AccuracySummary:
    """Accuracy readout for a game so far.

    Attributes:
        white: Mean accuracy% over White's moves, or None if White has not moved.
        black: Mean accuracy% over Black's moves, or None if Black has not moved.
        last_accuracy: Accuracy% of the most recent move, or None if no moves.
        last_mover_white: True if White made the last move, False if Black, or
            None if no moves have been played.
        last_word: Quality label for the last move ("Blunder", "Mistake",
            "Inaccuracy", "Good", "Brilliant"), or "" when the move is
            unremarkable or no move has been played.
    """

    white: Optional[float]
    black: Optional[float]
    last_accuracy: Optional[float]
    last_mover_white: Optional[bool]
    last_word: str


def win_percent(pawns_white_pov: float) -> float:
    """Return White's win probability (0..100) for a White-perspective eval.

    Args:
        pawns_white_pov: Evaluation in pawns, positive favouring White.

    Returns:
        Win probability as a percentage in [0, 100]. An even position (0.0)
        returns 50.0; large magnitudes saturate towards 100 or 0.
    """
    centipawns = pawns_white_pov * 100.0
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(_WIN_LOGISTIC_K * centipawns)) - 1.0)


def _mover_win_percent(pawns_white_pov: float, mover_white: bool) -> float:
    """Win probability (0..100) from the mover's own perspective."""
    white_win = win_percent(pawns_white_pov)
    return white_win if mover_white else 100.0 - white_win


def move_accuracy(win_before: float, win_after: float) -> float:
    """Accuracy% for a move given the mover's win% before and after it.

    Args:
        win_before: The mover's win probability before the move (0..100).
        win_after: The mover's win probability after the move (0..100).

    Returns:
        Accuracy in [0, 100]. A move that holds or improves the mover's win
        probability scores 100; larger drops score progressively lower.
    """
    if win_after >= win_before:
        return 100.0
    accuracy = (
        _ACCURACY_SCALE * math.exp(_ACCURACY_DECAY * (win_before - win_after))
        + _ACCURACY_OFFSET
    )
    return max(0.0, min(100.0, accuracy))


def classify_move(before_white_pov: float, after_white_pov: float, mover_white: bool) -> str:
    """Return a quality word for a move from the mover's perspective.

    Args:
        before_white_pov: White-perspective eval (pawns) before the move.
        after_white_pov: White-perspective eval (pawns) after the move.
        mover_white: True if White played the move, False if Black.

    Returns:
        One of "Blunder", "Mistake", "Inaccuracy", "Good", "Brilliant", or ""
        for an unremarkable move.
    """
    before = before_white_pov if mover_white else -before_white_pov
    after = after_white_pov if mover_white else -after_white_pov
    swing = after - before
    if swing <= _BLUNDER_SWING:
        return "Blunder"
    if swing <= _MISTAKE_SWING:
        return "Mistake"
    if swing <= _INACCURACY_SWING:
        return "Inaccuracy"
    if swing >= _BRILLIANT_SWING and before < _BRILLIANT_LOSING_THRESHOLD:
        return "Brilliant"
    if swing >= _GOOD_SWING:
        return "Good"
    return ""


def summarize(
    move_evals: Sequence[Tuple[float, bool]],
    start_eval: float = 0.0,
) -> AccuracySummary:
    """Summarise accuracy for a sequence of played moves.

    Args:
        move_evals: One ``(eval_white_pov, mover_white)`` tuple per played ply,
            in order. ``eval_white_pov`` is the White-perspective eval (pawns)
            of the position *after* that ply.
        start_eval: White-perspective eval (pawns) of the position before the
            first move. Defaults to 0.0 (an even starting position). For games
            begun from a custom position the true pre-game eval is unknown, so
            this default makes the first move's accuracy an approximation.

    Returns:
        An :class:`AccuracySummary`. Empty input yields all-None fields and an
        empty word.
    """
    white_accuracies: List[float] = []
    black_accuracies: List[float] = []
    last_accuracy: Optional[float] = None
    last_mover_white: Optional[bool] = None
    last_word = ""

    previous_eval = start_eval
    for eval_white_pov, mover_white in move_evals:
        win_before = _mover_win_percent(previous_eval, mover_white)
        win_after = _mover_win_percent(eval_white_pov, mover_white)
        accuracy = move_accuracy(win_before, win_after)
        (white_accuracies if mover_white else black_accuracies).append(accuracy)
        last_accuracy = accuracy
        last_mover_white = mover_white
        last_word = classify_move(previous_eval, eval_white_pov, mover_white)
        previous_eval = eval_white_pov

    return AccuracySummary(
        white=_mean(white_accuracies),
        black=_mean(black_accuracies),
        last_accuracy=last_accuracy,
        last_mover_white=last_mover_white,
        last_word=last_word,
    )


def _mean(values: List[float]) -> Optional[float]:
    """Arithmetic mean of ``values``, or None when empty."""
    if not values:
        return None
    return sum(values) / len(values)
