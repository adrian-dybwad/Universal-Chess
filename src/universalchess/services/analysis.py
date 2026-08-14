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
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, List, Optional, TYPE_CHECKING

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


# Sentinel centipawn magnitude used to represent a forced mate wherever a single
# integer must carry the evaluation (the database column, the broadcast payload).
# The web move table renders any |cp| >= this as "M", and the e-paper score does
# the same, so the two surfaces agree.
MATE_SCORE_CP = 10000


def annotate_positions_with_analysis(
    positions: List[dict],
    lookup: Callable[[str], Optional["PositionAnalysis"]],
) -> List[dict]:
    """Attach ``eval`` and ``best_move`` to each per-ply position entry.

    Pure: ``lookup`` supplies the analysis for a FEN, so the same function
    serves the live broadcast (looking up the in-memory cache) and any other
    consumer without either knowing where the results come from.

    A position with no analysis gets ``None`` for both fields rather than a
    zero. Null means "not analysed" and lets the chart draw a gap; 0 is a real
    evaluation meaning the position is dead equal, and the two must not be
    conflated.

    Entries are copied rather than mutated: callers pass lists whose dicts are
    shared with other consumers, and ``ChessGameState.history_positions()`` is
    specified to return exactly ``{fen, san, uci}``.
    """
    annotated = []
    for entry in positions:
        result = lookup(entry["fen"])
        annotated.append({
            **entry,
            "eval": result.eval_score_cp if result is not None else None,
            "best_move": result.best_move if result is not None else None,
        })
    return annotated


@dataclass(frozen=True)
class PositionAnalysis:
    """The engine's verdict on one position, addressed by its FEN.

    Kept separate from ``AnalysisState`` (which is display state for the
    currently shown position) because consumers ask about a *specific*
    position: the database backfills the row for the ply that was analysed, and
    the web draws an arrow for the ply the user is looking at. Answering those
    from "whatever finished most recently" is what produced the off-by-one in
    the persisted eval.

    ``score_cp`` and ``mate_in`` are mutually exclusive and both are from
    White's perspective. ``best_move`` is the first move of the principal
    variation in UCI, or None when the engine reported no PV.
    """

    fen: str
    score_cp: Optional[int]
    mate_in: Optional[int]
    best_move: Optional[str]

    @property
    def eval_score_cp(self) -> int:
        """The single integer stored in the database and put on the wire.

        Mate collapses to the +/-``MATE_SCORE_CP`` sentinel; a centipawn score
        is clamped to ``AnalysisService.SCORE_CLAMP_CP``, which is deliberately
        below that sentinel so a crushing-but-not-mating position can never be
        mistaken for forced mate.
        """
        if self.mate_in is not None:
            # Mate(0) means the side to move is mated, i.e. lost for White.
            return MATE_SCORE_CP if self.mate_in > 0 else -MATE_SCORE_CP
        clamp = AnalysisService.SCORE_CLAMP_CP
        return max(-clamp, min(clamp, self.score_cp))


