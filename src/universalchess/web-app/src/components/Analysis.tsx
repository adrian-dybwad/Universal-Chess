import { useEffect, useState, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Line } from 'react-chartjs-2';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from 'chart.js';
import { getStockfishService } from '../services/stockfish';
import type { PositionEntry } from '../types/game';
import './Analysis.css';

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend, Filler);

interface AnalysisProps {
  /**
   * Authoritative per-ply positions (python-chess computed), start first. This
   * is the single source of the move list for both variants: the web builds and
   * navigates history from these server-computed FENs/SANs and no longer uses
   * chess.js (which mis-computes Chess960 castling). Null/empty means no game.
   */
  positions?: PositionEntry[] | null;
  mode: 'live' | 'static';
  onPositionChange?: (fen: string, moveIndex: number, totalMoves: number) => void;
  onBestMoveChange?: (bestMove: { from: string; to: string } | null) => void;
  /** Called with the actual move played (next in history), or null if at end/start */
  onPlayedMoveChange?: (playedMove: { from: string; to: string } | null) => void;
  /** Called with current move index and eval history when they change */
  onMoveDataChange?: (moveIndex: number, evalHistory: (number | null)[]) => void;
  /** Ref to expose goToMove function for external navigation */
  goToMoveRef?: React.MutableRefObject<((index: number) => void) | null>;
  /** Whether to show the best move for the latest position (live mode only) */
  showBestMoveForLatest?: boolean;
  /** Callback to toggle showBestMoveForLatest (live mode only) */
  onToggleShowBestMove?: () => void;
}

interface MoveData {
  fen: string;
  san: string;
  eval: number | null;  // centipawns, null if not analyzed
  mate: number | null;  // mate in N, null if not mate
  uci: string | null;   // UCI notation of the move (e.g., "e2e4"), null for start position
}

// Pawn magnitude past which the headline eval number is capped for display.
// Beyond this the exact centipawn figure no longer conveys anything -- the side
// is simply winning -- so it is shown as ">+35.0"/"<-35.0" instead of a large
// literal like "+60.7". Only the display is capped; the stored centipawn score
// drives the chart/bar unchanged. Mirrors the device's SCORE_CLAMP_PAWNS so the
// browser and e-paper score agree.
const EVAL_DISPLAY_CAP_PAWNS = 35;

/**
 * Analysis component matching the original Flask template design.
 * Features: eval score, best move, horizontal eval bar, chart, navigation.
 */
/**
 * Parse UCI move string (e.g., "e2e4") into from/to squares.
 */
function parseUciMove(uci: string | null): { from: string; to: string } | null {
  if (!uci || uci.length < 4) return null;
  const from = uci.substring(0, 2);
  const to = uci.substring(2, 4);
  if (!/^[a-h][1-8]$/.test(from) || !/^[a-h][1-8]$/.test(to)) return null;
  return { from, to };
}

