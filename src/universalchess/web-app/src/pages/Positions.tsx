import { useState, useEffect, useCallback } from 'react';
import { Card, CardHeader } from '../components/ui';
import { LoginDialog } from '../components/LoginDialog';
import { MenuIcon } from '../components/MenuIcon';
import { useGameStore } from '../stores/gameStore';
import { apiFetch, getStoredCredentials } from '../utils/api';
import '../components/ApiSettingsDialog.css';
import './Positions.css';

interface PositionEntry {
  name: string;
  fen: string;
  hint: string | null;
}

interface PositionCategory {
  name: string;
  positions: PositionEntry[];
}

/** Title-case an INI key like "scholars_mate" -> "Scholars Mate". */
function prettify(name: string): string {
  return name
    .split(/[_\s]+/)
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ');
}

/**
 * Positions page.
 *
 * Lists the predefined positions shared with the board (GET /api/positions) and
 * sets a chosen one up on the physical board (POST /api/board/setup-position).
 * When a game is in progress, a confirmation is shown first because setting up a
 * position ends that game (recorded as abandoned on the board). Auth failures
 * reuse the same LoginDialog flow as Settings.
 */
export function Positions() {
  const [categories, setCategories] = useState<PositionCategory[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [status, setStatus] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  const [pending, setPending] = useState<PositionEntry | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const [loginError, setLoginError] = useState<string | undefined>();

  const gameState = useGameStore((s) => s.gameState);
  // A game is "in progress" when the board has a live, unfinished game with at
  // least one move. Setting up a position would end it, so we confirm first.
  const gameInProgress = Boolean(
    gameState && !gameState.game_over && ((gameState.pgn?.length ?? 0) > 0 || gameState.move_number > 0)
  );

  useEffect(() => {
    apiFetch('/api/positions')
      .then((r) => r.json())
      .then((data) => {
        if (data?.error) throw new Error(data.error);
        setCategories(Array.isArray(data?.categories) ? data.categories : []);
        setLoading(false);
      })
      .catch((e) => {
        console.error('Failed to load positions:', e);
        setLoadError('Could not load positions from the board.');
        setLoading(false);
      });
  }, []);

  const sendSetup = useCallback(async (entry: PositionEntry): Promise<void> => {
    setStatus(null);
    try {
      const response = await apiFetch('/api/board/setup-position', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fen: entry.fen, name: prettify(entry.name), hint: entry.hint ?? undefined }),
        requiresAuth: true,
      });

      if (response.status === 401) {
        setLoginError(getStoredCredentials() ? 'Invalid credentials. Please try again.' : undefined);
        setLoginOpen(true);
        return;
      }

      const data = await response.json().catch(() => ({}));
      if (response.ok && data.success) {
        setStatus({ kind: 'success', text: `Set up "${prettify(entry.name)}" on the board.` });
      } else {
        setStatus({ kind: 'error', text: data.error || 'Failed to set up position.' });
      }
    } catch (e) {
      console.error('Failed to set up position:', e);
      setStatus({ kind: 'error', text: 'Network error contacting the board.' });
    }
  }, []);

  const onSelect = (entry: PositionEntry) => {
    setPending(entry);
    if (gameInProgress) {
      setConfirmOpen(true);
    } else {
      void sendSetup(entry);
    }
  };

  const onConfirm = () => {
    setConfirmOpen(false);
    if (pending) void sendSetup(pending);
  };

  const onLoginSuccess = () => {
    setLoginOpen(false);
    setLoginError(undefined);
    if (pending) void sendSetup(pending);
  };

  if (loading) {
    return (
      <div className="page container--lg">
        <div className="loading">Loading positions...</div>
      </div>
    );
  }

  return (
    <>
      <LoginDialog
        isOpen={loginOpen}
        onClose={() => setLoginOpen(false)}
        onSuccess={onLoginSuccess}
        errorMessage={loginError}
      />

      {confirmOpen && pending && (
        <div className="dialog-overlay" onClick={() => setConfirmOpen(false)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>End current game?</h3>
              <button className="dialog-close" onClick={() => setConfirmOpen(false)}>×</button>
            </div>
            <div className="dialog-body">
              <p className="dialog-description">
                A game is in progress. Setting up <strong>{prettify(pending.name)}</strong> will end it.
                The game will be recorded as aborted.
              </p>
            </div>
            <div className="dialog-footer">
              <div className="dialog-footer-right">
                <button type="button" className="btn btn-secondary" onClick={() => setConfirmOpen(false)}>
                  Cancel
                </button>
                <button type="button" className="btn btn-primary" onClick={onConfirm}>
                  End game &amp; set up
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="page container--lg">
        <h2 className="page-title">
          <MenuIcon name="positions" size={24} style={{ verticalAlign: 'text-bottom', marginRight: 8 }} />
          Positions
        </h2>
        <p className="text-muted mb-6">
          Set up a predefined position on the board to practice or analyze.
        </p>

        {status && (
          <Card variant={status.kind === 'success' ? 'primary' : 'danger'} className="mb-6">
            {status.text}
          </Card>
        )}

        {loadError ? (
          <Card variant="danger">{loadError}</Card>
        ) : categories.length === 0 ? (
          <Card variant="muted">No positions are defined.</Card>
        ) : (
          categories.map((category) => (
            <Card key={category.name} className="mb-6">
              <CardHeader title={prettify(category.name)} />
              <div className="positions-grid">
                {category.positions.map((entry) => (
                  <button
                    key={entry.name}
                    type="button"
                    className="position-item"
                    onClick={() => onSelect(entry)}
                    title={entry.fen}
                  >
                    <span className="position-name">{prettify(entry.name)}</span>
                    <span className="position-fen">{entry.fen}</span>
                  </button>
                ))}
              </div>
            </Card>
          ))
        )}
      </div>
    </>
  );
}
