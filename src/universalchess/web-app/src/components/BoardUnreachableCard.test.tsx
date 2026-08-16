// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import type { ConnectionStatus } from '../types/game';
import { useGameStore } from '../stores/gameStore';
import { BoardUnreachableCard } from './BoardUnreachableCard';

/**
 * Guards the page-level "board is gone" stand-in. The Settings load path used
 * to dump a vite.config.ts / run-react setup paragraph and no Retry, so a
 * board that was merely restarting looked like a developer misconfiguration
 * with no way back. How a regression manifests: the title/body mention
 * backend/vite, or Retry/Reload are missing so the only recovery is a
 * manual browser refresh.
 *
 * When the navbar status returns to Connected the same Retry must run without
 * a click: the SPA stays on the error card through a reboot, and the green
 * dot is the signal the board is back. How that regression manifests: the
 * card stays up after connectionStatus flips to connected.
 */

function setConnection(status: ConnectionStatus): void {
  act(() => {
    useGameStore.setState({ connectionStatus: status });
  });
}

beforeEach(() => {
  useGameStore.setState({ connectionStatus: 'disconnected' });
});

afterEach(() => {
  cleanup();
});

describe('BoardUnreachableCard', () => {
  it('explains the outage in plain language and offers Retry and Reload', () => {
    render(<BoardUnreachableCard onRetry={() => {}} onReload={() => {}} />);
    expect(screen.getByRole('alert')).toHaveTextContent(/can't reach the board/i);
    expect(screen.getByText(/off, restarting, or the connection dropped/i)).toBeInTheDocument();
    expect(screen.queryByText(/vite\.config/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/backend/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument();
  });

  it('runs the caller retry and a page reload from the two buttons', () => {
    const onRetry = vi.fn();
    const onReload = vi.fn();
    render(<BoardUnreachableCard onRetry={onRetry} onReload={onReload} />);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
    expect(onReload).toHaveBeenCalledTimes(1);
  });

  it('retries when the navbar connection status becomes connected', () => {
    // Why: the status dot turning green is the same recovery Retry is for, so
    // a reboot should not leave the error card up until the user clicks.
    // Failure: onRetry stays at 0 after disconnected/reconnecting -> connected.
    const onRetry = vi.fn();
    render(<BoardUnreachableCard onRetry={onRetry} />);
    expect(onRetry).not.toHaveBeenCalled();

    setConnection('reconnecting');
    expect(onRetry).not.toHaveBeenCalled();

    setConnection('connected');
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('does not retry on mount when the connection is already connected', () => {
    // Why: a 500 while SSE is still up must not loop Retry. Failure: onRetry
    // is called from the first render because status happens to be connected.
    setConnection('connected');
    const onRetry = vi.fn();
    render(<BoardUnreachableCard onRetry={onRetry} />);
    expect(onRetry).not.toHaveBeenCalled();
  });
});
