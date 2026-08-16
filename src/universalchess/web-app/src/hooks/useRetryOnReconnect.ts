import { useEffect, useRef } from 'react';
import { useGameStore } from '../stores/gameStore';

/**
 * Fire ``onRetry`` when the navbar connection status transitions to connected.
 *
 * Retry is on screen because the board could not be reached. The status dot
 * turning green is the same recovery the button is for, so clicking it after a
 * reboot or brief outage is redundant. Only the transition is observed: a
 * control that mounted while already connected (a 500 while SSE is up) is not
 * retried, which would loop.
 */
export function useRetryOnReconnect(onRetry: () => void): void {
  const connectionStatus = useGameStore((state) => state.connectionStatus);
  const previousRef = useRef(connectionStatus);

  useEffect(() => {
    const previous = previousRef.current;
    previousRef.current = connectionStatus;
    if (previous !== 'connected' && connectionStatus === 'connected') {
      onRetry();
    }
  }, [connectionStatus, onRetry]);
}
