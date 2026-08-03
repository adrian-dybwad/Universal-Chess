import { useEffect, useRef, useState } from 'react';
import { useSettingsStore } from '../stores/settingsStore';
import { getStockfishService, destroyStockfishService } from '../services/stockfish';
import { MATE_SCORE_CP } from '../types/game';

/** Search depth for the viewed position. */
const DEEP_ANALYSIS_DEPTH = 18;

/** An evaluation strong enough to override the board's, in White's perspective. */
export interface DeepAnalysisResult {
  evalCp: number;
  bestMove: string | null;
}

/**
 * Normalise one engine result to White's perspective and the shared centipawn
 * encoding used everywhere else (mate as the +/-{@link MATE_SCORE_CP} sentinel).
 *
 * UCI reports scores from the side to move, so a Black-to-move position needs
 * the sign flipped before it can sit alongside the board's White-perspective
 * numbers -- otherwise every other ply of the eval chart is inverted.
 */
export function toWhitePerspective(
  fen: string,
  score: number | null,
  mate: number | null,
): number | null {
  const blackToMove = fen.split(' ')[1] === 'b';
  if (mate !== null) {
    const whiteMates = blackToMove ? mate < 0 : mate > 0;
    return whiteMates ? MATE_SCORE_CP : -MATE_SCORE_CP;
  }
  if (score === null) return null;
  return blackToMove ? -score : score;
}

function isDeepAnalysisEnabled(value: string | undefined): boolean {
  return value?.toLowerCase() === 'true';
}

/**
 * Evaluate the position the user is viewing with the opt-in CDN engine.
 *
 * Returns `null` whenever the board's own evaluation should stand: the setting
 * is off (the default, and the only configuration that contacts nobody), no
 * position is selected, the search has not finished, or the engine failed to
 * load. That last case is not an error to surface -- a LAN-only board or a CDN
 * outage simply leaves the caller with the board's number, which is correct,
 * just shallower.
 *
 * Results are keyed by the FEN they describe and kept for the lifetime of the
 * hook, so stepping back and forth through a game re-searches nothing and no
 * position can ever be shown another position's evaluation. Turning the setting
 * off releases the engine but keeps the results: they remain correct, and
 * turning it back on then costs no repeated search.
 */
export function useDeepAnalysis(fen: string | null): DeepAnalysisResult | null {
  const load = useSettingsStore((s) => s.load);
  const settingValue = useSettingsStore((s) => s.raw?.game?.deep_analysis);
  const enabled = isDeepAnalysisEnabled(settingValue);

  const [results, setResults] =
    useState<ReadonlyMap<string, DeepAnalysisResult>>(new Map());
  // Mirrors `results` for the effect to test without depending on it, which
  // would re-run the effect on every completed search.
  const knownRef = useRef(new Set<string>());

  // Seed the store in case this hook mounts before anything else triggered a
  // load; load() is idempotent. A failure is swallowed deliberately: an
  // unreachable board leaves the setting at its default (off), which is the
  // safe reading, and an unhandled rejection here would surface as a spurious
  // error while the view itself is perfectly usable.
  useEffect(() => {
    load?.()?.catch(() => {});
  }, [load]);

  // Turning the setting off must release the worker and the ~39 MB of blobs it
  // holds, not merely stop reading from it.
  useEffect(() => {
    if (enabled) return;
    destroyStockfishService();
  }, [enabled]);

  useEffect(() => {
    if (!enabled || !fen || knownRef.current.has(fen)) return;

    let cancelled = false;
    getStockfishService()
      .analyze(fen, DEEP_ANALYSIS_DEPTH, true)
      .then((analysis) => {
        if (cancelled) return;
        const evalCp = toWhitePerspective(fen, analysis.score, analysis.mate);
        if (evalCp === null) return;
        knownRef.current.add(fen);
        setResults((prev) =>
          new Map(prev).set(fen, { evalCp, bestMove: analysis.bestMove }),
        );
      })
      .catch(() => {
        // Fall back to the board's evaluation. `stop()` rejects superseded
        // requests as a matter of course, so this is not necessarily a failure.
      });

    return () => {
      cancelled = true;
    };
  }, [enabled, fen]);

  if (!enabled || !fen) return null;
  return results.get(fen) ?? null;
}
