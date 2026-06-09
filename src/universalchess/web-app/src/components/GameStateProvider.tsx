import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';
import { useGameStore } from '../stores/gameStore';
import type { GameState } from '../types/game';
import { MoveBanner } from './MoveBanner';
import { buildApiUrl } from '../utils/api';

/**
 * Global SSE connection manager.
 * Maintains connection to /events and updates the game store.
 * Shows banner notifications for new moves when not on the live board.
 */
export function GameStateProvider({ children }: { children: React.ReactNode }) {
  const eventSourceRef = useRef<EventSource | null>(null);
  const lastPgnRef = useRef<string>('');
  const lastGameIdRef = useRef<number | null>(null);
  const isInitializedRef = useRef(false);
  const isOnLiveBoardRef = useRef(false);
  const { setGameState, setConnectionStatus } = useGameStore();
  const { toast, showToast, hideToast } = useGameStore();
  const location = useLocation();

  // Update ref when location changes
  isOnLiveBoardRef.current = location.pathname === '/';

  // Hide banner when navigating to live board
  useEffect(() => {
    if (location.pathname === '/' && toast) {
      hideToast();
    }
  }, [location.pathname, toast, hideToast]);

  useEffect(() => {
    // Client-driven reconnection. The native EventSource only auto-retries while
    // it is CONNECTING; once it transitions to CLOSED (server returned an error
    // or was down during a restart) it never retries, which previously left the
    // UI stuck on "reconnecting" until a manual page reload. We close the dead
    // source and reconnect with capped exponential backoff instead.
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let reconnectAttempts = 0;
    let disposed = false;

    const clearReconnectTimer = () => {
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    };

    const scheduleReconnect = () => {
      if (disposed || reconnectTimer !== null) return;
      // 1s, 2s, 4s ... capped at 10s so a long outage doesn't hammer the board.
      const delay = Math.min(1000 * 2 ** reconnectAttempts, 10000);
      reconnectAttempts += 1;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };

    const connect = () => {
      if (disposed) return;
      clearReconnectTimer();
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
      }

      setConnectionStatus('reconnecting');
      const eventsUrl = buildApiUrl('/events');
      const es = new EventSource(eventsUrl);
      eventSourceRef.current = es;

      es.onopen = () => {
        console.log('[SSE] Connected');
        reconnectAttempts = 0;
        clearReconnectTimer();
        setConnectionStatus('connected');
      };

      es.onmessage = (event) => {
        try {
          const state: GameState = JSON.parse(event.data);
          setGameState(state);

          const prevPgn = lastPgnRef.current;
          const prevGameId = lastGameIdRef.current;
          
          lastPgnRef.current = state.pgn || '';
          lastGameIdRef.current = state.game_id;

          // Skip banner on initial load
          if (!isInitializedRef.current) {
            isInitializedRef.current = true;
            return;
          }

          // Check if this is a new move (not on live board)
          if (!isOnLiveBoardRef.current) {
            const isNewGame = state.game_id !== prevGameId;
            const currentPgn = state.pgn || '';
            // Detect new move by comparing PGN - if it got longer, there's a new move
            const isNewMove = currentPgn.length > prevPgn.length && currentPgn !== prevPgn;

            if (isNewMove || (isNewGame && state.move_number > 0)) {
              // Show banner for the new move
              const lastMove = extractLastMove(state.pgn);
              if (lastMove) {
                // Determine if white or black moved based on whose turn it is now
                // If it's white's turn now, black just moved. If black's turn, white just moved.
                const whiteJustMoved = state.turn === 'b';
                showToast({
                  move: lastMove,
                  moveNumber: state.move_number,
                  white: state.white,
                  black: state.black,
                  isWhiteMove: whiteJustMoved,
                });
              }
            }
          }
        } catch (e) {
          console.error('[SSE] Failed to parse game state:', e);
        }
      };

      es.onerror = () => {
        setConnectionStatus('reconnecting');
        // Only drive our own reconnect once the source has given up (CLOSED).
        // While CONNECTING the browser is still retrying on its own, so leave it.
        if (es.readyState === EventSource.CLOSED) {
          console.log('[SSE] Connection closed, scheduling reconnect');
          es.close();
          scheduleReconnect();
        }
      };
    };

    connect();

    // Reconnect promptly when the tab is refocused or the network returns,
    // instead of waiting out the current backoff delay.
    const handleWake = () => {
      if (disposed) return;
      const es = eventSourceRef.current;
      if (!es || es.readyState === EventSource.CLOSED) {
        reconnectAttempts = 0;
        clearReconnectTimer();
        connect();
      }
    };
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') handleWake();
    };
    window.addEventListener('online', handleWake);
    document.addEventListener('visibilitychange', handleVisibility);

    return () => {
      disposed = true;
      clearReconnectTimer();
      window.removeEventListener('online', handleWake);
      document.removeEventListener('visibilitychange', handleVisibility);
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
    };
  }, [setGameState, setConnectionStatus, showToast]);

  return (
    <>
      {toast && <MoveBanner toast={toast} onDismiss={hideToast} />}
      {children}
    </>
  );
}

/**
 * Extract the last move from PGN string.
 */
function extractLastMove(pgn: string): string | null {
  if (!pgn) return null;
  
  // Remove comments and variations
  const cleaned = pgn
    .replace(/\{[^}]*\}/g, '')
    .replace(/\([^)]*\)/g, '')
    .trim();
  
  // Split by whitespace and find last move
  const tokens = cleaned.split(/\s+/).filter(t => t.length > 0);
  
  // Find last token that looks like a move (not a result or move number)
  for (let i = tokens.length - 1; i >= 0; i--) {
    const token = tokens[i];
    // Skip results
    if (['1-0', '0-1', '1/2-1/2', '*'].includes(token)) continue;
    // Skip move numbers
    if (/^\d+\.+$/.test(token)) continue;
    // This should be a move
    return token;
  }
  
  return null;
}

