import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { Button, Card } from './ui';
import { useLoginRetry } from './useLoginRetry';

/**
 * Shared chrome for Settings log viewers (System Event Log, Original Centaur
 * launch log, and any later dump that needs the same refresh/auth/empty flow).
 *
 * Owns title, intro, Refresh, login retry, loading/error/empty states. Callers
 * supply ``fetchLog`` (which may call ``requireLogin`` with ``retryLoad`` and
 * return ``pending`` while the dialog is open) and render the loaded body
 * through ``children``.
 */

export type LogViewerShellLabels = {
  title: string;
  intro: string;
  errorLabel: string;
  refresh: string;
  refreshing: string;
  none: string;
  loadFailed: string;
  networkError: string;
};

export type LogFetchResult<T> =
  | { kind: 'ok'; data: T }
  | { kind: 'empty' }
  | { kind: 'error'; message: string }
  | { kind: 'pending' };

export type LogViewerRequireLogin = (
  response: Pick<Response, 'status'>,
  retry: () => Promise<void>,
) => boolean;

export type LogViewerFetchContext = {
  requireLogin: LogViewerRequireLogin;
  /** Re-run the shell load after a successful login (pass this to requireLogin). */
  retryLoad: () => Promise<void>;
};

type LogViewerShellProps<T> = {
  labels: LogViewerShellLabels;
  /** Auth-aware load. Return ``pending`` when ``requireLogin`` queued a retry. */
  fetchLog: (ctx: LogViewerFetchContext) => Promise<LogFetchResult<T>>;
  /** When omitted, any ``ok`` result is treated as non-empty. */
  isEmpty?: (data: T) => boolean;
  children: (data: T) => ReactNode;
};

export function LogViewerShell<T>({
  labels,
  fetchLog,
  isEmpty,
  children,
}: LogViewerShellProps<T>) {
  const [data, setData] = useState<T | null>(null);
  // Starts true because the mount effect loads immediately. load() does NOT set
  // loading synchronously on mount (every update after the awaited fetch) so the
  // effect triggers no synchronous render cascade — the pattern Settings loaders
  // use, enforced by react-hooks/set-state-in-effect. Refresh flips it from an
  // event handler.
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { requireLogin, loginDialog } = useLoginRetry({
    onAbandon: () => setLoading(false),
  });

  const load = useCallback(async () => {
    // Named inner so requireLogin can replay the same request. leftForLogin
    // keeps the Refresh spinner up while the dialog is open; abandon clears it.
    let leftForLogin = false;
    const submit = async (): Promise<void> => {
      leftForLogin = false;
      try {
        const gatedRequireLogin: LogViewerRequireLogin = (response, retry) => {
          const queued = requireLogin(response, retry);
          if (queued) leftForLogin = true;
          return queued;
        };
        const result = await fetchLog({
          requireLogin: gatedRequireLogin,
          retryLoad: submit,
        });
        if (result.kind === 'pending') {
          leftForLogin = true;
          return;
        }
        if (result.kind === 'error') {
          setError(result.message);
          return;
        }
        setError(null);
        setData(result.kind === 'empty' ? null : result.data);
        setLoaded(true);
      } catch {
        setError(labels.networkError);
      } finally {
        if (!leftForLogin) setLoading(false);
      }
    };
    await submit();
  }, [fetchLog, labels.networkError, requireLogin]);

  const handleRefresh = () => {
    setLoading(true);
    void load();
  };

  useEffect(() => {
    void (async () => {
      await load();
    })();
  }, [load]);

  const showEmpty =
    loaded
    && !error
    && (data === null || (isEmpty !== undefined && isEmpty(data)));
  const showContent =
    data !== null && !error && !(isEmpty !== undefined && isEmpty(data));

  return (
    <>
      {loginDialog}
      <section>
        <h4 className="settings-group-title">{labels.title}</h4>
        <p className="text-muted mb-4">{labels.intro}</p>
        {error && (
          <Card variant="danger" className="mb-4">
            <strong>{labels.errorLabel}</strong> {error}
          </Card>
        )}
        <div className="mb-4">
          <Button variant="secondary" onClick={handleRefresh} disabled={loading}>
            {loading ? labels.refreshing : labels.refresh}
          </Button>
        </div>
        {showEmpty && <p className="text-muted">{labels.none}</p>}
        {showContent && children(data as T)}
      </section>
    </>
  );
}