def position_analysis_from_stored(
    fen: Optional[str],
    eval_cp: Optional[int],
    best_move: Optional[str],
) -> Optional[PositionAnalysis]:
    """Rebuild a PositionAnalysis from the integers stored on a move row.

    NULL eval_cp means the ply was never analysed. The +/-MATE_SCORE_CP
    sentinel is how mate is stored; the exact mate distance is not
    recoverable from it, so 1 stands for "mate".
    """
    if not fen or eval_cp is None:
        return None
    if eval_cp >= MATE_SCORE_CP:
        return PositionAnalysis(fen, None, 1, best_move)
    if eval_cp <= -MATE_SCORE_CP:
        return PositionAnalysis(fen, None, -1, best_move)
    return PositionAnalysis(fen, eval_cp, None, best_move)


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

    # Evaluations are stored (and shown on the e-paper score) clamped to this pawn
    # magnitude. Past it the exact figure conveys nothing beyond "clearly winning",
    # but the cap is kept high enough that lopsided-but-not-mating positions still
    # read as a real number rather than pegging early. Mirrors the web-app's
    # EVAL_DISPLAY_CAP_PAWNS so both surfaces agree.
    SCORE_CLAMP_PAWNS = 35.0

    # The same clamp in centipawns, for the persisted/broadcast integer.
    SCORE_CLAMP_CP = int(SCORE_CLAMP_PAWNS * 100)

    # Upper bound on the per-FEN result cache. Two entries per move comfortably
    # covers a full game plus review navigation, while keeping the memory
    # footprint negligible on a 415 MiB board.
    MAX_POSITION_RESULTS = 400

    def __init__(self):
        """Initialize the analysis service."""
        self._game_state: ChessGameState = get_chess_game()
        self._analysis_state: AnalysisState = get_analysis()

        # Completed results keyed by the analysed FEN, oldest first so the cache
        # can be trimmed by eviction. Guarded by its own lock: the worker thread
        # writes while consumers (hint, web request handlers) read.
        self._position_results: "OrderedDict[str, PositionAnalysis]" = OrderedDict()
        self._results_lock = threading.Lock()
        self._position_listeners: List[Callable[[PositionAnalysis], None]] = []
        
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

        # Drop per-position results too: a repeated position (a transposition,
        # or simply the start FEN) would otherwise answer the new game with the
        # previous game's evaluation and best move.
        with self._results_lock:
            self._position_results.clear()

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
        
        # Convert centipawns to pawns, clamped to +/-SCORE_CLAMP_PAWNS
        pawn_scores = []
        for cp in centipawn_scores:
            if cp is not None:
                pawn_score = cp / 100.0
                pawn_score = max(-self.SCORE_CLAMP_PAWNS, min(self.SCORE_CLAMP_PAWNS, pawn_score))
                pawn_scores.append(pawn_score)
        
        if pawn_scores:
            self._analysis_state.set_history(pawn_scores)
            log.info(f"[AnalysisService] Restored {len(pawn_scores)} scores from history")

        # Rebuild the per-ply accuracy record. Each restored score is the eval
        # after that ply (index 0 = ply 1), so the mover's colour alternates from
        # the root position's side to move. Unanalysed (None) plies are skipped
        # but their index still sets the colour of later plies, keeping the
        # colour assignment aligned to the real move sequence.
        first_ply_white = self._game_state.board.root().turn == chess.WHITE
        move_evals = []
        for ply_index, cp in enumerate(centipawn_scores):
            if cp is None:
                continue
            pawn_score = max(-self.SCORE_CLAMP_PAWNS, min(self.SCORE_CLAMP_PAWNS, cp / 100.0))
            mover_white = first_ply_white if ply_index % 2 == 0 else not first_ply_white
            move_evals.append((pawn_score, mover_white))
        if move_evals:
            self._analysis_state.set_move_evals(move_evals)
    
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
        self._queue_position(add_to_history=not is_first_move, is_new_ply=True)
    
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
        
        self._queue_position(add_to_history=add_to_history, is_new_ply=False)
    
    def analyze_position(self, board: chess.Board) -> None:
        """Queue an arbitrary position for evaluation, outside the live game.

        Used to gap-fill a stored game under review. The result is published
        through the normal per-position channel (cache plus listeners) but is
        deliberately kept out of the live game's display state: it neither
        extends the e-paper eval graph nor counts towards accuracy, both of
        which describe the game actually being played.

        Args:
            board: The position to evaluate. Copied, so the caller may reuse it.
        """
        try:
            request = (board.copy(), board.fen(), False, self._time_limit,
                       self._reset_generation, False)
            self._analysis_queue.put_nowait(request)
        except queue.Full:
            log.warning("[AnalysisService] Queue full, dropping on-demand analysis")

    def _queue_position(self, add_to_history: bool, is_new_ply: bool) -> None:
        """Enqueue the current game position for the worker to analyze.
        
        Args:
            add_to_history: Whether the resulting score should be appended to
                the history graph (see analyze_current_position).
            is_new_ply: True when this evaluation is for a freshly played
                half-move (so its result should be recorded for accuracy),
                False for a re-evaluation of the current position.
        """
        try:
            fen = self._game_state.fen
            # Copy through the state (not chess.Board(fen)) so the chess960 flag
            # is carried over; python-chess only emits UCI_Chess960 and applies
            # 960 castling when the analysed board has chess960 set.
            board_copy = self._game_state.board_copy()
            
            request = (board_copy, fen, add_to_history, self._time_limit,
                       self._reset_generation, is_new_ply)
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
                board_copy, fen, add_to_history, time_limit, request_generation, is_new_ply = request
                
                # Check if stale
                if request_generation != self._reset_generation:
                    log.debug(f"[AnalysisService] Discarding stale request")
                    self._analysis_queue.task_done()
                    continue
                
                # Run analysis (via handle for serialized access). Strength
                # limits are cleared per search: the handle is pooled per binary,
                # so a reduced-ELO player engine on the same path leaves
                # UCI_LimitStrength/Skill Level in force and the "objective"
                # evaluation would come from a deliberately weakened search.
                try:
                    info = self._engine_handle.analyse(
                        board_copy,
                        chess.engine.Limit(time=time_limit),
                        options=self._engine_handle.full_strength_options(),
                    )
                    
                    # Check again after analysis
                    if request_generation != self._reset_generation:
                        log.debug(f"[AnalysisService] Discarding stale result")
                        self._analysis_queue.task_done()
                        continue
                    
                    # The side that just moved is the opposite of the side to move
                    # in the analysed position. Only newly played plies feed the
                    # accuracy record; re-evaluations pass None.
                    mover_white = (not board_copy.turn) if is_new_ply else None
                    self._update_state_from_analysis(info, add_to_history, mover_white, fen)
                    
                except Exception as e:
                    log.warning(f"[AnalysisService] Analysis error: {e}")
                
                self._analysis_queue.task_done()
                
            except Exception as e:
                log.error(f"[AnalysisService] Worker error: {e}")
    
    # -------------------------------------------------------------------------
    # Per-position results
    # -------------------------------------------------------------------------

    def get_position_analysis(self, fen: str) -> Optional[PositionAnalysis]:
        """Return the recorded analysis for ``fen``, or None if not analysed.

        Absence is reported as None rather than a zero-valued result: a
        fabricated 0.0 is indistinguishable from a genuinely equal position and
        would be persisted and charted as a real evaluation.
        """
        with self._results_lock:
            return self._position_results.get(fen)

    def on_position_analysed(self, callback: Callable[[PositionAnalysis], None]) -> None:
        """Register a callback invoked when any position finishes analysis.

        Drives the three consumers that cannot poll: backfilling the persisted
        ``eval_score``/``best_move``, rebroadcasting game state to the web, and
        resolving a ``?`` hint pressed while the search was still in flight.
        """
        if callback not in self._position_listeners:
            self._position_listeners.append(callback)

    def remove_position_listener(self, callback: Callable[[PositionAnalysis], None]) -> None:
        """Detach a previously registered result callback."""
        if callback in self._position_listeners:
            self._position_listeners.remove(callback)

    def restore_position_results(self, stored: list) -> None:
        """Seed the per-position cache from evaluations persisted for a game.

        A resumed game has already been analysed once; without this the cache
        starts empty and the web chart would be blank until every ply was
        re-analysed, repeating work the board already stored.

        Args:
            stored: ``(fen, eval_score_cp, best_move)`` triples. A NULL
                ``eval_score_cp`` means the ply was never analysed and is
                skipped, so absence stays distinguishable from a real 0.
        """
        for fen, eval_cp, best_move in stored:
            result = position_analysis_from_stored(fen, eval_cp, best_move)
            if result is None:
                continue
            with self._results_lock:
                self._position_results.pop(fen, None)
                self._position_results[fen] = result
                while len(self._position_results) > self.MAX_POSITION_RESULTS:
                    self._position_results.popitem(last=False)

    def _build_position_analysis(self, fen: str,
                                 analysis_info: dict) -> Optional[PositionAnalysis]:
        """Convert a python-chess InfoDict into a PositionAnalysis.

        Scores are read through ``PovScore.white()`` rather than by slicing the
        repr of the score object, which is what the previous implementation did:
        fixed character offsets return a plausible but wrong number the moment
        that repr changes, so the eval would be silently incorrect.

        Returns None when the engine reported no score, so the caller records
        nothing at all instead of inventing a value.
        """
        pov_score = analysis_info.get("score")
        if pov_score is None:
            return None

        white_score = pov_score.white()
        pv = analysis_info.get("pv")
        # Engines omit the PV in some very short or terminal searches; that must
        # not discard the evaluation, only the arrow.
        best_move = pv[0].uci() if pv else None

        return PositionAnalysis(
            fen=fen,
            score_cp=white_score.score(),
            mate_in=white_score.mate(),
            best_move=best_move,
        )

    def _record_position_analysis(self, result: Optional[PositionAnalysis]) -> None:
        """Cache a completed result by FEN and notify listeners.

        Listener exceptions are contained: an analysis result feeds several
        independent consumers, and one failing (say, a web broadcast on a closed
        socket) must not prevent the others from running or abort the worker.
        """
        if result is None:
            return

        with self._results_lock:
            # Re-inserting moves the entry to the end, so a re-analysed position
            # is treated as freshly used rather than evicted as stale.
            self._position_results.pop(result.fen, None)
            self._position_results[result.fen] = result
            while len(self._position_results) > self.MAX_POSITION_RESULTS:
                self._position_results.popitem(last=False)

        for callback in list(self._position_listeners):
            try:
                callback(result)
            except Exception:
                log.exception("[AnalysisService] Position result listener failed")

    def _update_state_from_analysis(self, analysis_info: dict, add_to_history: bool,
                                    mover_white: Optional[bool] = None,
                                    fen: Optional[str] = None) -> None:
        """Update AnalysisState from an engine analysis result.

        Args:
            analysis_info: Raw analysis dict from chess engine.
            add_to_history: Whether to append the score to the history graph.
            mover_white: Colour of the side that just moved for a newly played
                half-move (recorded for accuracy), or None for a re-evaluation.
            fen: The analysed position, used to key the per-position record.
        """
        result = self._build_position_analysis(fen or self._game_state.fen, analysis_info)
        if result is None:
            return

        if result.mate_in is not None:
            self._analysis_state.set_mate_score(
                result.mate_in, add_to_history=add_to_history, mover_white=mover_white)
        else:
            pawns = result.score_cp / 100.0
            display_score = max(-self.SCORE_CLAMP_PAWNS, min(self.SCORE_CLAMP_PAWNS, pawns))
            self._analysis_state.set_score(
                display_score, add_to_history=add_to_history, mover_white=mover_white)

        self._record_position_analysis(result)


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
