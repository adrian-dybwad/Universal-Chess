import { useCallback, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { LoginDialog } from './LoginDialog';
import { getStoredCredentials } from '../utils/api';

/**
 * Run an authenticated action, opening the login dialog on 401 and retrying.
 *
 * Returns a `dialog` element the caller must render and an `onUnauthorized`
 * callback to invoke when a privileged request returns 401. The callback stores
 * the retry, surfaces the login dialog (pre-filling an "invalid credentials"
 * message only when credentials were already stored), and replays the retry once
 * the user authenticates successfully. Shared by the Connectivity panel cards and
 * the navbar cast button so both get the same login-and-retry behavior.
 */
export function useAuthedAction() {
  const { t } = useTranslation();
  const [loginOpen, setLoginOpen] = useState(false);
  const [loginError, setLoginError] = useState<string | undefined>();
  const pending = useRef<(() => void | Promise<void>) | null>(null);

  const onUnauthorized = useCallback((retry: () => void | Promise<void>) => {
    pending.current = retry;
    setLoginError(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
    setLoginOpen(true);
  }, [t]);

  const dialog = (
    <LoginDialog
      isOpen={loginOpen}
      onClose={() => setLoginOpen(false)}
      onSuccess={() => {
        setLoginOpen(false);
        setLoginError(undefined);
        const retry = pending.current;
        pending.current = null;
        if (retry) void retry();
      }}
      errorMessage={loginError}
    />
  );

  return { dialog, onUnauthorized };
}
