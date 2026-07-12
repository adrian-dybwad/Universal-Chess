"""
Analysis service - manages position analysis in background.

Observes ChessGameState for position changes and runs analysis using a
chess engine. Updates AnalysisState with results, which widgets can observe.

This follows the pattern:
- Service observes ChessGameState.on_position_change
- Service runs analysis in background thread
- Service updates AnalysisState with results
- Widgets observe AnalysisState for display updates
"""

import queue
import threading
from typing import Optional, TYPE_CHECKING

import chess
import chess.engine

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from universalchess.state import get_chess_game
from universalchess.state.analysis import get_analysis, AnalysisState
from universalchess.state.chess_game import ChessGameState

if TYPE_CHECKING:
    from universalchess.services.engine_registry import EngineHandle


class AnalysisService:
    """Service that analyzes chess positions and updates AnalysisState.
    
    Subscribes to ChessGameState.on_position_change and runs analysis
    in a background worker thread. Results are written to AnalysisState,
    which widgets can observe.
    
    Thread model:
    - Position changes queued from game state observer
    - Worker thread processes queue sequentially
    - All positions analyzed to ensure complete history graph
    """
    
    DEFAULT_TIME_LIMIT = 0.3  # seconds per position
    
    def __init__(self):
        """Initialize the analysis service."""
        self._game_state: ChessGameState = get_chess_game()
        self._analysis_state: AnalysisState = get_analysis()
        
        # Analysis engine handle from registry
        self._engine_handle: Optional["EngineHandle"] = None
        
        # Analysis queue and worker
        self._analysis_queue: queue.Queue = queue.Queue(maxsize=50)
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # Reset generation - incremented on reset to discard stale results
        self._reset_generation = 0
        
        # Time limit for analysis
        self._time_limit = self.DEFAULT_TIME_LIMIT
        
        # Track if we're subscribed
        self._subscribed = False
    
    def start(self) -> None:
        """Start the analysis service.
        
        Subscribes to game state and starts worker thread.
        """
        if self._subscribed:
            return
        
        # Subscribe to position changes
        self._game_state.on_position_change(self._on_position_change)
        self._subscribed = True
        
        # Start worker thread
        self._start_worker()
        
        log.info("[AnalysisService] Started")
    
    def stop(self) -> None:
        """Stop the analysis service.
        
        Unsubscribes from game state and stops worker thread.
        """
        # Unsubscribe
        if self._subscribed:
            self._game_state.remove_observer(self._on_position_change)
            self._subscribed = False
        
        # Stop worker
        self._stop_worker()
        
        log.info("[AnalysisService] Stopped")
    
    def set_engine_handle(self, handle: Optional["EngineHandle"]) -> None:
        """Set the analysis engine handle from registry.
        
        Args:
            handle: EngineHandle from registry, or None to disable.
        """
        self._engine_handle = handle
        log.info(f"[AnalysisService] Engine handle set: {handle is not None}")
    
    def set_time_limit(self, seconds: float) -> None:
        """Set the time limit for analysis.
        
        Args:
            seconds: Time limit per position in seconds.
        """
        self._time_limit = seconds
    
    def reset(self) -> None:
        """Reset analysis state.
        
        Clears history and pending queue, increments generation to
        discard any in-flight analysis results.
        """
        self._reset_generation += 1
        
        # Clear the queue
        self._clear_queue()
        
        # Reset state
        self._analysis_state.reset()
        
        log.debug(f"[AnalysisService] Reset (generation {self._reset_generation})")
    
    def restore_history(self, centipawn_scores: list) -> None:
        """Restore score history from database values.
        
        Used when resuming a saved game to restore the full evaluation history.
        
        Args:
            centipawn_scores: List of scores in centipawns (integers).
        """
        if not centipawn_scores:
            return
        
        # Increment generation to discard any pending analysis
        self._reset_generation += 1
        self._clear_queue()
        
        # Convert centipawns to pawns (-12 to +12 clamped)
        pawn_scores = []
        for cp in centipawn_scores:
            if cp is not None:
                pawn_score = cp / 100.0
                pawn_score = max(-12.0, min(12.0, pawn_score))
                pawn_scores.append(pawn_score)
        
        if pawn_scores:
            self._analysis_state.set_history(pawn_scores)
            log.info(f"[AnalysisService] Restored {len(pawn_scores)} scores from history")
    
    def remove_last_score(self) -> None:
        """Remove the last score from history.
        
        Called on takeback to keep analysis in sync with game state.
        """
        self._analysis_state.remove_last()
        log.debug("[AnalysisService] Removed last score (takeback)")
    
    def _clear_queue(self) -> None:
        """Clear pending analysis requests from the queue."""
        try:
            while not self._analysis_queue.empty():
                try:
                    self._analysis_queue.get_nowait()
                    self._analysis_queue.task_done()
                except queue.Empty:
                    break
        except Exception:  # noqa: S110  # nosec B110 - best-effort queue drain; nothing to recover if get/task_done races during reset
            pass
        
        # Reset state
        self._analysis_state.reset()
        
        log.debug(f"[AnalysisService] Reset (generation {self._reset_generation})")
    
    def _on_position_change(self) -> None:
        """Handle position change from game state.
        
        Queues the position for analysis if game is in progress. The first move
        is shown but not graphed (the opening evaluation is the graph baseline,
        not a data point), so it is queued with add_to_history=False.
        """
        # Only analyze if game has started
        if not self._game_state.is_game_in_progress:
            return
        
        is_first_move = len(self._game_state.move_stack) == 1
        self._queue_position(add_to_history=not is_first_move)
    
    def analyze_current_position(self, add_to_history: bool = True) -> None:
        """Queue the current position for a fresh evaluation on demand.
        
        Used when a position is reached outside normal play and the board must
        still show a running evaluation - e.g. resuming a finished game for
        review, mirroring the web client which analyzes any position regardless
        of game-over state. No position change is emitted in that flow, so the
        evaluation must be requested explicitly.
        
        Args:
            add_to_history: When False, only the displayed score is refreshed;
                the history graph is left unchanged. Pass False when the current
                position's score is already present in the restored history so
                the graph is not extended with a duplicate trailing point.
        """
        # Nothing to evaluate at the standard start; matches _on_position_change.
        if not self._game_state.is_game_in_progress:
            return
        
        self._queue_position(add_to_history=add_to_history)
    
    def _queue_position(self, add_to_history: bool) -> None:
        """Enqueue the current game position for the worker to analyze.
        
        Args:
            add_to_history: Whether the resulting score should be appended to
                the history graph (see analyze_current_position).
        """
        try:
            fen = self._game_state.fen
            # Copy through the state (not chess.Board(fen)) so the chess960 flag
            # is carried over; python-chess only emits UCI_Chess960 and applies
            # 960 castling when the analysed board has chess960 set.
            board_copy = self._game_state.board_copy()
            
            request = (board_copy, fen, add_to_history, self._time_limit, self._reset_generation)
            self._analysis_queue.put_nowait(request)
            
        except queue.Full:
            log.warning("[AnalysisService] Queue full, dropping analysis request")
        except Exception as e:
            log.warning(f"[AnalysisService] Error queuing analysis: {e}")
    
    def _start_worker(self) -> None:
        """Start the worker thread."""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="analysis-service-worker",
            daemon=True
        )
        self._worker_thread.start()
    
    def _stop_worker(self) -> None:
        """Stop the worker thread."""
        if self._worker_thread is not None:
            self._stop_event.set()
            try:
                self._worker_thread.join(timeout=2.0)
            except Exception:  # noqa: S110  # nosec B110 - join is best-effort on shutdown; the worker is a daemon thread and will be abandoned
                pass
            self._worker_thread = None
    
    def _worker_loop(self) -> None:
        """Worker thread that processes analysis requests."""
        while not self._stop_event.is_set():
            try:
                # Get next request
                try:
                    request = self._analysis_queue.get(timeout=0.1)
                except queue.Empty:  # noqa: S112 - poll timeout; loop again to re-check the stop event
                    continue
                
                # Wait for engine
                if self._engine_handle is None:
                    try:
                        self._analysis_queue.put_nowait(request)
                    except queue.Full:  # noqa: S110  # nosec B110 - requeue is best-effort while waiting for the engine; request is re-fetched next loop
                        pass
                    self._analysis_queue.task_done()
                    import time
                    time.sleep(0.2)
                    continue
                
                # Unpack request
                board_copy, fen, add_to_history, time_limit, request_generation = request
                
                # Check if stale
                if request_generation != self._reset_generation:
                    log.debug(f"[AnalysisService] Discarding stale request")
                    self._analysis_queue.task_done()
                    continue
                
                # Run analysis (via handle for serialized access)
                try:
                    info = self._engine_handle.analyse(board_copy, chess.engine.Limit(time=time_limit))
                    
                    # Check again after analysis
                    if request_generation != self._reset_generation:
                        log.debug(f"[AnalysisService] Discarding stale result")
                        self._analysis_queue.task_done()
                        continue
                    
                    # Update state
                    self._update_state_from_analysis(info, add_to_history)
                    
                except Exception as e:
                    log.warning(f"[AnalysisService] Analysis error: {e}")
                
                self._analysis_queue.task_done()
                
            except Exception as e:
                log.error(f"[AnalysisService] Worker error: {e}")
    
    def _update_state_from_analysis(self, analysis_info: dict, add_to_history: bool) -> None:
        """Update AnalysisState from engine analysis result.
        
        Args:
            analysis_info: Raw analysis dict from chess engine.
            add_to_history: Whether to append the score to the history graph.
        """
        if "score" not in analysis_info:
            return
        
        score_str = str(analysis_info["score"])
        
        # Parse score
        if "Mate" in score_str:
            # Extract mate value
            mate_str = score_str[13:24]
            mate_str = mate_str[1:mate_str.find(")")]
            mate_value = int(float(mate_str))
            
            # Negate if black is winning
            if "BLACK" in score_str:
                mate_value = -mate_value
            
            self._analysis_state.set_mate_score(mate_value, add_to_history=add_to_history)
        else:
            # Extract centipawn value
            cp_str = score_str[11:24]
            cp_str = cp_str[1:cp_str.find(")")]
            score_value = float(cp_str) / 100.0
            
            # Negate if black is winning
            if "BLACK" in score_str:
                score_value = -score_value
            
            # Clamp for display
            display_score = max(-12.0, min(12.0, score_value))
            
            self._analysis_state.set_score(display_score, add_to_history=add_to_history)


# -----------------------------------------------------------------------------
# Singleton instance
# -----------------------------------------------------------------------------

_instance: Optional[AnalysisService] = None


def get_analysis_service() -> AnalysisService:
    """Get the singleton AnalysisService instance.
    
    Returns:
        The global AnalysisService instance.
    """
    global _instance
    if _instance is None:
        _instance = AnalysisService()
    return _instance


def reset_analysis_service() -> AnalysisService:
    """Reset the singleton to a fresh instance.
    
    Stops the current service if running.
    
    Returns:
        The new AnalysisService instance.
    """
    global _instance
    if _instance is not None:
        _instance.stop()
    _instance = AnalysisService()
    return _instance
