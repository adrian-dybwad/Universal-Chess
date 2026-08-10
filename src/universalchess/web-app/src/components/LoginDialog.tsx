import { useState, useEffect, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { encodeBasicAuth, storeCredentials, getStoredCredentials, clearCredentials, buildApiUrl } from '../utils/api';
import './ApiSettingsDialog.css'; // Reuse the same dialog styles

interface LoginDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
  errorMessage?: string;
}

/**
 * Dialog for entering authentication credentials.
 * Uses the same credentials as WebDAV (Linux system user).
 *
 * A gate around the form, which holds all the state and therefore exists only
 * while the dialog is open. Every opening then starts from the saved username,
 * an empty password and the caller's message, without an effect to clear the
 * last attempt out of a form that never went away.
 */
export function LoginDialog({ isOpen, onClose, onSuccess, errorMessage }: LoginDialogProps) {
  if (!isOpen) return null;
  return <LoginForm onClose={onClose} onSuccess={onSuccess} errorMessage={errorMessage} />;
}

/** The username saved from a previous login, or '' when there is none to offer. */
function savedUsername(): string {
  const stored = getStoredCredentials();
  if (!stored) return '';
  try {
    const [username] = atob(stored).split(':', 1);
    return username || '';
  } catch {
    return '';
  }
}

function LoginForm({ onClose, onSuccess, errorMessage }: Omit<LoginDialogProps, 'isOpen'>) {
  const { t } = useTranslation();
  const [username, setUsername] = useState(savedUsername);
  const [password, setPassword] = useState('');
  const [rememberMe, setRememberMe] = useState(true);
  const [error, setError] = useState(errorMessage ?? '');
  const [systemUsername, setSystemUsername] = useState('');

  // The board's own account name, offered as the placeholder. Read when the
  // dialog opens rather than on every page load.
  useEffect(() => {
    fetch(buildApiUrl('/api/system/info'))
      .then(r => r.json())
      .then(data => { if (data.username) setSystemUsername(data.username); })
      .catch(() => {});
  }, []);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    
    if (!username.trim()) {
      setError(t('login.enterUsername'));
      return;
    }
    
    if (!password) {
      setError(t('login.enterPassword'));
      return;
    }

    const encoded = encodeBasicAuth(username.trim(), password);
    
    // Always store credentials for the API call to use.
    // If rememberMe is true, store persistently (localStorage).
    // If rememberMe is false, store for session only (sessionStorage, clears on tab close).
    storeCredentials(encoded, rememberMe);
    
    onSuccess();
  };

  const handleLogout = () => {
    clearCredentials();
    setPassword('');
    setError('');
  };

  const hasStoredCredentials = !!getStoredCredentials();

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div className="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="dialog-header">
          <h3>{t('login.title')}</h3>
          <button className="dialog-close" onClick={onClose}>×</button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="dialog-body">
            <p className="dialog-description">
              {t('login.description')}
            </p>

            <div className="form-group">
              <label htmlFor="auth-username">{t('login.username')}</label>
              <input
                id="auth-username"
                type="text"
                value={username}
                onChange={(e) => {
                  setUsername(e.target.value);
                  setError('');
                }}
                placeholder={systemUsername || t('login.usernamePlaceholder')}
                autoComplete="username"
              />
            </div>

            <div className="form-group">
              <label htmlFor="auth-password">{t('login.password')}</label>
              <input
                id="auth-password"
                type="password"
                value={password}
                onChange={(e) => {
                  setPassword(e.target.value);
                  setError('');
                }}
                placeholder={t('login.passwordPlaceholder')}
                autoComplete="current-password"
                autoFocus
              />
            </div>

            <div className="form-group">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={rememberMe}
                  onChange={(e) => setRememberMe(e.target.checked)}
                />
                {t('login.rememberMe')}
              </label>
            </div>

            {error && <div className="form-error">{error}</div>}
          </div>

          <div className="dialog-footer">
            {hasStoredCredentials && (
              <button type="button" className="btn btn-ghost" onClick={handleLogout}>
                {t('login.clearSaved')}
              </button>
            )}
            <div className="dialog-footer-right">
              <button type="button" className="btn btn-secondary" onClick={onClose}>
                {t('common.cancel')}
              </button>
              <button type="submit" className="btn btn-primary">
                {t('login.login')}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}

