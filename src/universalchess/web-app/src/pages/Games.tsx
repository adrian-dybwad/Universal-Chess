import { useState, useEffect, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { Button, Card, Badge } from '../components/ui';
import { LoginDialog } from '../components/LoginDialog';
import type { GameRecord } from '../types/game';
import { apiFetch, getStoredCredentials } from '../utils/api';
import './Games.css';

/**
 * Games history page.
 */
export function Games() {
  const [games, setGames] = useState<GameRecord[]>([]);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [expandedPgn, setExpandedPgn] = useState<Record<number, string>>({});
  const [loginDialogOpen, setLoginDialogOpen] = useState(false);
  const [loginError, setLoginError] = useState<string | undefined>(undefined);
  const [pendingDeleteId, setPendingDeleteId] = useState<number | null>(null);

  const fetchGames = useCallback(async () => {
    setLoading(true);
    try {
      const response = await apiFetch(`/getgames/${page}`);
      const data = await response.json();
      const gameList = Object.values(data) as GameRecord[];
      setGames(gameList);
    } catch (e) {
      console.error('Failed to fetch games:', e);
      setGames([]);
    } finally {
      setLoading(false);
    }
  }, [page]);

  useEffect(() => {
    fetchGames();
  }, [fetchGames]);

  const togglePgn = async (gameId: number) => {
    if (expandedPgn[gameId]) {
      setExpandedPgn((prev) => {
        const next = { ...prev };
        delete next[gameId];
        return next;
      });
      return;
    }

    try {
      const response = await apiFetch(`/getpgn/${gameId}`);
      const pgn = await response.text();
      setExpandedPgn((prev) => ({ ...prev, [gameId]: pgn }));
    } catch (e) {
      console.error('Failed to fetch PGN:', e);
    }
  };

  // Deleting a game mutates the database and requires authentication via a
  // POST. On 401 the login dialog is shown and the delete is retried once the
  // user authenticates (pendingDeleteId drives the retry).
  const deleteGame = async (gameId: number, skipConfirm = false) => {
    if (!skipConfirm && !confirm('Delete this game? This cannot be undone.')) return;
    try {
      const response = await apiFetch(`/deletegame/${gameId}`, {
        method: 'POST',
        requiresAuth: true,
      });

      if (response.status === 401) {
        setLoginError(getStoredCredentials() ? 'Invalid credentials. Please try again.' : undefined);
        setPendingDeleteId(gameId);
        setLoginDialogOpen(true);
        return;
      }

      if (!response.ok) {
        console.error('Failed to delete game:', response.status);
        return;
      }

      fetchGames();
    } catch (e) {
      console.error('Failed to delete game:', e);
    }
  };

  const handleLoginSuccess = () => {
    setLoginDialogOpen(false);
    setLoginError(undefined);
    const retryId = pendingDeleteId;
    setPendingDeleteId(null);
    if (retryId !== null) {
      deleteGame(retryId, true);
    }
  };

  return (
    <div className="page container--lg">
      <div className="page-header">
        <h1 className="page-title">Game History</h1>
        <div className="flex gap-4 items-center">
          <Button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1}>
            ◀ Previous
          </Button>
          <span className="text-muted">Page {page}</span>
          <Button onClick={() => setPage((p) => p + 1)} disabled={games.length === 0}>
            Next ▶
          </Button>
        </div>
      </div>

      {loading ? (
        <div className="loading">Loading games...</div>
      ) : games.length === 0 ? (
        <div className="empty">No games found</div>
      ) : (
        <div className="flex flex-col gap-4">
          {games.map((game) => (
            <Card key={game.id}>
              <div className="game-header">
                <div className="game-players">
                  <strong>{game.white || 'Player'}</strong>
                  <span className="text-muted">(W)</span>
                  <span className="game-vs">vs</span>
                  <strong>{game.black || 'Player'}</strong>
                  <span className="text-muted">(B)</span>
                </div>
                {game.result && <Badge>{game.result}</Badge>}
              </div>

              <div className="game-meta">
                {game.created_at && (
                  <span>{new Date(game.created_at).toLocaleDateString()}</span>
                )}
                {game.source && <span>{game.source}</span>}
              </div>

              {expandedPgn[game.id] && (
                <pre className="game-pgn">{expandedPgn[game.id]}</pre>
              )}

              <div className="flex gap-2 mt-4">
                <Button size="sm" onClick={() => togglePgn(game.id)}>
                  {expandedPgn[game.id] ? 'Hide PGN' : 'Show PGN'}
                </Button>
                <Link to={`/analyze/${game.id}`}>
                  <Button size="sm" variant="primary">Analyze</Button>
                </Link>
                <Button size="sm" variant="danger" onClick={() => deleteGame(game.id)}>
                  Delete
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      <LoginDialog
        isOpen={loginDialogOpen}
        onClose={() => {
          setLoginDialogOpen(false);
          setPendingDeleteId(null);
        }}
        onSuccess={handleLoginSuccess}
        errorMessage={loginError}
      />
    </div>
  );
}
