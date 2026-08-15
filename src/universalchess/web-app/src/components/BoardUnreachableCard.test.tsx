// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { BoardUnreachableCard } from './BoardUnreachableCard';

/**
 * Guards the page-level "board is gone" stand-in. The Settings load path used
 * to dump a vite.config.ts / run-react setup paragraph and no Retry, so a
 * board that was merely restarting looked like a developer misconfiguration
 * with no way back. How a regression manifests: the title/body mention
 * backend/vite, or Retry/Reload are missing so the only recovery is a
 * manual browser refresh.
 */

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
});
