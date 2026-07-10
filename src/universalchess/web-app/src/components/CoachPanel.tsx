import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Card, CardHeader } from './ui';
import { apiFetch } from '../utils/api';
import { renderFigurineText } from '../utils/figurineText';
import './CoachPanel.css';

interface CoachPanelProps {
  /** Database id of the game whose move is being viewed, or null if unknown. */
  gameId: number | null;
  /** 1-based ply currently viewed (0 = start position, before any move). */
  ply: number;
  /**
   * Identity (e.g. UCI) of the move at the viewed ply. Included in the cache key
   * so that when a live takeback replaces the move occupying a ply, the cached
   * coaching for the old move is not shown for the new one -- the changed key
   * misses the cache and refetches. Omit for static games (no takebacks).
   */
  moveKey?: string;
  /**
   * Container chrome: 'box' matches the live board, 'card' matches Analyze, and
   * 'inline' renders the coaching text with no box/card chrome so it can be
   * embedded inside another card (e.g. above the Analysis graph).
   */
  variant?: 'box' | 'card' | 'inline';
}

// Wait this long after the viewed move settles before fetching, so scrubbing
// quickly through moves does not fire (and bill) a request for every ply.
const DEBOUNCE_MS = 500;

interface CoachResponse {
  statement: string | null;
  cached?: boolean;
  error: string | null;
  // Failure category and a user-facing sentence, present on a generation failure.
  // ``reason`` distinguishes a permanent problem (quota/auth) from a transient one
  // (rate_limited/unavailable) so the panel only offers Retry when retrying helps.
  reason?: string;
  message?: string;
}

type Status =
  | { kind: 'prompt' }          // no move selected (start position)
  | { kind: 'loading' }
  | { kind: 'ready'; text: string }
  | { kind: 'pending' }         // move not yet stored (e.g. latest live move)
  // ``retryable`` is false for a billing/key problem, where a retry cannot help
  // until the user fixes it, so the panel shows the reason without a Retry button.
  | { kind: 'error'; message: string; retryable: boolean };

/**
 * Shows the AI coach's remark for the currently-viewed move.
 *
 * A stored statement is shown instantly; an unstored one is generated after the
 * move settles (debounced) via GET /api/coach/statement/<gameId>/<ply>, then
 * cached in memory so revisiting a move never refetches. When no coach provider
 * is configured the panel hides itself entirely (learned from the endpoint's
 * ``not_configured`` response) so it never nags a board without a coach set up.
 */
export function CoachPanel({ gameId, ply, moveKey, variant = 'box' }: CoachPanelProps) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<Status>({ kind: 'prompt' });
  // undefined until the first response tells us whether a coach is configured;
  // false hides the panel for the rest of the session.
  const [configured, setConfigured] = useState<boolean | undefined>(undefined);
  const [retryToken, setRetryToken] = useState(0);

  // Per-move statement cache, keyed "gameId:ply". Kept in a ref so it survives
  // re-renders and navigation without retriggering fetches.
  const cacheRef = useRef<Map<string, string>>(new Map());

  useEffect(() => {
    if (gameId === null || ply < 1) {
      setStatus({ kind: 'prompt' });
      return;
    }

    const key = `${gameId}:${ply}:${moveKey ?? ''}`;
    const cached = cacheRef.current.get(key);
    if (cached !== undefined) {
      setStatus({ kind: 'ready', text: cached });
      return;
    }

    let cancelled = false;
    setStatus({ kind: 'loading' });

    const timer = setTimeout(async () => {
      try {
        const res = await apiFetch(`/api/coach/statement/${gameId}/${ply}`);
        const data: CoachResponse = await res.json().catch(() => ({ statement: null, error: 'bad_json' }));
        if (cancelled) return;

        if (data.error === 'not_configured') {
          setConfigured(false);
          return;
        }
        setConfigured(true);

        if (data.statement) {
          cacheRef.current.set(key, data.statement);
          setStatus({ kind: 'ready', text: data.statement });
        } else if (data.error === 'out_of_range') {
          setStatus({ kind: 'pending' });
        } else {
          // A quota/auth failure is permanent until the user acts, so surface the
          // specific reason and suppress the (futile) Retry; transient failures
          // keep the generic retryable message.
          const permanent = data.reason === 'quota' || data.reason === 'auth';
          setStatus({
            kind: 'error',
            message: data.message ?? t('coach.unavailable'),
            retryable: !permanent,
          });
        }
      } catch {
        if (!cancelled) {
          setStatus({ kind: 'error', message: t('coach.unavailable'), retryable: true });
        }
      }
    }, DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [gameId, ply, moveKey, retryToken, t]);

  // Hidden entirely when there is no game to coach or no coach is configured.
  if (gameId === null || configured === false) {
    return null;
  }

  const body = (() => {
    switch (status.kind) {
      case 'prompt':
        return <p className="coach-panel-muted">{t('coach.selectMove')}</p>;
      case 'loading':
        return <p className="coach-panel-muted">{t('coach.loading')}</p>;
      case 'ready':
        return <p className="coach-panel-text">{renderFigurineText(status.text)}</p>;
      case 'pending':
        return <p className="coach-panel-muted">{t('coach.pending')}</p>;
      case 'error':
        return (
          <p className="coach-panel-muted">
            {status.message}{' '}
            {status.retryable && (
              <button className="coach-panel-retry" onClick={() => setRetryToken((n) => n + 1)}>
                {t('common.retry')}
              </button>
            )}
          </p>
        );
    }
  })();

  if (variant === 'inline') {
    return <div className="coach-panel-inline">{body}</div>;
  }

  if (variant === 'card') {
    return (
      <Card className="mt-4">
        <CardHeader title={t('coach.title')} />
        <div className="coach-panel-body">{body}</div>
      </Card>
    );
  }

  return (
    <div className="box coach-panel" style={{ marginTop: '1rem' }}>
      <h3 className="title is-5 box-title">{t('coach.title')}</h3>
      <div className="coach-panel-body">{body}</div>
    </div>
  );
}
