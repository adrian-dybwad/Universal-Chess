"""
Analysis state - evaluation scores and history.

Holds the current position evaluation and score history for display.
The actual analysis engine and worker thread are managed elsewhere.

Widgets observe this state to display evaluation bar and history graph.
"""

import logging
from typing import Optional, Callable, List, Tuple

from universalchess.utils.accuracy import AccuracySummary, summarize

log = logging.getLogger(__name__)


class AnalysisState:
    """Observable analysis state.
    
    Holds:
    - Current evaluation score
    - Score history for graphing
    - Move annotations (!, ??, etc.)
    
    Observers are notified when evaluation or history changes.
    """
    
    # Maximum history size to prevent unbounded growth
    MAX_HISTORY_SIZE = 200
    
    def __init__(self):
        """Initialize with no analysis data."""
        # Current evaluation
        self._score: float = 0.0  # In pawns, positive = white advantage
        self._score_text: str = "0.0"  # Formatted score string
        self._is_mate: bool = False  # Whether score is a mate score
        self._mate_in: Optional[int] = None  # Moves to mate, or None
        
        # Move annotation for current position
        self._annotation: str = ""  # !, !!, ?, ??, !?, ?!
        
        # Score history for graphing
        self._history: List[float] = []

        # Per-ply record for accuracy: one (eval_white_pov_pawns, mover_white)
        # tuple per played half-move, in order. Distinct from ``_history``, which
        # is display-only and omits the first move (its opening eval is the graph
        # baseline). This list stays complete -- including the first move -- so
        # per-colour accuracy is not missing a ply.
        self._move_evals: List[Tuple[float, bool]] = []
        
        # Previous score for annotation calculation
        self._previous_score: float = 0.0
        
        # Observer callbacks
        self._on_score_change: List[Callable[[], None]] = []
        self._on_history_change: List[Callable[[], None]] = []
    
    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------
    
    @property
    def score(self) -> float:
        """Current evaluation in pawns (positive = white advantage)."""
        return self._score
    
    @property
    def score_text(self) -> str:
        """Formatted score string (e.g., '+1.5', '-0.3', 'M5')."""
        return self._score_text
    
    @property
    def is_mate(self) -> bool:
        """Whether the current score is a forced mate."""
        return self._is_mate
    
    @property
    def mate_in(self) -> Optional[int]:
        """Moves to mate, or None if not a mate score."""
        return self._mate_in
    
    @property
    def annotation(self) -> str:
        """Current move annotation (!, !!, ?, ??, !?, ?!)."""
        return self._annotation
    
    @property
    def history(self) -> List[float]:
        """Score history for graphing. Returns a copy."""
        return list(self._history)
    
    @property
    def history_length(self) -> int:
        """Number of positions in history."""
        return len(self._history)

    def accuracy_summary(self) -> AccuracySummary:
        """Per-colour accuracy and last-move quality for the game so far.

        Derived on demand from the complete per-ply eval record, so it always
        reflects the current move list (including the first move, which the
        graph history omits).
        """
        return summarize(self._move_evals)
    
    # -------------------------------------------------------------------------
    # Observer management
    # -------------------------------------------------------------------------
    
    def on_score_change(self, callback: Callable[[], None]) -> None:
        """Register callback for score changes.
        
        Args:
            callback: Function called when score or annotation changes.
        """
        if callback not in self._on_score_change:
            self._on_score_change.append(callback)
    
    def on_history_change(self, callback: Callable[[], None]) -> None:
        """Register callback for history changes.
        
        Args:
            callback: Function called when history is updated.
        """
        if callback not in self._on_history_change:
            self._on_history_change.append(callback)
    
    def remove_observer(self, callback: Callable) -> None:
        """Remove a previously registered callback.
        
        Args:
            callback: The callback to remove (from any observer list).
        """
        if callback in self._on_score_change:
            self._on_score_change.remove(callback)
        if callback in self._on_history_change:
            self._on_history_change.remove(callback)
    
    def _notify_score(self) -> None:
        """Notify score change observers."""
        for callback in self._on_score_change:
            try:
                callback()
            except Exception:
                log.exception("Score observer callback failed")
    
    def _notify_history(self) -> None:
        """Notify history change observers."""
        for callback in self._on_history_change:
            try:
                callback()
            except Exception:
                log.exception("History observer callback failed")
    
    # -------------------------------------------------------------------------
    # State mutations
    # -------------------------------------------------------------------------
    
    def set_score(self, score: float, add_to_history: bool = True,
                  mover_white: Optional[bool] = None) -> None:
        """Set the current evaluation score.
        
        Args:
            score: Evaluation in pawns (positive = white advantage).
            add_to_history: If True, append to the display history graph.
            mover_white: Colour of the side that just moved when this score is
                the result of a newly played half-move (True=white, False=black),
                or None when it is a re-evaluation of the current position rather
                than a new move. Only a non-None value appends to the accuracy
                record, so re-analysing the same position never double-counts a
                move.
        """
        self._previous_score = self._score
        self._score = score
        self._is_mate = False
        self._mate_in = None
        
        # Format score text
        if score >= 0:
            self._score_text = f"+{score:.1f}"
        else:
            self._score_text = f"{score:.1f}"
        
        # Calculate annotation based on score change
        self._calculate_annotation()
        
        self._notify_score()

        if mover_white is not None:
            self._move_evals.append((score, mover_white))
        
        if add_to_history:
            self._add_to_history(score)
    
    def set_mate_score(self, mate_in: int, add_to_history: bool = True,
                       mover_white: Optional[bool] = None) -> None:
        """Set a forced mate score.
        
        Args:
            mate_in: Moves to mate (positive = white mates, negative = black mates).
            add_to_history: If True, append to the display history graph.
            mover_white: Colour of the side that just moved for a newly played
                half-move, or None for a re-evaluation (see :meth:`set_score`).
        """
        self._previous_score = self._score
        self._is_mate = True
        self._mate_in = mate_in
        
        # Use large score value for history
        if mate_in > 0:
            self._score = 100.0  # White winning
            self._score_text = f"M{mate_in}"
        else:
            self._score = -100.0  # Black winning
            self._score_text = f"M{abs(mate_in)}"
        
        # Mate is always significant
        self._annotation = "!!" if abs(mate_in) <= 5 else "!"
        
        self._notify_score()

        if mover_white is not None:
            self._move_evals.append((self._score, mover_white))
        
        if add_to_history:
            self._add_to_history(self._score)
    
    def set_annotation(self, annotation: str) -> None:
        """Override the annotation.
        
        Args:
            annotation: Annotation symbol (!, !!, ?, ??, !?, ?!).
        """
        self._annotation = annotation
        self._notify_score()
    
    def _calculate_annotation(self) -> None:
        """Calculate move annotation based on score change.
        
        Annotation rules:
        - !! (brilliant): improves by 2+ pawns when losing
        - !  (good): improves by 0.5-2 pawns
        - !? (interesting): roughly equal trade-off
        - ?! (dubious): worsens by 0.5-1 pawn
        - ?  (mistake): worsens by 1-2 pawns
        - ?? (blunder): worsens by 2+ pawns
        """
        delta = self._score - self._previous_score
        
        # No annotation for first move or small changes
        if abs(delta) < 0.3:
            self._annotation = ""
        elif delta >= 2.0 and self._previous_score < -1.0:
            self._annotation = "!!"  # Brilliant - big improvement when losing
        elif delta >= 0.5:
            self._annotation = "!"  # Good move
        elif delta <= -2.0:
            self._annotation = "??"  # Blunder
        elif delta <= -1.0:
            self._annotation = "?"  # Mistake
        elif delta <= -0.5:
            self._annotation = "?!"  # Dubious
        else:
            self._annotation = ""
    
    def _add_to_history(self, score: float) -> None:
        """Add score to history, respecting max size.
        
        Args:
            score: Score to add.
        """
        self._history.append(score)
        
        # Trim if exceeds max
        if len(self._history) > self.MAX_HISTORY_SIZE:
            self._history = self._history[-self.MAX_HISTORY_SIZE:]
        
        self._notify_history()
    
    def clear_history(self) -> None:
        """Clear the score history."""
        self._history.clear()
        self._previous_score = 0.0
        self._notify_history()
    
    def set_history(self, scores: List[float]) -> None:
        """Set the score history directly.
        
        Used for restoring history from database on game resume.
        
        Args:
            scores: List of scores in pawns.
        """
        self._history = list(scores)
        if scores:
            self._score = scores[-1]
            self._previous_score = scores[-1]
            # Format score text
            if self._score >= 0:
                self._score_text = f"+{self._score:.1f}"
            else:
                self._score_text = f"{self._score:.1f}"
        else:
            self._score = 0.0
            self._previous_score = 0.0
            self._score_text = "0.0"
        self._annotation = ""
        self._is_mate = False
        self._mate_in = None
        self._notify_score()
        self._notify_history()
    
    def set_move_evals(self, move_evals: List[Tuple[float, bool]]) -> None:
        """Replace the per-ply accuracy record.

        Used on resume to rebuild the complete per-ply ``(eval_white_pov,
        mover_white)`` list from persisted data, so per-colour accuracy is
        available immediately for a restored game.

        Args:
            move_evals: One ``(eval_white_pov_pawns, mover_white)`` tuple per
                played half-move, in order.
        """
        self._move_evals = list(move_evals)
        self._notify_score()

    def remove_last(self) -> None:
        """Remove the last score from history.
        
        Used for takeback to keep analysis in sync with game state. Pops both
        the display history and the accuracy record so a taken-back move no
        longer counts towards either the graph or the accuracy figures.
        """
        if self._move_evals:
            self._move_evals.pop()
        if self._history:
            self._history.pop()
            if self._history:
                self._score = self._history[-1]
                self._previous_score = self._history[-1] if len(self._history) > 1 else 0.0
                if self._score >= 0:
                    self._score_text = f"+{self._score:.1f}"
                else:
                    self._score_text = f"{self._score:.1f}"
            else:
                self._score = 0.0
                self._previous_score = 0.0
                self._score_text = "0.0"
            self._annotation = ""
            self._notify_score()
            self._notify_history()
    
    def reset(self) -> None:
        """Reset all analysis state."""
        self._score = 0.0
        self._score_text = "0.0"
        self._is_mate = False
        self._mate_in = None
        self._annotation = ""
        self._history.clear()
        self._move_evals.clear()
        self._previous_score = 0.0
        self._notify_score()
        self._notify_history()


# -----------------------------------------------------------------------------
# Singleton instance
# -----------------------------------------------------------------------------

_instance: Optional[AnalysisState] = None


def get_analysis() -> AnalysisState:
    """Get the singleton AnalysisState instance.
    
    Returns:
        The global AnalysisState instance.
    """
    global _instance
    if _instance is None:
        _instance = AnalysisState()
    return _instance


def reset_analysis() -> AnalysisState:
    """Reset the singleton to a fresh instance.
    
    Primarily for testing.
    
    Returns:
        The new AnalysisState instance.
    """
    global _instance
    _instance = AnalysisState()
    return _instance
