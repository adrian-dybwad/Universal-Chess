import { useCallback, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { LoginDialog } from './LoginDialog';
import { getStoredCredentials } from '../utils/api';

/**
 * Credential prompt and replay for a request the board refused with 401.
 *
 * Every write in this app is authenticated, and the same four-part scaffold had
 * been rewritten in each component that makes one: an open flag, an error
 * string, a ref holding the rejected action, and a success handler that replays
 * it. The copies drifted -- some forgot to distinguish wrong credentials from
 * absent ones, some left the queued action in place after the dialog was
 * dismissed so it fired at the next unrelated login. This owns all four.
 *
 * Callers hand `requireLogin` the response and a closure that re-sends the same
 * request, and render `loginDialog` anywhere in their tree:
 *
 *     const { requireLogin, loginDialog } = useLoginRetry();
 *
 *     const save = useCallback(async () => {
 *       // Compute the request once, so the replay is identical and the user is
 *       // not asked to redo an edit or re-answer a confirmation.
 *       const submit = async (): Promise<void> => {
 *         const response = await apiFetch(url, { method: 'POST', requiresAuth: true });
 *         if (requireLogin(response, submit)) return;
 *         ...
 *       };
 *       await submit();
 *     }, [requireLogin]);
 *
 * The retry is a closure rather than a description of the request because the
 * actions differ in shape -- a file, an engine name, a move, nothing at all --
 * and a named inner function lets each call site queue itself without a
 * component-level callback having to depend on itself.
 */

export interface UseLoginRetryOptions {
  /**
   * Run when the user dismisses the dialog, abandoning the queued request.
   *
   * For callers that showed something on the strength of the request going
   * through -- an optimistic board frame, say -- this is where that is undone.
   * Provide a stable (useCallback) function.
   */
  onAbandon?: () => void;
}

export interface UseLoginRetry {
  /**
   * True when `response` was a 401: `retry` is queued and the dialog is open,
   * and the caller must stop. False for every other status, so the caller's own
   * error handling runs unchanged.
   */
  requireLogin: (response: Pick<Response, 'status'>, retry: () => Promise<void>) => boolean;
  /**
   * Queue `retry` and open the dialog unconditionally.
   *
   * For callers with no fetch Response to inspect: an XHR upload reading
   * `xhr.status`, or a request not attempted at all because no credentials are
   * held. Prefer `requireLogin` where there is a response.
   */
  promptLogin: (retry: () => Promise<void>) => void;
  /** The dialog element. Render it once, anywhere in the caller's tree. */
  loginDialog: ReactNode;
}

export function useLoginRetry({ onAbandon }: UseLoginRetryOptions = {}): UseLoginRetry {
  const { t } = useTranslation();
  const [isOpen, setIsOpen] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | undefined>();
  const pendingRetryRef = useRef<(() => Promise<void>) | null>(null);

  const promptLogin = useCallback((retry: () => Promise<void>) => {
    // Credentials already stored means the ones held are wrong, not missing --
    // say so, otherwise the dialog reappears with no explanation.
    setErrorMessage(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
    pendingRetryRef.current = retry;
    setIsOpen(true);
  }, [t]);

  const requireLogin = useCallback((response: Pick<Response, 'status'>, retry: () => Promise<void>) => {
    if (response.status !== 401) return false;
    promptLogin(retry);
    return true;
  }, [promptLogin]);

  const handleSuccess = useCallback(async () => {
    setIsOpen(false);
    setErrorMessage(undefined);
    const retry = pendingRetryRef.current;
    pendingRetryRef.current = null;
    if (retry) await retry();
  }, []);

  const handleClose = useCallback(() => {
    setIsOpen(false);
    // Drop the queued request: leaving it would fire it at the next login the
    // user performs for something else entirely.
    pendingRetryRef.current = null;
    onAbandon?.();
  }, [onAbandon]);

  const loginDialog = (
    <LoginDialog
      isOpen={isOpen}
      onClose={handleClose}
      onSuccess={() => void handleSuccess()}
      errorMessage={errorMessage}
    />
  );

  return { requireLogin, promptLogin, loginDialog };
}
