import { useEffect, useState } from 'react';
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
 * Statements are produced by the board as it plays; this panel only displays what
 * the board stored. It reads GET /api/coach/statement/<gameId>/<ply> (debounced
 * until the move settles) and caches the text in memory so revisiting a move never
 * refetches. The endpoint never generates, so a move the board has not coached yet
 * reports ``not_generated`` and shows as pending rather than triggering a billed
 * AI call from the browser. When no coach provider is configured the panel hides
 * itself entirely (via ``not_configured``) so it never nags a board without a
 * coach set up.
 */
export function CoachPanel({ gameId, ply, moveKey, variant = 'box' }: CoachPanelProps) {
  const { t } = useTranslation();
  // undefined until the first response tells us whether a coach is configured;
  // false hides the panel for the rest of the session.
  const [configured, setConfigured] = useState<boolean | undefined>(undefined);
  const [retryToken, setRetryToken] = useState(0);

  // Per-move statement cache, keyed "gameId:ply:move". Held as state rather than
  // a ref because what the panel shows is read from it while rendering: a move
  // already coached must appear at once, without a loading line and without
  // asking the board again.
  const [statements, setStatements] = useState<ReadonlyMap<string, string>>(new Map());

  // What the last request for a move concluded, tagged with the move it was
  // about. Tagging is what stops one move's failure from being shown against the
  // next one while that next one is still loading.
  const [outcome, setOutcome] = useState<{ key: string; status: Status } | null>(null);

  // The move being viewed, or null at the start position, where no move has been
  // played for a coach to remark on.
  const key = gameId !== null && ply >= 1 ? `${gameId}:${ply}:${moveKey ?? ''}` : null;

  // Everything on screen follows from the move and what is known about it, so it
  // is worked out here rather than pushed into state by the effect below. Until
  // a request for this move concludes, it is loading.
  const cached = key === null ? undefined : statements.get(key);
  const status: Status =
    key === null ? { kind: 'prompt' }
    : cached !== undefined ? { kind: 'ready', text: cached }
    : outcome?.key === key ? outcome.status
    : { kind: 'loading' };

  useEffect(() => {
    if (key === null || statements.has(key)) return;

    let cancelled = false;
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
          const statement = data.statement;
          setStatements((prev) => new Map(prev).set(key, statement));
        } else if (data.error === 'not_generated') {
          setOutcome({ key, status: { kind: 'pending' } });
        } else {
          // A quota/auth failure is permanent until the user acts, so surface the
          // specific reason and suppress the (futile) Retry; transient failures
          // keep the generic retryable message.
          const permanent = data.reason === 'quota' || data.reason === 'auth';
          setOutcome({
            key,
            status: {
              kind: 'error',
              message: data.message ?? t('coach.unavailable'),
              retryable: !permanent,
            },
          });
        }
      } catch {
        if (!cancelled) {
          setOutcome({ key, status: { kind: 'error', message: t('coach.unavailable'), retryable: true } });
        }
      }
    }, DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [key, statements, gameId, ply, retryToken, t]);

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
              <button
                className="coach-panel-retry"
                onClick={() => {
                  // Dropping the outcome puts the panel back to loading at once,
                  // so the click is acknowledged before the debounce elapses.
                  setOutcome(null);
                  setRetryToken((n) => n + 1);
                }}
              >
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
