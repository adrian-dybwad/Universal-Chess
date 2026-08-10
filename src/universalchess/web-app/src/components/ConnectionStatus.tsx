import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useGameStore } from '../stores/gameStore';
import { ApiSettingsDialog } from './ApiSettingsDialog';
import { getApiUrl, isCrossOriginApi } from '../utils/api';
import './ConnectionStatus.css';

interface ConnectionStatusProps {
  /** When true, shows only the status dot without text (for mobile) */
  compact?: boolean;
}

/**
 * Connection status indicator - displays as a clickable tag in the navbar.
 * Clicking opens the API settings dialog to change the chess board URL.
 */
export function ConnectionStatus({ compact = false }: ConnectionStatusProps) {
  const { t } = useTranslation();
  const connectionStatus = useGameStore((state) => state.connectionStatus);
  const [dialogOpen, setDialogOpen] = useState(false);

  const getStatusClass = () => {
    switch (connectionStatus) {
      case 'connected':
        return 'is-success';
      case 'reconnecting':
        return 'is-warning';
      case 'disconnected':
        return 'is-danger';
      default:
        return 'is-light';
    }
  };

  const getStatusText = () => {
    switch (connectionStatus) {
      case 'connected':
        return t('connection.connected');
      case 'reconnecting':
        return t('connection.reconnecting');
      case 'disconnected':
        return t('connection.offline');
      default:
        return t('connection.unknown');
    }
  };

  const handleSave = () => {
    // Reload the page to reconnect with new API URL
    window.location.reload();
  };

  // Show custom API indicator if using a different origin
  const showApiIndicator = isCrossOriginApi();
  const apiUrl = getApiUrl();
  const apiHost = (() => {
    try {
      return new URL(apiUrl).host;
    } catch {
      return apiUrl;
    }
  })();

  return (
    <>
      <button
        className={`tag tag-button ${getStatusClass()} ${compact ? 'tag-compact' : ''}`}
        id="connection-status"
        onClick={() => setDialogOpen(true)}
        title={`${t('connection.changeSettings')}\n${showApiIndicator ? t('connection.connectedTo', { host: apiHost }) : t('connection.localServer')}`}
      >
        <span className={`status-dot ${connectionStatus}`} />
        {!compact && (
          <>
            <span className="status-text">{getStatusText()}</span>
            {showApiIndicator && (
              <span className="api-host">{apiHost}</span>
            )}
          </>
        )}
      </button>

      {dialogOpen && (
        <ApiSettingsDialog
          onClose={() => setDialogOpen(false)}
          onSave={handleSave}
        />
      )}
    </>
  );
}
