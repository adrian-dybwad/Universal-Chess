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
import type { ActiveElement, ChartOptions, TooltipItem } from 'chart.js';
import type { PositionEntry } from '../types/game';
import { MATE_SCORE_CP } from '../types/game';
import { useDeepAnalysis } from '../hooks/useDeepAnalysis';
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
  /**
   * Centipawns from White's perspective as evaluated by the board, or
   * +/-MATE_SCORE_CP for a forced mate. null means the board has not analysed
   * this position -- distinct from 0, which is a real "dead equal" evaluation.
   */
  eval: number | null;
  /** The board engine's best move in UCI, or null when unanalysed. */
  bestMove: string | null;
  /** UCI of the move that produced this position; null for the start. */
  uci: string | null;
}

/** True when a centipawn value is the board's forced-mate sentinel. */
function isMateScore(cp: number): boolean {
  return Math.abs(cp) >= MATE_SCORE_CP;
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
  const [newMovesToast, setNewMovesToast] = useState(0);

  const chartRef = useRef<ChartJS<'line'> | null>(null);
  // The positions signature the move list above was built from; '' until the
  // first list arrives (an empty game signs as '' too, and has nothing to build).
  const [appliedSignature, setAppliedSignature] = useState('');

  // Total moves in the game
  const totalMoves = moves.length > 0 ? moves.length - 1 : 0;  // moves[0] is start position

  // Build move history from the authoritative per-ply positions (python-chess
  // computed on the server). This is the single source for both variants; the
  // web no longer replays the PGN with chess.js, and it no longer runs an
  // engine of its own -- each entry already carries the board's evaluation and
  // best move for that ply.
  //
  // A signature gates rebuilds so frequent live broadcasts that don't change
  // anything this component displays (e.g. pending-move updates) do not churn
  // state. It includes the evals because the board re-broadcasts as searches
  // complete: keying on FENs alone would ignore exactly those updates and the
  // newest ply's evaluation would never appear.
  //
  // Applied during render rather than from an effect: this is the move list the
  // props already describe, not a synchronisation with anything outside React.
  // Taking it in an effect meant every arriving move was painted once with the
  // previous list and cursor before the correction landed.
  const entries = Array.isArray(positions) ? positions : [];
  const signature = entries
    .map((p) => `${p.fen}:${p.eval ?? ''}:${p.best_move ?? ''}`)
    .join('|');

  if (signature !== appliedSignature) {
    // The first entry is the start; later entries carry the post-move FEN/SAN/UCI.
    const newMoves: MoveData[] = entries.map((p, i) => ({
      fen: p.fen,
      san: i === 0 ? t('analysis.start') : (p.san ?? p.uci ?? ''),
      eval: p.eval ?? null,
      bestMove: p.best_move ?? null,
      uci: p.uci,
    }));

    const prevLength = moves.length;
    setAppliedSignature(signature);
    setMoves(newMoves);

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
  }

  // Evaluation and best move for the position being viewed, read straight from
  // the board's per-ply data. Derived during render rather than held in state:
  // there is nothing asynchronous left to await, and a state copy could only
  // drift from the entry it mirrors.
  //
  // The opt-in deep-analysis engine, when the user has enabled it and its search
  // has landed, overrides both for this one position; it is a far deeper search
  // than the board's. It yields to the board whenever it is off or unavailable.
  const currentMove = moves[movePos] ?? null;
  const deep = useDeepAnalysis(currentMove?.fen ?? null);
  const currentEvalCp = deep ? deep.evalCp : (currentMove?.eval ?? null);
  const bestMove = deep ? deep.bestMove : (currentMove?.bestMove ?? null);

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

  // Format eval display. Returns null when the board has not analysed this
  // position: showing "0.0" there would claim the position is equal, which is
  // a real evaluation and not what "unanalysed" means.
  const formatEval = (): { text: string; color: string } | null => {
    if (currentEvalCp === null) return null;
    if (isMateScore(currentEvalCp)) {
      const whiteMates = currentEvalCp > 0;
      return {
        // The board sends only the +/- sentinel, not the distance, so the
        // number of moves to mate is deliberately not displayed rather than
        // guessed at.
        text: 'M',
        color: whiteMates ? 'var(--color-success, green)' : 'var(--color-danger, red)',
      };
    }
    // Cap the displayed number the way mainstream chess UIs do: past the cap
    // the exact figure conveys nothing beyond "clearly winning", so show
    // ">+35.0"/"<-35.0". The underlying centipawn value is left untouched so
    // the chart and eval bar (which clamp separately) are unchanged.
    const pawns = currentEvalCp / 100;
    const cappedMagnitude = Math.min(Math.abs(pawns), EVAL_DISPLAY_CAP_PAWNS).toFixed(1);
    const beyondCap = Math.abs(pawns) > EVAL_DISPLAY_CAP_PAWNS;
    const sign = pawns < 0 ? '-' : '+';
    const overflow = beyondCap ? (pawns < 0 ? '<' : '>') : '';
    return {
      text: `${overflow}${sign}${cappedMagnitude}`,
      color: '',
    };
  };

  // Eval bar value (0-100, 50 = equal). An unanalysed position sits at the
  // midpoint, matching the absent headline rather than implying an advantage.
  const evalBarValue = (() => {
    const clampedCp = Math.max(-1000, Math.min(1000, currentEvalCp ?? 0));
    return 50 - (clampedCp / 20);
  })();

  // Eval bar class
  const evalBarClass = (() => {
    if (currentEvalCp !== null && currentEvalCp > 100) return 'progress is-success';
    if (currentEvalCp !== null && currentEvalCp < -100) return 'progress is-danger';
    return 'progress is-warning';
  })();

  // Chart data - one point per played move, from White's perspective.
  const analyzedMoves = moves.slice(1);  // Skip start position
  const chartLabels = analyzedMoves.map((_, i) => String(i + 1));
  // null renders as a gap (Chart.js skips null points), which is the honest
  // representation of a ply the board has not analysed. The previous code
  // substituted 0 here, drawing an unanalysed ply as a dead-equal position.
  const chartEvals = analyzedMoves.map((m) =>
    m.eval === null ? null : Math.max(-500, Math.min(500, m.eval))
  );

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

  const chartOptions: ChartOptions<'line'> = {
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
          title: (items: TooltipItem<'line'>[]) =>
            t('analysis.tooltipMove', { n: (items[0]?.dataIndex ?? 0) + 1 || '' }),
          label: (item: TooltipItem<'line'>) => {
            const cp = item.raw as number | null;
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
    onClick: (_event: unknown, elements: ActiveElement[]) => {
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
          style={{ color: evalDisplay?.color || undefined }}
        >
          {evalDisplay?.text ?? ''}
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
            t('analysis.notAnalyzedYet')
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
          ref={chartRef}
          data={chartData}
          options={chartOptions}
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
