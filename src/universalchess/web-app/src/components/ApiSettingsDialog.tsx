import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { getApiUrl, setApiUrl, resetApiUrl, getDefaultApiUrl, sanitizeApiUrl } from '../utils/api';
import './ApiSettingsDialog.css';

interface ApiSettingsDialogProps {
  onClose: () => void;
  onSave: () => void;
}

/**
 * Dialog for configuring the API URL.
 * Allows users to change which chess board the PWA connects to.
 *
 * Mounted only while open, so each opening starts from the stored URL with no
 * error or test result carried over from the last time. It used to stay mounted
 * and clear itself from an effect when `isOpen` turned true, which is the same
 * reset one render later.
 */
export function ApiSettingsDialog({ onClose, onSave }: ApiSettingsDialogProps) {
  const { t } = useTranslation();
  const [url, setUrl] = useState(getApiUrl);
  const [error, setError] = useState('');
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<'success' | 'error' | null>(null);

  // Require a well-formed http(s) URL. Uses the same sanitizer as storage so a
  // value the dialog accepts is exactly a value setApiUrl will store (e.g. a
  // javascript:/data: URL parses via new URL() but is rejected here).
  const validateUrl = (input: string): boolean => sanitizeApiUrl(input) !== null;

  const handleTest = async () => {
    if (!validateUrl(url)) {
      setError(t('apiSettings.invalidUrl'));
      return;
    }

    setTesting(true);
    setTestResult(null);
    setError('');

    try {
      // Determine the fetch URL
      // If testing the default API target and we're on localhost (dev mode),
      // use relative URL to go through Vite proxy
      const defaultUrl = getDefaultApiUrl();
      const isDevMode = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
      const isTestingDefault = url === defaultUrl;
      
      let fetchUrl: string;
      const fetchOptions: RequestInit = {
        method: 'GET',
        signal: AbortSignal.timeout(5000),
      };

      if (isDevMode && isTestingDefault) {
        // Use relative URL to go through Vite proxy
        fetchUrl = '/fen';
      } else {
        // Direct fetch with CORS
        fetchUrl = `${url}/fen`;
        fetchOptions.mode = 'cors';
      }

      const response = await fetch(fetchUrl, fetchOptions);

      if (response.ok) {
        setTestResult('success');
      } else {
        setTestResult('error');
        setError(t('apiSettings.serverStatus', { status: response.status }));
      }
    } catch (e) {
      setTestResult('error');
      if (e instanceof Error) {
        if (e.name === 'TimeoutError') {
          setError(t('apiSettings.timedOut'));
        } else {
          setError(t('apiSettings.couldNotConnect'));
        }
      }
    } finally {
      setTesting(false);
    }
  };

  const handleSave = () => {
    if (!validateUrl(url)) {
      setError(t('apiSettings.invalidHttpUrl'));
      return;
    }

    try {
      setApiUrl(url);
    } catch {
      // Defensive: validateUrl already guarantees validity, but keep the UI
      // responsive if setApiUrl's own sanitizer ever rejects the value.
      setError(t('apiSettings.invalidHttpUrl'));
      return;
    }
    onSave();
    onClose();
  };

  const handleReset = () => {
    resetApiUrl();
    setUrl(getDefaultApiUrl());
    setTestResult(null);
    setError('');
  };

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div className="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="dialog-header">
          <h3>{t('apiSettings.title')}</h3>
          <button className="dialog-close" onClick={onClose}>×</button>
        </div>

        <div className="dialog-body">
          <p className="dialog-description">
            {t('apiSettings.descriptionPre')}<code>http://dgt.local</code>{t('apiSettings.descriptionPost')}
          </p>

          <div className="form-group">
            <label htmlFor="api-url">{t('apiSettings.boardUrl')}</label>
            <div className="input-with-button">
              <input
                id="api-url"
                type="url"
                value={url}
                onChange={(e) => {
                  setUrl(e.target.value);
                  setError('');
                  setTestResult(null);
                }}
                placeholder="http://dgt.local"
                className={testResult === 'success' ? 'is-success' : testResult === 'error' ? 'is-error' : ''}
              />
              <button
                className="btn btn-secondary"
                onClick={handleTest}
                disabled={testing}
              >
                {testing ? t('apiSettings.testing') : t('apiSettings.test')}
              </button>
            </div>
            {error && <span className="form-error">{error}</span>}
            {testResult === 'success' && <span className="form-success">{t('apiSettings.success')}</span>}
          </div>

          <div className="dialog-info">
            <div><strong>{t('apiSettings.apiUrlInUse')}</strong> {getApiUrl()}</div>
            {getApiUrl() !== window.location.origin && (
              <div style={{ marginTop: '0.5rem' }}>
                <strong>{t('apiSettings.currentOrigin')}</strong> {window.location.origin}
              </div>
            )}
          </div>
        </div>

        <div className="dialog-footer">
          <button className="btn btn-ghost" onClick={handleReset}>
            {t('apiSettings.resetDefault')}
          </button>
          <div className="dialog-footer-right">
            <button className="btn btn-secondary" onClick={onClose}>
              {t('common.cancel')}
            </button>
            <button className="btn btn-primary" onClick={handleSave}>
              {t('apiSettings.saveReconnect')}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

