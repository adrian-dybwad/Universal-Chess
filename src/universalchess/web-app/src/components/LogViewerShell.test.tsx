// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';
import {
  LogViewerShell,
  type LogFetchResult,
  type LogViewerFetchContext,
} from './LogViewerShell';

type StringLogFetcher = (ctx: LogViewerFetchContext) => Promise<LogFetchResult<string>>;

/**
 * Guards the shared auth/refresh/empty chrome used by the System Event Log and
 * the Original Centaur launch-log viewer.
 *
 * Why: both viewers need the same login-retry, Refresh, error, and empty
 * states. Duplicating that in Settings.tsx already drifted once (loading flags
 * vs set-state-in-effect). A regression here shows as a stuck Refresh button,
 * a missing empty message on 404, or content rendered while an error card is up.
 */

const labels = {
  title: 'Sample log',
  intro: 'Intro copy for the sample log.',
  errorLabel: 'Error:',
  refresh: 'Refresh',
  refreshing: 'Refreshing...',
  none: 'No log yet.',
  loadFailed: 'Failed to load the log.',
  networkError: 'Network error',
};

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('LogViewerShell', () => {
  it('renders fetched content after a successful load', async () => {
    // Why: the shell must hand parsed data to children once the auth-gated
    // fetch succeeds, so Event Log rows and Centaur pre text both appear.
    // How a regression manifests: title shows but children never receive data.
    const fetchLog: StringLogFetcher = async () => ({ kind: 'ok', data: 'hello from log' });

    render(
      <LogViewerShell labels={labels} fetchLog={fetchLog}>
        {(data) => <pre>{data}</pre>}
      </LogViewerShell>,
    );

    expect(await screen.findByText('hello from log')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Sample log' })).toBeInTheDocument();
    expect(screen.getByText('Intro copy for the sample log.')).toBeInTheDocument();
  });

  it('shows the empty message when fetchLog reports absent', async () => {
    // Why: centaur.log 404 and an empty event list both mean "nothing to show",
    // not an error. Support copy depends on the empty state being distinct.
    // How a regression manifests: loadFailed text, or a blank section with no none copy.
    const fetchLog: StringLogFetcher = async () => ({ kind: 'empty' });

    render(
      <LogViewerShell labels={labels} fetchLog={fetchLog}>
        {(data) => <pre>{data}</pre>}
      </LogViewerShell>,
    );

    expect(await screen.findByText('No log yet.')).toBeInTheDocument();
  });

  it('reloads when Refresh is clicked', async () => {
    // Why: after a Centaur crash the user refreshes without leaving the tab;
    // the button must call fetchLog again and replace the body.
    // How a regression manifests: second click does not call fetchLog, or old
    // text remains after a successful reload.
    const fetchLog = vi.fn<StringLogFetcher>()
      .mockResolvedValueOnce({ kind: 'ok', data: 'first' })
      .mockResolvedValueOnce({ kind: 'ok', data: 'second' });
    const user = userEvent.setup();

    render(
      <LogViewerShell labels={labels} fetchLog={fetchLog}>
        {(data) => <pre>{data}</pre>}
      </LogViewerShell>,
    );

    expect(await screen.findByText('first')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(screen.getByText('second')).toBeInTheDocument());
    expect(fetchLog).toHaveBeenCalledTimes(2);
  });

  it('shows the load-failed message when fetchLog reports an error', async () => {
    // Why: a 500 must not look like an empty log; operators need the failure.
    // How a regression manifests: none message, or children rendered with stale data.
    const fetchLog: StringLogFetcher = async () => ({
      kind: 'error',
      message: labels.loadFailed,
    });

    render(
      <LogViewerShell labels={labels} fetchLog={fetchLog}>
        {(data) => <pre>{data}</pre>}
      </LogViewerShell>,
    );

    expect(await screen.findByText(/Failed to load the log/)).toBeInTheDocument();
    expect(screen.queryByText('No log yet.')).not.toBeInTheDocument();
  });
});
