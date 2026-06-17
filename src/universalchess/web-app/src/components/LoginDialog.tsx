import { useState, useEffect, type FormEvent } from 'react';
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
 */
export function LoginDialog({ isOpen, onClose, onSuccess, errorMessage }: LoginDialogProps) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [rememberMe, setRememberMe] = useState(true);
  const [error, setError] = useState('');
  const [systemUsername, setSystemUsername] = useState('');

  useEffect(() => {
    fetch(buildApiUrl('/api/system/info'))
      .then(r => r.json())
      .then(data => { if (data.username) setSystemUsername(data.username); })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (isOpen) {
      const stored = getStoredCredentials();
      if (stored) {
        try {
          const decoded = atob(stored);
          const [storedUsername] = decoded.split(':', 1);
          setUsername(storedUsername || '');
        } catch {
          setUsername('');
        }
      } else {
        setUsername('');
      }
      setPassword('');
      setError(errorMessage || '');
    }
  }, [isOpen, errorMessage]);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    
    if (!username.trim()) {
      setError('Please enter a username');
      return;
    }
    
    if (!password) {
      setError('Please enter a password');
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

  if (!isOpen) return null;

  const hasStoredCredentials = !!getStoredCredentials();

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div className="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="dialog-header">
          <h3>Authentication Required</h3>
          <button className="dialog-close" onClick={onClose}>×</button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="dialog-body">
            <p className="dialog-description">
              Enter your Raspberry Pi credentials to modify settings.
              This is the same username and password you use for SSH or WebDAV.
            </p>

            <div className="form-group">
              <label htmlFor="auth-username">Username</label>
              <input
                id="auth-username"
                type="text"
                value={username}
                onChange={(e) => {
                  setUsername(e.target.value);
                  setError('');
                }}
                placeholder={systemUsername || 'username'}
                autoComplete="username"
              />
            </div>

            <div className="form-group">
              <label htmlFor="auth-password">Password</label>
              <input
                id="auth-password"
                type="password"
                value={password}
                onChange={(e) => {
                  setPassword(e.target.value);
                  setError('');
                }}
                placeholder="Enter your password"
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
                Remember me on this device
              </label>
            </div>

            {error && <div className="form-error">{error}</div>}
          </div>

          <div className="dialog-footer">
            {hasStoredCredentials && (
              <button type="button" className="btn btn-ghost" onClick={handleLogout}>
                Clear Saved Credentials
              </button>
            )}
            <div className="dialog-footer-right">
              <button type="button" className="btn btn-secondary" onClick={onClose}>
                Cancel
              </button>
              <button type="submit" className="btn btn-primary">
                Login
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}

