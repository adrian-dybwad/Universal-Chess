import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router';
import { useGameStore } from '../stores/gameStore';
import { useSettingsStore } from '../stores/settingsStore';
import type { GameState } from '../types/game';
import { MoveBanner } from './MoveBanner';
import { buildApiUrl } from '../utils/api';
import { publishSseEvent, type SseEventPayload } from '../utils/sseBus';

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
  const { setGameState, setConnectionStatus, setBattery, setClock } = useGameStore();
  const { toast, showToast, hideToast } = useGameStore();
  const location = useLocation();

  // Seed the shared settings store once on mount so every screen (including ones
  // that never open Settings, e.g. LiveBoard reading notation/clock preset) has
  // settings available and stays live via the settings_changed handler below.
  useEffect(() => {
    void useSettingsStore.getState().load();
  }, []);

  // Where the user is, for the SSE handler to read when a move arrives: it must
  // not announce a move the user is already watching. It lives in a ref because
  // the subscription below is deliberately not re-created on navigation, and it
  // is written from an effect rather than during render because a render must not
  // have side effects. The live board is /board (the root path is the welcome
  // page).
  //
  // The handler reads this only from a network callback, never while rendering,
  // so the commit-to-effect gap is not reachable from there; a move that did slip
  // through it is caught by the effect below, which clears the banner on arrival
  // at /board.
  useEffect(() => {
    isOnLiveBoardRef.current = location.pathname === '/board';
  }, [location.pathname]);

  // Hide banner when navigating to live board
  useEffect(() => {
    if (location.pathname === '/board' && toast) {
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
          const data = JSON.parse(event.data);

          // Fan every message out on the shared bus so other components (navbar
          // Cast button, Connectivity cards) consume this one connection instead
          // of opening their own EventSource. Over HTTP/1.1 each extra stream
          // permanently holds one of the browser's ~6 per-host connection slots,
          // so redundant streams froze the site once a second tab was open.
          if (typeof data?.type === 'string') {
            publishSseEvent(data.type, data as SseEventPayload);
          }

          // The /events stream multiplexes several event types over one
          // connection. Battery updates feed the navbar indicator; other
          // non-game events (bt_status, chromecast_state, pairing) are handled
          // by the pages that need them. Only game_state messages carry board
          // state, so anything else must not be written into gameState.
          if (data.type === 'battery_status') {
            setBattery({
              battery_level: data.battery_level ?? null,
              battery_percent: data.battery_percent ?? null,
              charger_connected: Boolean(data.charger_connected),
            });
            return;
          }
          // Live clock ticks feed the LiveBoard countdown, which interpolates
          // the active side locally between these once-per-second snapshots.
          if (data.type === 'clock_status') {
            setClock({
              white_time: data.white_time ?? null,
              black_time: data.black_time ?? null,
              active_color: data.active_color ?? null,
              is_running: Boolean(data.is_running),
              is_paused: Boolean(data.is_paused),
              timed_mode: Boolean(data.timed_mode),
              synced_at: data.synced_at ?? null,
            });
            return;
          }
          // A settings change on the board or in another browser tab arrives as
          // a payload-less settings_changed event. Re-pull /api/settings into the
          // shared store so every screen (Settings form, notation, clock preset)
          // reflects it live. This is the single place the whole app consumes the
          // event -- individual pages read the store instead of opening their own
          // EventSource.
          if (data.type === 'settings_changed') {
            void useSettingsStore.getState().refresh();
            return;
          }
          if (data.type && data.type !== 'game_state') {
            return;
          }

          const state: GameState = data;
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
    // instead of waiting out the current backoff delay. Always tear down and
    // open a fresh EventSource: after a board power cycle Safari often leaves
    // the prior source stuck in CONNECTING (or a zombie OPEN), and only
    // reconnecting on CLOSED left the PWA dark until a manual reload. Mid-stream
    // onerror still defers to the browser while readyState is CONNECTING.
    const handleWake = () => {
      if (disposed) return;
      reconnectAttempts = 0;
      clearReconnectTimer();
      connect();
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
  }, [setGameState, setConnectionStatus, setBattery, setClock, showToast]);

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

