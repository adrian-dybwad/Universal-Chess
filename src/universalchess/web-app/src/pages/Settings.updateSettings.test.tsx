// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * Guards that the System tab's update Channel + Auto Download controls are
 * rendered from the SHARED catalog nodes (updates.channel / updates.auto) -- the
 * same nodes the board's Updates menu uses -- rather than a web-only duplicate
 * (the former field.system.update_channel / field.system.auto_update, now
 * removed). The convergence is only correct if these controls:
 *  - carry the shared catalog label/options (Update Channel + stable/nightly,
 *    Auto Download Updates), proving they read the one definition; and
 *  - still write through the dedicated /api/updates/{channel,auto} endpoints
 *    (an `update` store adapter), NOT the generic /api/settings save.
 *
 * How a regression manifests: the old field.system.* labels return (or the
 * control disappears) if the duplicate is reintroduced; or a change routes
 * through /api/settings, so the board is never told to switch channel / auto.
 */

const menuSchema: unknown = menuSchemaFixture;

const idleEngineStatus = {
  active: false, installing: false, engine: null, display_name: null,
  stage: null, message: '', percent: 0, interrupted: false, result: null,
};

interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
}

function jsonResponse(body: unknown, status = 200): JsonResponseLike {
  return { ok: status >= 200 && status < 300, status, json: async () => body, text: async () => JSON.stringify(body) };
}