export function Analysis({ positions, mode, onPositionChange, onBestMoveChange, onPlayedMoveChange, onMoveDataChange, goToMoveRef, showBestMoveForLatest, onToggleShowBestMove }: AnalysisProps) {
  const { t } = useTranslation();
  const [moves, setMoves] = useState<MoveData[]>([]);
  const [movePos, setMovePos] = useState(0);  // 0 = start, 1 = after first move, etc.
  const [currentEval, setCurrentEval] = useState<{ cp: number; mate: number | null }>({ cp: 0, mate: null });
  const [bestMove, setBestMove] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [newMovesToast, setNewMovesToast] = useState(0);
  const [sfReady, setSfReady] = useState(false);
  
  const chartRef = useRef<ChartJS<'line'> | null>(null);
  const lastSignatureRef = useRef('');
  const queueRef = useRef<number[]>([]);
  const processingRef = useRef(false);
  // Counter to track the latest analysis request - used to ignore stale results
  const analysisRequestIdRef = useRef(0);

  // Total moves in the game
  const totalMoves = moves.length > 0 ? moves.length - 1 : 0;  // moves[0] is start position

  // Initialize Stockfish, retrying with exponential backoff so a failed or
  // slow worker load recovers on its own. Without a retry a single init failure
  // (e.g. a cold-cache WASM compile exceeding the load timeout) would leave
  // sfReady stuck false and analysis permanently disabled even though the worker
  // becomes usable moments later. The service tears down its failed worker on
  // rejection, so each retry starts a fresh worker.
  useEffect(() => {
    const sf = getStockfishService();
    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;

    const tryInit = () => {
      sf.init()
        .then(() => {
          if (cancelled) return;
          console.log('[Analysis] Stockfish ready');
          setSfReady(true);
        })
        .catch((e) => {
          if (cancelled) return;
          attempt += 1;
          // 1s, 2s, 4s, ... capped at 30s so a persistent failure keeps
          // retrying without hammering.
          const delayMs = Math.min(1000 * 2 ** (attempt - 1), 30000);
          console.error(
            `[Analysis] Failed to initialize Stockfish (attempt ${attempt}), retrying in ${delayMs}ms:`,
            e,
          );
          retryTimer = setTimeout(tryInit, delayMs);
        });
    };

    tryInit();

    return () => {
      cancelled = true;
      if (retryTimer !== undefined) clearTimeout(retryTimer);
      sf.stop();
    };
  }, []);

  // Build move history from the authoritative per-ply positions (python-chess
  // computed on the server), preserving evals for unchanged positions. This is
  // the single source for both variants; the web no longer replays the PGN with
  // chess.js. A signature (the ordered FENs) gates rebuilds so frequent live
  // broadcasts that don't change the move list (e.g. pending-move updates) do
  // not churn state or discard in-flight analysis.
  useEffect(() => {
    const entries = Array.isArray(positions) ? positions : [];
    const signature = entries.map((p) => p.fen).join('|');
    if (signature === lastSignatureRef.current) return;
    lastSignatureRef.current = signature;

    const preserveEvalAt = (index: number, fen: string): { eval: number | null; mate: number | null } => {
      // Keep a previously computed eval when the position at this index is
      // unchanged, so rebuilding after a new move doesn't re-analyze the whole
      // game. Matching on FEN distinguishes a genuine position change from a
      // no-op rebuild.
      const existingMove = moves[index];
      const same = existingMove?.fen === fen;
      return {
        eval: same ? existingMove.eval : null,
        mate: same ? existingMove.mate : null,
      };
    };

    // The first entry is the start; later entries carry the post-move FEN/SAN/UCI.
    const newMoves: MoveData[] = entries.map((p, i) => {
      const preserved =
        i === 0
          ? { eval: moves[0]?.eval ?? null, mate: moves[0]?.mate ?? null }
          : preserveEvalAt(i, p.fen);
      return {
        fen: p.fen,
        san: i === 0 ? t('analysis.start') : (p.san ?? p.uci ?? ''),
        eval: preserved.eval,
        mate: preserved.mate,
        uci: p.uci,
      };
    });

    const prevLength = moves.length;
    setMoves(newMoves);

    // Build analysis queue - only queue positions that haven't been analyzed yet
    const newQueue: number[] = [];
    for (let i = 1; i < newMoves.length; i++) {
      if (newMoves[i].eval === null) {
        newQueue.push(i);
      }
    }
    
    // Merge with existing queue (avoid duplicates)
    const existingQueue = new Set(queueRef.current);
    for (const idx of newQueue) {
      if (!existingQueue.has(idx)) {
        queueRef.current.push(idx);
      }
    }

    // Handle position based on mode
    if (mode === 'live') {
      if (movePos > 0 && movePos < prevLength - 1 && newMoves.length > prevLength) {
        setNewMovesToast(newMoves.length - 1 - movePos);
      } else {
        setMovePos(newMoves.length - 1);
      }
    } else if (mode === 'static' && movePos === 0) {
      setMovePos(newMoves.length - 1);
    }
  }, [positions, mode, t]);

  // Store moves in a ref so processNext always has current data
  const movesRef = useRef<MoveData[]>([]);
  useEffect(() => {
    movesRef.current = moves;
  }, [moves]);

  // Process queue when Stockfish is ready and we have moves
  useEffect(() => {
    if (!sfReady || queueRef.current.length === 0 || processingRef.current) return;
    if (movesRef.current.length === 0) return;  // Wait for moves to be populated

    const processNext = async () => {
      if (queueRef.current.length === 0) {
        processingRef.current = false;
        setAnalyzing(false);
        return;
      }

      processingRef.current = true;
      setAnalyzing(true);

      const index = queueRef.current.shift()!;
      
      // Get move data from ref (always current)
      const move = movesRef.current[index];
      if (!move) {
        console.log(`[Analysis] Move ${index} not found, skipping`);
        setTimeout(processNext, 0);
        return;
      }

      if (move.eval !== null) {
        // Already analyzed, skip
        setTimeout(processNext, 0);
        return;
      }

      // Capture the FEN before async operation
      const fenToAnalyze = move.fen;
      const isBlackToMove = fenToAnalyze.includes(' b ');

      // Analyze this position
      const sf = getStockfishService();
      sf.analyze(fenToAnalyze, 10)
        .then((result) => {
          // Stockfish returns score from side-to-move's perspective.
          // We want all scores from White's perspective for consistent chart.
          let cp = result.score ?? 0;
          let mate = result.mate;
          
          if (isBlackToMove) {
            cp = -cp;
            if (mate !== null) {
              mate = -mate;
            }
          }
          
          // For chart: use large values for mate, otherwise centipawns
          const evalValue = mate !== null ? (mate > 0 ? 10000 : -10000) : cp;

          setMoves((prev) => {
            const updated = [...prev];
            if (updated[index]) {
              updated[index] = {
                ...updated[index],
                eval: evalValue,
                mate: mate,
              };
            }
            return updated;
          });

          // Process next in queue
          setTimeout(processNext, 0);
        })
        .catch((e) => {
          console.error('[Analysis] Analysis failed for move', index, e);
          setTimeout(processNext, 0);
        });
    };

    processNext();
  }, [sfReady, moves.length]);

  // FEN of the position currently being viewed. Used as the analysis key below.
  const currentPositionFen = moves[movePos]?.fen ?? null;

  // Analyze the current position at higher depth for eval + best move.
  //
  // Key this effect on the position FEN (a value), NOT the whole `moves` array.
  // Background queue analysis mutates `moves` (a new array per analyzed ply), and
  // if this effect re-ran on those updates it would bump analysisRequestId and
  // discard its own in-flight result. While background analysis streams in, the
  // best move would then never resolve and the green best-move arrow would never
  // appear. Keying on the FEN re-runs only on a genuine position change.
  useEffect(() => {
    if (!sfReady || movePos < 0 || !currentPositionFen) return;

    const fenToAnalyze = currentPositionFen;
    
    // Reset the best move for the new position before analyzing. Without this,
    // bestMove keeps the previous position's value until the new analysis
    // resolves; if the new best move is the same UCI string, the state never
    // changes and the onBestMoveChange notify effect never fires. The parent
    // (LiveBoard) clears its own arrow copy on every position change, so it
    // would be left with no best move and the green arrow would not appear.
    // Resetting here guarantees a null -> value transition that always re-notifies.
    setBestMove(null);
    
    // Increment request ID to track this specific request
    // This ensures we only use results from the latest analysis, not stale ones
    analysisRequestIdRef.current += 1;
    const thisRequestId = analysisRequestIdRef.current;

    // Priority: this is the position the user is viewing, so surface its eval
    // and best move ahead of the background chart-fill backlog.
    const sf = getStockfishService();
    sf.analyze(fenToAnalyze, 16, true)
      .then((result) => {
        // Ignore stale results - only update if this is still the latest request
        if (analysisRequestIdRef.current !== thisRequestId) {
          return;
        }
        
        // Normalize to White's perspective
        const isBlackToMove = fenToAnalyze.includes(' b ');
        
        let cp = result.score ?? 0;
        let mate = result.mate;
        
        if (isBlackToMove) {
          cp = -cp;
          if (mate !== null) {
            mate = -mate;
          }
        }
        
        setCurrentEval({
          cp: mate !== null ? (mate > 0 ? 10000 : -10000) : cp,
          mate: mate,
        });
        setBestMove(result.bestMove);
      })
      .catch(() => {
        // Keep previous eval
      });
  }, [sfReady, movePos, currentPositionFen]);

  // Notify parent of position change
  useEffect(() => {
    if (onPositionChange && movePos >= 0 && moves[movePos]) {
      onPositionChange(moves[movePos].fen.split(' ')[0], movePos, totalMoves);
    }
  }, [movePos, moves, totalMoves, onPositionChange]);

  // Notify parent of best move change
  useEffect(() => {
    if (onBestMoveChange) {
      onBestMoveChange(parseUciMove(bestMove));
    }
  }, [bestMove, onBestMoveChange]);

  // Notify parent of played move (the next move in history, if any)
  useEffect(() => {
    if (onPlayedMoveChange) {
      // If we're not at the end of the game, the next move is the "played move"
      const nextMove = moves[movePos + 1];
      if (nextMove && nextMove.uci) {
        onPlayedMoveChange(parseUciMove(nextMove.uci));
      } else {
        onPlayedMoveChange(null);
      }
    }
  }, [movePos, moves, onPlayedMoveChange]);

  // Notify parent of move data changes (for move table)
  useEffect(() => {
    if (onMoveDataChange) {
      // Build eval history array (index 0 unused, index 1 = first move, etc.)
      const evalHistory: (number | null)[] = [null]; // index 0 is start position
      for (let i = 1; i < moves.length; i++) {
        evalHistory.push(moves[i].eval);
      }
      onMoveDataChange(movePos, evalHistory);
    }
  }, [movePos, moves, onMoveDataChange]);

  // Expose goToMove via ref for external navigation (e.g., from MoveTable)
  useEffect(() => {
    if (goToMoveRef) {
      goToMoveRef.current = (index: number) => {
        if (index >= 0 && index <= totalMoves) {
          setMovePos(index);
          setNewMovesToast(0);
        }
      };
    }
    return () => {
      if (goToMoveRef) {
        goToMoveRef.current = null;
      }
    };
  }, [goToMoveRef, totalMoves]);

  // Navigation
  const goFirst = () => setMovePos(0);
  const goPrev = () => setMovePos((p) => Math.max(0, p - 1));
  const goNext = () => setMovePos((p) => Math.min(totalMoves, p + 1));
  const goLast = () => {
    setMovePos(totalMoves);
    setNewMovesToast(0);
  };
  const jumpToLatest = () => {
    setMovePos(totalMoves);
    setNewMovesToast(0);
  };

  // Format eval display
  const formatEval = (): { text: string; color: string } => {
    if (currentEval.mate !== null) {
      const m = currentEval.mate;
      return {
        text: m > 0 ? `M${m}` : `M${-m}`,
        color: m > 0 ? 'var(--color-success, green)' : 'var(--color-danger, red)',
      };
    }
    // Cap the displayed number the way mainstream chess UIs do. Stockfish emits
    // very large centipawn scores in crushing (but not forced-mate) positions --
    // its PV wins tons of material -- so the raw value (e.g. +60.7) is both
    // unusual to show and meaningless once a side is clearly winning. Show
    // ">+35.0"/"<-35.0" past the cap; the underlying currentEval.cp is left
    // untouched so the chart and eval bar (which clamp separately) are unchanged.
    const pawns = currentEval.cp / 100;
    const cappedMagnitude = Math.min(Math.abs(pawns), EVAL_DISPLAY_CAP_PAWNS).toFixed(1);
    const beyondCap = Math.abs(pawns) > EVAL_DISPLAY_CAP_PAWNS;
    const sign = pawns < 0 ? '-' : '+';
    const overflow = beyondCap ? (pawns < 0 ? '<' : '>') : '';
    return {
      text: `${overflow}${sign}${cappedMagnitude}`,
      color: '',
    };
  };

  // Eval bar value (0-100, 50 = equal)
  const evalBarValue = (() => {
    let effectiveCp = currentEval.cp;
    if (currentEval.mate !== null) {
      effectiveCp = currentEval.mate > 0 ? 10000 : -10000;
    }
    const clampedCp = Math.max(-1000, Math.min(1000, effectiveCp));
    return 50 - (clampedCp / 20);
  })();

  // Eval bar class
  const evalBarClass = (() => {
    if (currentEval.cp > 100 || (currentEval.mate !== null && currentEval.mate > 0)) {
      return 'progress is-success';
    }
    if (currentEval.cp < -100 || (currentEval.mate !== null && currentEval.mate < 0)) {
      return 'progress is-danger';
    }
    return 'progress is-warning';
  })();

  // Chart data - evaluations for each move after the start position
  // The chart shows one point per move, with the evaluation from white's perspective
  const analyzedMoves = moves.slice(1);  // Skip start position
  const chartLabels = analyzedMoves.map((_, i) => String(i + 1));
  const chartEvals = analyzedMoves.map((m) => {
    if (m.eval === null) return 0;  // Show 0 for unanalyzed instead of null (avoids gaps)
    return Math.max(-500, Math.min(500, m.eval));
  });

  const chartData = {
    labels: chartLabels,
    datasets: [
      {
        label: 'Eval',
        data: chartEvals,
        fill: true,
        borderColor: 'rgb(150, 150, 150)',
        borderWidth: 2,
        tension: 0.4,
        backgroundColor: 'rgba(150, 150, 150, 0.3)',
        pointRadius: analyzedMoves.map((_, i) => i + 1 === movePos ? 6 : 3),
        pointBackgroundColor: analyzedMoves.map((_, i) =>
          i + 1 === movePos ? '#aa44aa' : 'rgba(255, 255, 255, 1)'
        ),
        pointBorderColor: analyzedMoves.map((_, i) =>
          i + 1 === movePos ? '#aa44aa' : 'rgb(150, 150, 150)'
        ),
      },
    ],
  };

  const chartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    animation: {
      duration: 0,  // Disable animations for faster updates
    },
    clip: false,  // Allow points to render outside chart area
    plugins: {
      legend: { display: false },
      title: { display: false },
      tooltip: {
        callbacks: {
          title: (items: any[]) => t('analysis.tooltipMove', { n: items[0]?.dataIndex + 1 || '' }),
          label: (item: any) => {
            const cp = item.raw;
            if (cp === null) return t('analysis.notAnalyzed');
            return t('analysis.pawns', { value: (cp / 100).toFixed(2) });
          },
        },
      },
    },
    scales: {
      y: {
        type: 'linear' as const,
        min: -500,
        max: 500,
        ticks: {
          stepSize: 250,
          callback: function(tickValue: string | number) {
            const value = typeof tickValue === 'number' ? tickValue : parseFloat(tickValue);
            return (value / 100).toFixed(0);
          },
        },
        grid: { color: 'rgba(200,200,200,0.3)' },
      },
      x: {
        type: 'category' as const,
        display: false,
        grid: { display: false },
      },
    },
    onClick: (_: any, elements: any[]) => {
      if (elements.length > 0) {
        setMovePos(elements[0].index + 1);
      }
    },
  };

  const evalDisplay = formatEval();

  return (
    <div className="analysis-widget">
      {/* Eval display and best move */}
      <div className="analysis-eval-display">
        <span
          className="eval-score"
          style={{ color: evalDisplay.color || undefined }}
        >
          {movePos > 0 ? evalDisplay.text : '0.0'}
        </span>
        <span className="eval-best-move">
          {bestMove ? (
            // In live mode at latest position: show toggle for best move visibility
            mode === 'live' && movePos === totalMoves && onToggleShowBestMove ? (
              showBestMoveForLatest ? (
                <>{t('analysis.best')} <strong>{bestMove}</strong> <button className="best-move-toggle" onClick={onToggleShowBestMove} title={t('analysis.hideBestMove')}>&times;</button></>
              ) : (
              <button className="best-move-toggle-link" onClick={onToggleShowBestMove}>{t('analysis.showBest')}</button>
            )
          ) : (
              // Static mode or not at latest: always show best move
              <>{t('analysis.best')} <strong>{bestMove}</strong></>
            )
          ) : (
            analyzing ? t('analysis.analyzing') : (sfReady ? t('analysis.waiting') : t('analysis.loadingStockfish'))
          )}
        </span>
      </div>

      {/* Eval bar - horizontal progress bar */}
      <progress
        className={evalBarClass}
        value={evalBarValue}
        max={100}
      />

      {/* Chart */}
      <div className="analysis-chart">
        <Line
          ref={chartRef as any}
          data={chartData}
          options={chartOptions as any}
        />
      </div>

      {/* Navigation buttons */}
      <div className="analysis-nav">
        <button className="button is-small" onClick={goFirst} title={t('analysis.firstMove')}>&lt;&lt;</button>
        <button className="button is-small" onClick={goPrev} title={t('analysis.previousMove')}>&lt;</button>
        <span className="move-indicator">{movePos}/{totalMoves}</span>
        <button className="button is-small" onClick={goNext} title={t('analysis.nextMove')}>&gt;</button>
        <button className="button is-small" onClick={goLast} title={t('analysis.lastMove')}>&gt;&gt;</button>
      </div>

      {/* New moves toast (live mode only) */}
      {mode === 'live' && newMovesToast > 0 && (
        <div className="notification is-info is-light new-moves-toast" onClick={jumpToLatest}>
          {t('analysis.newMoves', { count: newMovesToast })}
        </div>
      )}
    </div>
  );
}
