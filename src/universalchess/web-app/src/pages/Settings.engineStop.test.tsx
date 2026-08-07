// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import type { EngineDefinition } from '../types/game';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Stopping a running install, and resuming or discarding a paused one.
 *
 * Why these tests exist
 * ---------------------
 * A source build can run for an hour, and the page offered no way to end one --
 * the only exit was rebooting the board, which threw away every minute of
 * compilation. Stop keeps the build tree so the work can be picked up later.
 *
 * The paused controls hang off the engine's own record rather than the
 * install-status poll, because that poll describes a single install and several
 * engines can be paused at once. Driving Resume from the poll is what limited the
 * page to one recoverable install and made a newly started install erase the
 * previous engine's paused state.
 *
 * How a regression manifests
 * --------------------------
 * Reading paused state from the status poll instead of the engine record leaves
 * at most one engine resumable, and whichever engine installs last wins. Sending
 * Resume or Discard without the engine name reaches an endpoint that now requires
 * it, so the button 400s and appears dead.
 */

vi.mock('../components/LoginDialog', () => ({
  LoginDialog: ({ isOpen, onSuccess }: { isOpen: boolean; onSuccess: () => void }) =>
    isOpen ? <button data-testid="login-submit" onClick={onSuccess}>login</button> : null,
}));

const ENGINE = 'reckless';
const DISPLAY_NAME = 'Reckless';
const OTHER_ENGINE = 'berserk';
const OTHER_DISPLAY_NAME = 'Berserk';
const ENGINE_REF = 'v2.1.0';
const OTHER_REF = 'v13';
const CREDENTIALS = 'dGVzdGVyOnNlY3JldA=='; // base64 "tester:secret"
const AUTH_STORAGE_KEY = 'universal-chess-auth';

interface RecordedCall {
  url: string;
  method: string;
  body: string | undefined;
  authorization: string | undefined;
}

interface InstallStatus {
  active: boolean;
  installing: boolean;
  engine: string | null;
  display_name: string | null;
  stage: string | null;
  message: string;
  percent: number;
  interrupted: boolean;
  stopped: boolean;
  eta_seconds: number | null;
  result: { success: boolean; error: string | null } | null;
}

const idleStatus: InstallStatus = {
  active: false, installing: false, engine: null, display_name: null, stage: null,
  message: '', percent: 0, interrupted: false, stopped: false, eta_seconds: null,
  result: null,
};

// Reckless mid-build: the state that must offer Stop.
const buildingStatus: InstallStatus = {
  ...idleStatus,
  active: true, installing: true, engine: ENGINE, display_name: DISPLAY_NAME,
  stage: 'building', message: 'Building Reckless: crate 41 of ~120', percent: 61,
  eta_seconds: 1500,
};

function engineDef(overrides: Partial<EngineDefinition> = {}): EngineDefinition {
  return {
    name: ENGINE,
    display_name: DISPLAY_NAME,
    description: 'desc',
    summary: 'summary',
    info_url: '',
    installed: false,
    has_prebuilt: false,
    estimated_install_minutes: 60,
    has_profiles: false,
    profiles_ready: false,
    last_failure: null,
    needs_repair: false,
    can_repair: false,
    missing_net_count: 0,
    supported: true,
    unsupported_reason: null,
    source_installable: true,
    recommended_ref: null,
    installed_ref: null,
    resume_point: null,
    ...overrides,
  };
}

/** A paused install, as GET /api/engines/all reports it. */
function pausedAt(percent: number, ref: string) {
  return {
    ref,
    stage: 'building',
    message: 'Building',
    percent,
    stopped_at: 1_700_000_000,
    reason: 'stopped' as const,
  };
}