class MockEventSource {
  url: string;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(url: string) { this.url = url; }
  close(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

function settingsPayload() {
  return {
    PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', ponder: 'False', coach_provider: 'none', coach_id: 'off' },
    lichess: { api_token: '', range: '', username: '' },
    sound: {},
    system: { inactivity_timeout: '900', timezone: 'UTC' },
    DATABASE: { database_uri: '' },
  };
}

// Persisted update status the UpdateManager polls: stable channel, auto-download
// off, nothing to install (so the version readout/actions stay quiet and the
// two settings controls are the focus). `last_check` is null so the baseline
// represents a device that has never checked -- the up-to-date confirmation must
// stay hidden in that state. Tests that exercise the confirmation set last_check.
function baselineUpdateStatus() {
  return {
    channel: 'stable',
    auto_update: false,
    current_version: '2.4.0',
    available_version: null as string | null,
    has_pending_update: false,
    last_check: null as string | null,
    is_checking: false,
    is_downloading: false,
    is_installing: false,
  };
}

// Reassigned per-test (reset in beforeEach) so the shared fetch mock can serve a
// tailored status. The mock reads this at call time, after render, so a test may
// mutate it before rendering to select the state under test.
let updateStatus = baselineUpdateStatus();

// HTTP status the shared mock returns for the on-open POST /api/updates/check.
// 200 is the authenticated success path; a test sets 401 to model the
// unauthenticated device where the check cannot run and the confirmation must
// stay hidden. Reset in beforeEach.
let checkHttpStatus = 200;
// Count of on-open update checks the mock served, so a test can assert that
// opening the page triggers exactly one fresh check.
let checkCount = 0;

interface PostRecord { url: string; body: Record<string, unknown> }
let posts: PostRecord[] = [];

beforeEach(() => {
  posts = [];
  checkHttpStatus = 200;
  checkCount = 0;
  updateStatus = baselineUpdateStatus();
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });

  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload());
    if (url === '/api/settings' && method === 'POST') { posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') }); return jsonResponse({ success: true }); }
    if (url === '/api/updates/status') return jsonResponse(updateStatus);
    if (url === '/api/updates/check' && method === 'POST') {
      checkCount += 1;
      return jsonResponse(
        checkHttpStatus === 200
          ? { update_available: !!updateStatus.available_version, version: updateStatus.available_version }
          : { error: 'unauthorized' },
        checkHttpStatus,
      );
    }
    if (url === '/api/updates/channel' && method === 'POST') {
      posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') });
      return jsonResponse({ success: true });
    }
    if (url === '/api/updates/auto' && method === 'POST') {
      posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') });
      return jsonResponse({ success: true });
    }
    if (url === '/api/system/hardware') return jsonResponse({}, 503);
    if (url === '/api/system/stats') return jsonResponse({}, 503);
    if (url === '/api/accounts') return jsonResponse({ accounts: [] });
    if (url === '/api/engines/all') return jsonResponse([]);
    if (url === '/api/sprites') return jsonResponse(['default']);
    if (url === '/api/agents') return jsonResponse({ agents: [] });
    if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
    if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
    if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', MockEventSource);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderSystemTab() {
  return render(
    <MemoryRouter initialEntries={['/settings/system']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

// The System tab has more than one switch (other settings render toggles too),
// so scope to the Auto Download row by its label rather than the ambiguous
// page-wide switch role. The catalog Toggle renders its label as sibling text
// within the same .form-row as the switch button.
function autoDownloadSwitch(): HTMLElement {
  const label = screen.getByText('Auto Download Updates');
  const row = label.closest('.form-row');
  if (!row) throw new Error('Auto Download Updates row not found');
  return within(row as HTMLElement).getByRole('switch');
}

describe('System tab update settings (catalog-driven)', () => {
  it('renders Channel + Auto Download from the shared catalog nodes', async () => {
    renderSystemTab();
    // Label "Update Channel" and the stable/nightly options come from the shared
    // updates.channel node + update_channel option set. If the web-only duplicate
    // were reintroduced the label would be identical, but the point of the test is
    // that a SINGLE node now backs it -- guarded together with the endpoint test
    // below (the duplicate wrote via the same endpoints, so both must hold).
    const channel = (await screen.findByLabelText('Update Channel')) as HTMLSelectElement;
    expect(channel.value).toBe('stable');
    const values = Array.from(channel.options).map((o) => o.value);
    expect(values).toEqual(['stable', 'nightly']);

    // Reflects auto_update=false from the polled status.
    const auto = autoDownloadSwitch();
    expect(auto).toHaveAttribute('aria-checked', 'false');
  });

  it('routes a channel change through /api/updates/channel, not /api/settings', async () => {
    renderSystemTab();
    const channel = (await screen.findByLabelText('Update Channel')) as HTMLSelectElement;
    fireEvent.change(channel, { target: { value: 'nightly' } });

    await waitFor(() => {
      expect(posts.some((p) => p.url === '/api/updates/channel')).toBe(true);
    });
    const post = posts.find((p) => p.url === '/api/updates/channel');
    expect(post?.body).toEqual({ channel: 'nightly' });
    // Must not leak into the generic settings save (which the board ignores for
    // channel switching).
    expect(posts.some((p) => p.url === '/api/settings')).toBe(false);
  });

  it('routes an auto-download toggle through /api/updates/auto', async () => {
    renderSystemTab();
    // Wait for the control to reflect loaded status before toggling.
    await screen.findByLabelText('Update Channel');
    const auto = autoDownloadSwitch();
    fireEvent.click(auto);

    await waitFor(() => {
      expect(posts.some((p) => p.url === '/api/updates/auto')).toBe(true);
    });
    const post = posts.find((p) => p.url === '/api/updates/auto');
    expect(post?.body).toEqual({ enabled: true });
    expect(posts.some((p) => p.url === '/api/settings')).toBe(false);
  });
});

describe('UpdateManager up-to-date confirmation', () => {
  it('runs exactly one fresh update check when the page is opened', async () => {
    // Why: the confirmation is only trustworthy if it reflects a check made now,
    // not a stale historical one, so opening the page must trigger a fresh check.
    // A regression that drops the on-open check (or fires it from the 10s status
    // poll instead) makes checkCount 0 (or grow past 1), so this pins the exact
    // "one check per open" contract.
    renderSystemTab();
    await waitFor(() => expect(checkCount).toBe(1));
    // The recurring poll refreshes status only; it must not issue more checks.
    await screen.findByText("You're running the latest version.");
    expect(checkCount).toBe(1);
  });

  it('skips the on-open check when the persisted last check is recent', async () => {
    // Why: the freshness window rate-limits the upstream release check so
    // re-opening the page (or navigating back) within a few minutes reuses the
    // prior result instead of hitting the API again. A recent last_check must
    // yield the confirmation with zero network checks. A regression removing the
    // window would issue a check here, pushing checkCount to 1.
    updateStatus.last_check = new Date().toISOString();
    renderSystemTab();
    expect(
      await screen.findByText("You're running the latest version.")
    ).toBeInTheDocument();
    expect(checkCount).toBe(0);
  });

  it('runs the on-open check when the persisted last check is stale', async () => {
    // Why: guards the other side of the window -- a check older than the
    // freshness window (here 10 minutes vs the 5-minute limit) must trigger a
    // fresh check so a days-old "latest version" claim cannot persist. A
    // regression widening or dropping the staleness path leaves checkCount 0.
    updateStatus.last_check = new Date(Date.now() - 10 * 60 * 1000).toISOString();
    renderSystemTab();
    await waitFor(() => expect(checkCount).toBe(1));
  });

  it('shows the up-to-date message after the on-open check completes with no update', async () => {
    // Why: the message must confirm the system *looked now* and found nothing.
    // last_check is null here (the mock does not simulate the backend stamping
    // it), proving the confirmation is gated on this session's completed check
    // -- not on a pre-existing last_check. A regression reverting to the old
    // last_check gate would hide the message despite a successful on-open check.
    renderSystemTab();
    expect(
      await screen.findByText("You're running the latest version.")
    ).toBeInTheDocument();
  });

  it('hides the up-to-date message when the on-open check cannot run (unauthenticated)', async () => {
    // Why: without a completed check the page cannot honestly claim "latest
    // version". A 401 on the on-open check (unauthenticated device) must leave
    // the confirmation hidden. A regression that claims up-to-date without a
    // successful check surfaces the message here.
    checkHttpStatus = 401;
    renderSystemTab();
    // The check button returns to its idle label once the on-open check settles.
    await screen.findByRole('button', { name: 'Check for Updates' });
    expect(
      screen.queryByText("You're running the latest version.")
    ).not.toBeInTheDocument();
  });

  it('hides the up-to-date message when an update is available', async () => {
    // Why: the confirmation must be mutually exclusive with the "update
    // available" card. A regression in the derived condition would show both the
    // "Update Available" prompt and a contradictory "latest version" line.
    updateStatus.available_version = '2.5.0';
    renderSystemTab();
    expect(await screen.findByText('Update Available: v2.5.0')).toBeInTheDocument();
    expect(
      screen.queryByText("You're running the latest version.")
    ).not.toBeInTheDocument();
  });
});
