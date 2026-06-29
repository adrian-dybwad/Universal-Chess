import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { MenuIcon } from './MenuIcon';
import { buildApiUrl } from '../utils/api';
import './ConnectionStatus.css';
import './CastButton.css';

interface CastButtonProps {
  /** When true, shows only the cast glyph without text (for mobile). */
  compact?: boolean;
}

type CastStateName = 'idle' | 'connecting' | 'streaming' | 'reconnecting' | 'error';

interface CastDevice {
  name: string;
  state: CastStateName;
}

// States that mean the board is actively pushing a stream to at least one
// device. 'idle' and 'error' are not active; 'error' is surfaced as a warning
// rather than an active stream so a failed cast does not read as "casting".
const ACTIVE_STATES: ReadonlySet<CastStateName> = new Set(['connecting', 'streaming', 'reconnecting']);

/**
 * Navbar Chromecast button.
 *
 * Reflects the board's live Chromecast streaming state (mirrored over the same
 * `chromecast_state` SSE event the Connectivity panel listens to) and links to
 * the Connectivity settings tab where casting is started, stopped, and managed.
 * The board push is one-way with no replay, so the button defaults to idle until
 * the first event arrives; this is correct because no event means nothing is
 * streaming.
 */
export function CastButton({ compact = false }: CastButtonProps) {
  const navigate = useNavigate();
  const [devices, setDevices] = useState<CastDevice[]>([]);

  useEffect(() => {
    const es = new EventSource(buildApiUrl('/events'));
    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'chromecast_state') {
          setDevices(Array.isArray(data.devices) ? data.devices : []);
        }
      } catch {
        /* ignore non-JSON keepalives */
      }
    };
    return () => es.close();
  }, []);

  const activeDevices = devices.filter((d) => ACTIVE_STATES.has(d.state));
  const hasError = devices.some((d) => d.state === 'error');
  const streaming = activeDevices.length > 0;

  const statusClass = streaming ? 'is-success' : hasError ? 'is-danger' : 'is-light';
  const statusText = streaming
    ? activeDevices.length === 1
      ? activeDevices[0].name
      : `Casting (${activeDevices.length})`
    : 'Cast';
  const title = streaming
    ? `Streaming to ${activeDevices.map((d) => d.name).join(', ')}\nClick to manage`
    : 'Not casting\nClick to manage Chromecast';

  return (
    <button
      className={`tag tag-button ${statusClass} ${compact ? 'tag-compact' : ''} cast-button`}
      onClick={() => navigate('/settings/connectivity')}
      title={title}
      aria-label={title}
    >
      <MenuIcon name="cast" size={16} />
      {!compact && <span className="status-text">{statusText}</span>}
    </button>
  );
}