class MockEventSource {
  url: string;
  constructor(url: string) { this.url = url; }
  close(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

/** Stub fetch: reads succeed, engine POSTs answer `postStatus` and are recorded. */
function mockFetch(
  status: InstallStatus,
  engines: EngineDefinition[],
  postStatus = 200,
) {
  const calls: RecordedCall[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    const headers = (init?.headers ?? {}) as Record<string, string>;
    // Reads are recorded as well as writes so a test can assert that an action
    // refetched the engine list, which is what makes a retired resume point
    // reach the card.
    calls.push({
      url, method,
      body: init?.body as string | undefined,
      authorization: headers['Authorization'],
    });
    if (method === 'POST') {
      return jsonResponse({ success: postStatus < 400 }, postStatus);
    }

    if (url === '/api/menu-schema') return jsonResponse(menuSchemaFixture);
    if (url === '/api/settings') {
      return jsonResponse({
        PlayerOne: { type: 'human', name: '', engine: ENGINE, elo: 'Default', hand_brain_mode: 'normal', account: '' },
        PlayerTwo: { type: 'engine', name: '', engine: ENGINE, elo: 'Default', hand_brain_mode: 'normal', account: '' },
        game: { time_control: '0', analysis_mode: 'True', analysis_engine: ENGINE, ponder: 'False', chess960: '', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
        lichess: { api_token: '', range: '', username: '' },
        sound: {}, system: { inactivity_timeout: '900' }, DATABASE: { database_uri: '' },
      });
    }
    if (url === '/api/accounts') return jsonResponse({ accounts: [] });
    if (url === '/api/engines/all') return jsonResponse(engines);
    if (url === '/api/sprites') return jsonResponse(['default']);
    if (url === '/api/agents') return jsonResponse({ agents: [] });
    if (url === '/api/engines/status') return jsonResponse(status);
    if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
    if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', MockEventSource);
  return calls;
}

const postsTo = (calls: RecordedCall[], url: string) =>
  calls.filter((c) => c.url === url && c.method === 'POST');
const getsTo = (calls: RecordedCall[], url: string) =>
  calls.filter((c) => c.url === url && c.method === 'GET');

function renderSettings() {
  return render(
    <MemoryRouter initialEntries={['/settings/engines']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

async function cardFor(displayName: string): Promise<HTMLElement> {
  const title = await screen.findByText(displayName);
  const card = title.closest('.engine-card');
  if (!card) throw new Error(`Engine card for ${displayName} not found`);
  return card as HTMLElement;
}

async function clickInCard(displayName: string, name: RegExp) {
  const card = await cardFor(displayName);
  fireEvent.click(await within(card).findByRole('button', { name }));
}

describe('stopping a running install', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    localStorage.clear();
    localStorage.setItem(AUTH_STORAGE_KEY, CREDENTIALS);
    vi.stubGlobal('confirm', () => true);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it('offers Stop while the install is running', async () => {
    // Why: an hour-long build with no stop control is the reported problem. The
    // control has to appear on the card that is actually building, next to the
    // progress bar the user is watching.
    // Regression: no Stop button is rendered and the only way out is a reboot.
    mockFetch(buildingStatus, [engineDef()]);
    renderSettings();

    const card = await cardFor(DISPLAY_NAME);
    expect(await within(card).findByRole('button', { name: /^stop$/i })).toBeInTheDocument();
  });

  it('sends an authenticated stop request', async () => {
    // Why: stopping another user's build is destructive, so the server requires
    // credentials. Regression: the POST goes out with no Authorization header and
    // the board rejects an action the user is entitled to perform.
    const calls = mockFetch(buildingStatus, [engineDef()]);
    renderSettings();

    await clickInCard(DISPLAY_NAME, /^stop$/i);

    await waitFor(() => expect(postsTo(calls, '/api/engines/stop')).toHaveLength(1));
    expect(postsTo(calls, '/api/engines/stop')[0].authorization).toBe(`Basic ${CREDENTIALS}`);
  });

  it('does not offer Stop when nothing is installing', async () => {
    // Why: the button is driven by the live status, and an idle card showing Stop
    // would post against no install and 400. Regression: Stop renders
    // unconditionally rather than only for the engine currently building.
    mockFetch(idleStatus, [engineDef()]);
    renderSettings();

    const card = await cardFor(DISPLAY_NAME);
    expect(within(card).queryByRole('button', { name: /^stop$/i })).not.toBeInTheDocument();
  });
});

describe('resuming and discarding paused installs', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    localStorage.clear();
    localStorage.setItem(AUTH_STORAGE_KEY, CREDENTIALS);
    vi.stubGlobal('confirm', () => true);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it('shows how far a paused install got', async () => {
    // Why: the percent is the user's only basis for deciding between resuming and
    // discarding -- it says how much preserved work is at stake. It comes from the
    // engine record because the install-status poll has long since moved on to
    // whatever ran next.
    // Regression: the card shows a paused install with no progress, or reverts to
    // a plain Install button and strands the preserved tree.
    mockFetch(idleStatus, [engineDef({ resume_point: pausedAt(61, ENGINE_REF) })]);
    renderSettings();

    const card = await cardFor(DISPLAY_NAME);
    // Both the action and the explanation carry the percent: the button so the
    // choice is legible on its own, the note so the stake is stated in words.
    expect(within(card).getByRole('button', { name: /^resume/i })).toHaveAccessibleName(/61%/);
    expect(within(card).getByText(/stopped at 61%/i)).toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: /^install$/i })).not.toBeInTheDocument();
  });

  it('resumes the engine named on the card', async () => {
    // Why: resume is engine-scoped now that several installs can be paused. The
    // body must name the engine or the server cannot tell which tree to continue.
    // Regression: an empty body 400s, so the button silently does nothing.
    const calls = mockFetch(idleStatus, [engineDef({ resume_point: pausedAt(61, ENGINE_REF) })]);
    renderSettings();

    await clickInCard(DISPLAY_NAME, /^resume/i);

    await waitFor(() => expect(postsTo(calls, '/api/engines/resume')).toHaveLength(1));
    const call = postsTo(calls, '/api/engines/resume')[0];
    expect(JSON.parse(call.body ?? '{}')).toEqual({ engine: ENGINE });
    expect(call.authorization).toBe(`Basic ${CREDENTIALS}`);
  });

  it('confirms before discarding the preserved work', async () => {
    // Why: discard deletes a build tree that may represent an hour of compiling,
    // and it cannot be undone. Regression: dropping the confirmation makes a
    // misclick next to Resume destroy the work the user was about to continue.
    const confirmSpy = vi.fn(() => false);
    vi.stubGlobal('confirm', confirmSpy);
    const calls = mockFetch(idleStatus, [engineDef({ resume_point: pausedAt(61, ENGINE_REF) })]);
    renderSettings();

    await clickInCard(DISPLAY_NAME, /^discard/i);

    expect(confirmSpy).toHaveBeenCalled();
    expect(postsTo(calls, '/api/engines/discard')).toHaveLength(0);
  });

  it('discards the engine named on the card once confirmed', async () => {
    // Why: same engine-scoping as resume; an unnamed discard cannot identify the
    // tree. Regression: the request omits the engine and 400s, leaving the tree.
    const calls = mockFetch(idleStatus, [engineDef({ resume_point: pausedAt(61, ENGINE_REF) })]);
    renderSettings();

    await clickInCard(DISPLAY_NAME, /^discard/i);

    await waitFor(() => expect(postsTo(calls, '/api/engines/discard')).toHaveLength(1));
    expect(JSON.parse(postsTo(calls, '/api/engines/discard')[0].body ?? '{}'))
      .toEqual({ engine: ENGINE });
  });

  it('drops the paused controls and note once that engine is building again', async () => {
    // Why: this is the reported bug. An engine that is building is not paused, so
    // its card must show the live install and nothing else. The two states can
    // legitimately be seen together by a client that has not refetched the engine
    // list -- the status poll runs every few seconds, the list does not -- so the
    // card resolves the contradiction in favour of the live status rather than
    // relying on the fetches arriving in a particular order.
    // Regression: "Stopped at 61%" and a dead Resume button sit beside the
    // progress bar for the whole rebuild, saying the install is paused while it
    // visibly runs.
    mockFetch(buildingStatus, [engineDef({ resume_point: pausedAt(61, ENGINE_REF) })]);
    renderSettings();

    const card = await cardFor(DISPLAY_NAME);
    expect(await within(card).findByRole('button', { name: /^stop$/i })).toBeInTheDocument();
    expect(within(card).queryByText(/stopped at/i)).not.toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: /^resume/i })).not.toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: /^discard/i })).not.toBeInTheDocument();
  });

  it('refetches the engine list after resuming', async () => {
    // Why: resuming retires that engine's resume point on the board, and the
    // paused controls are rendered from the engine list -- the same reason discard
    // refetches. Without it the card holds the stale record until the install
    // ends, so any moment the live status is not yet active (the gap between the
    // resume POST and the first poll that sees the build) redisplays the paused
    // state.
    // Regression: no second GET of /api/engines/all follows the resume.
    const calls = mockFetch(idleStatus, [engineDef({ resume_point: pausedAt(61, ENGINE_REF) })]);
    renderSettings();
    await cardFor(DISPLAY_NAME);
    const before = getsTo(calls, '/api/engines/all').length;

    await clickInCard(DISPLAY_NAME, /^resume/i);

    await waitFor(() => expect(postsTo(calls, '/api/engines/resume')).toHaveLength(1));
    await waitFor(() =>
      expect(getsTo(calls, '/api/engines/all').length).toBeGreaterThan(before)
    );
  });

  it('renders independent controls for two paused engines', async () => {
    // Why: this is the multi-install requirement at the UI. Both cards must show
    // their own progress and act on their own engine.
    // Regression: paused state read from the shared install-status poll shows the
    // controls on only one card, or resuming one dispatches the other.
    const calls = mockFetch(idleStatus, [
      engineDef({ resume_point: pausedAt(61, ENGINE_REF) }),
      engineDef({
        name: OTHER_ENGINE, display_name: OTHER_DISPLAY_NAME,
        estimated_install_minutes: 15,
        resume_point: pausedAt(22, OTHER_REF),
      }),
    ]);
    renderSettings();

    expect(within(await cardFor(DISPLAY_NAME)).getByRole('button', { name: /^resume/i }))
      .toHaveAccessibleName(/61%/);
    expect(within(await cardFor(OTHER_DISPLAY_NAME)).getByRole('button', { name: /^resume/i }))
      .toHaveAccessibleName(/22%/);

    await clickInCard(OTHER_DISPLAY_NAME, /^resume/i);

    await waitFor(() => expect(postsTo(calls, '/api/engines/resume')).toHaveLength(1));
    expect(JSON.parse(postsTo(calls, '/api/engines/resume')[0].body ?? '{}'))
      .toEqual({ engine: OTHER_ENGINE });
  });

  it('does not offer Resume while another install is running', async () => {
    // Why: only one engine can build at a time and the server answers 409. A live
    // Resume button during another build is a control that cannot work.
    // Regression: the button stays enabled, the request 409s, and the user is
    // shown a conflict error for pressing something the page offered.
    const otherEngineBuilding: InstallStatus = {
      ...buildingStatus,
      engine: OTHER_ENGINE, display_name: OTHER_DISPLAY_NAME,
      message: 'Building Berserk',
    };
    mockFetch(otherEngineBuilding, [
      engineDef({ name: OTHER_ENGINE, display_name: OTHER_DISPLAY_NAME }),
      engineDef({ resume_point: pausedAt(61, ENGINE_REF) }),
    ]);
    renderSettings();

    const card = await cardFor(DISPLAY_NAME);
    expect(await within(card).findByRole('button', { name: /^resume/i })).toBeDisabled();
  });
});
