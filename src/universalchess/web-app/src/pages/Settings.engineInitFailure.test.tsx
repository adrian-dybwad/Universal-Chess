// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import type { EngineDefinition } from '../types/game';
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * Guards the engine card for a binary that installed but will not start.
 *
 * Why these tests exist
 * ---------------------
 * A user reported CT800 rendering a green "Installed" badge while the profile
 * editor on the same page said the engine was not installed. Both came from the
 * card asking only whether the binary file exists, which stays true forever once
 * an install writes it. `profiles_ready` is the independent signal (derived
 * server-side from the seeded .uci, with no engine launched), and `last_failure`
 * carries the reason the initialization failed.
 *
 * The notice sits below the engine description with its technical detail behind
 * a collapsed toggle, so the card stays readable but the exact token a
 * maintainer needs is one click away and screenshottable. Dismissal
 * acknowledges the notice only -- the badge reflects the engine's current state
 * and must survive it.
 *
 * How a regression manifests
 * --------------------------
 * - If the badge branch is dropped or ordered after `installed`, the card shows
 *   plain "Installed" again and the badge assertions fail.
 * - If `last_failure` is not rendered, the user is told the engine is broken with
 *   no indication of what to do, and the reason assertion fails.
 * - If dismissal hides the badge as well as the notice, the card claims a broken
 *   engine is healthy -- the original bug, reintroduced by the fix for it.
 * - If the profile-editor button is still offered, the user is sent into an
 *   editor that cannot load, which is the loop the original report described.
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

function engine(overrides: Partial<EngineDefinition>): EngineDefinition {
  return {
    name: 'placeholder',
    display_name: 'Placeholder',
    description: 'desc',
    summary: 'summary',
    installed: true,
    has_prebuilt: false,
    install_time: null,
    has_profiles: true,
    profiles_ready: true,
    last_failure: null,
    needs_repair: false,
    can_repair: false,
    missing_net_count: 0,
    supported: true,
    unsupported_reason: null,
    source_installable: true,
    recommended_ref: null,
    installed_ref: null,
    ...overrides,
  };
}

const FAILED_AT = 1780000000;

// CT800 as reported: the binary is installed and executable (installed=true,
// needs_repair=false, nothing missing) but the post-install probe failed, so no
// .uci ladder was ever written and the reason was recorded as an architecture
// mismatch.
const ct800WontStart = engine({
  name: 'ct800',
  display_name: 'CT800',
  installed: true,
  profiles_ready: false,
  last_failure: {
    phase: 'initialize',
    reason_code: 'incompatible_binary',
    detail: 'OSError ENOEXEC',
    failed_at: FAILED_AT,
    dismissed: false,
  },
});

// The same failure after the user acknowledged it. The engine is still broken,
// so only the notice goes away.
const ct800Dismissed = engine({
  name: 'ct800',
  display_name: 'CT800',
  installed: true,
  profiles_ready: false,
  last_failure: {
    phase: 'initialize',
    reason_code: 'incompatible_binary',
    detail: 'OSError ENOEXEC',
    failed_at: FAILED_AT,
    dismissed: true,
  },
});

// The same engine after a working reinstall: the control case that keeps the
// warning from being applied to every card.
const ct800Healthy = engine({
  name: 'ct800',
  display_name: 'CT800',
  installed: true,
  profiles_ready: true,
  last_failure: null,
});

// An install that failed outright: no binary at all. The card must keep offering
// Install rather than the "installed but broken" treatment.
const arasanInstallFailed = engine({
  name: 'arasan',
  display_name: 'Arasan',
  installed: false,
  has_profiles: false,
  profiles_ready: false,
  last_failure: {
    phase: 'install',
    reason_code: 'build_failed',
    detail: 'CalledProcessError',
    failed_at: FAILED_AT,
    dismissed: false,
  },
});

let dismissPosts: string[];

/**
 * Backs the fetch mock with the state the server actually keeps: dismissal is
 * persisted server-side and read back on the next GET. A static list would make
 * a correct implementation look broken, since the UI deliberately refreshes
 * from the server rather than hiding the notice locally.
 */
function mockFetch(engines: EngineDefinition[]) {
  dismissPosts = [];
  let current = engines;
  const fetchMock = vi.fn(async (url: string): Promise<JsonResponseLike> => {
    const dismissMatch = url.match(/^\/api\/engines\/(.+)\/failure\/dismiss$/);
    if (dismissMatch) {
      const dismissed = decodeURIComponent(dismissMatch[1]);
      dismissPosts.push(dismissed);
      current = current.map((e) =>
        e.name === dismissed && e.last_failure
          ? { ...e, last_failure: { ...e.last_failure, dismissed: true } }
          : e
      );
      return jsonResponse({ success: true });
    }
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings') {
      return jsonResponse({
        PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
        PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
        game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', ponder: 'False', chess960: '', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
        lichess: { api_token: '', range: '', username: '' },
        sound: {}, system: { inactivity_timeout: '900' }, DATABASE: { database_uri: '' },
      });
    }
    if (url === '/api/accounts') return jsonResponse({ accounts: [] });
    if (url === '/api/engines/all') return jsonResponse(current);
    if (url === '/api/sprites') return jsonResponse(['default']);
    if (url === '/api/agents') return jsonResponse({ agents: [] });
    if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
    if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
    if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', MockEventSource);
  return fetchMock;
}

beforeEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderSettings() {
  return render(
    <MemoryRouter initialEntries={['/settings/engines']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

async function findEngineCard(displayName: string): Promise<HTMLElement> {
  const title = await screen.findByText(displayName);
  const card = title.closest('.engine-card');
  if (!card) throw new Error(`Engine card for ${displayName} not found`);
  return card as HTMLElement;
}

describe('Engine card for an engine that installed but will not start', () => {
  it('does not claim the engine is simply Installed', async () => {
    // The exact contradiction from the report: a green "Installed" badge on a
    // card whose engine cannot be launched. The badge must reflect the failure.
    mockFetch([ct800WontStart]);
    renderSettings();
    const card = await findEngineCard('CT800');

    await waitFor(() =>
      expect(within(card).getByText(/profiles unavailable/i)).toBeInTheDocument()
    );
    expect(within(card).queryByText(/^installed$/i)).not.toBeInTheDocument();
  });

  it('explains that the binary is installed but did not start', async () => {
    // "Profiles unavailable" alone leaves no next step. The card must say what
    // failed and name the architecture mismatch behind incompatible_binary.
    mockFetch([ct800WontStart]);
    renderSettings();
    const card = await findEngineCard('CT800');

    await waitFor(() =>
      expect(within(card).getByText(/did not start/i)).toBeInTheDocument()
    );
    expect(within(card).getByText(/architecture/i)).toBeInTheDocument();
  });

  it('suggests uninstalling and reinstalling without asserting a cause', async () => {
    // The remedy must match the button actually on the card -- an installed
    // engine shows "Uninstall", not "Install" -- and must stay a suggestion.
    // The reason codes say what the OS reported, not why: a board whose C
    // toolchain links the wrong startup objects produces the same
    // crashed_at_startup as a genuinely damaged build, and a reinstall does not
    // help there. Wording that promises a fix ("this rebuilds it for your
    // device") is a claim the evidence does not support.
    mockFetch([ct800WontStart]);
    renderSettings();
    const card = await findEngineCard('CT800');

    await waitFor(() =>
      expect(within(card).getByText(/did not start/i)).toBeInTheDocument()
    );
    expect(within(card).getByText(/try uninstalling and reinstalling/i))
      .toBeInTheDocument();
    // The suggestion is only actionable if the control it names is present.
    expect(within(card).getByRole('button', { name: /^uninstall$/i }))
      .toBeInTheDocument();
  });

  it('does not promise that reinstalling will fix the engine', async () => {
    // Guards the specific phrasing regression: an engine can fail to start for
    // reasons a reinstall cannot touch (a broken device toolchain being the one
    // that motivated this). Stating a cause or a guaranteed outcome sends the
    // user in a loop of reinstalls and hides the real fault.
    mockFetch([ct800WontStart]);
    renderSettings();
    const card = await findEngineCard('CT800');

    await waitFor(() =>
      expect(within(card).getByText(/did not start/i)).toBeInTheDocument()
    );
    expect(within(card).queryByText(/rebuild it for this device/i))
      .not.toBeInTheDocument();
    expect(within(card).queryByText(/build is damaged/i)).not.toBeInTheDocument();
  });

  it('does not suggest uninstalling an engine that never installed', async () => {
    // A failed install left no binary, so there is nothing to uninstall and the
    // card offers Install. Repeating the reinstall suggestion here would send
    // the user looking for a button that is not on this card, the same mismatch
    // this change fixes for the initialize case.
    mockFetch([arasanInstallFailed]);
    renderSettings();
    const card = await findEngineCard('Arasan');

    await waitFor(() =>
      expect(within(card).getByText(/could not be installed/i)).toBeInTheDocument()
    );
    expect(within(card).queryByText(/try uninstalling and reinstalling/i))
      .not.toBeInTheDocument();
  });

  it('withholds the profile editor that cannot load', async () => {
    // Offering "Configure profiles" sends the user into an editor that answers
    // with an error -- the loop the report described, where Reset was clicked
    // repeatedly. The card must not present it.
    mockFetch([ct800WontStart]);
    renderSettings();
    const card = await findEngineCard('CT800');

    await waitFor(() =>
      expect(within(card).getByText(/profiles unavailable/i)).toBeInTheDocument()
    );
    expect(
      within(card).queryByRole('button', { name: /configure profiles/i })
    ).not.toBeInTheDocument();
  });

  it('shows a healthy engine as Installed with no warning', async () => {
    // The control. A readiness check applied too broadly would flag every
    // engine, which is a louder failure than the one being fixed.
    mockFetch([ct800Healthy]);
    renderSettings();
    const card = await findEngineCard('CT800');

    await waitFor(() =>
      expect(within(card).getByText(/^installed$/i)).toBeInTheDocument()
    );
    expect(within(card).queryByText(/profiles unavailable/i)).not.toBeInTheDocument();
    expect(
      within(card).getByRole('button', { name: /configure profiles/i })
    ).toBeInTheDocument();
  });

  it('keeps the technical detail behind a collapsed toggle', async () => {
    // The summary has to stay readable for someone who only wants to know the
    // engine is broken, while the token a maintainer needs stays one click away
    // on the card itself -- not buried in a log file on a board they cannot
    // reach. Collapsed by default; the detail must not be in the initial DOM.
    mockFetch([ct800WontStart]);
    renderSettings();
    const card = await findEngineCard('CT800');

    await waitFor(() =>
      expect(within(card).getByText(/did not start/i)).toBeInTheDocument()
    );
    expect(within(card).queryByText(/OSError ENOEXEC/)).not.toBeInTheDocument();
    expect(
      within(card).getByRole('button', { name: /show details/i })
    ).toBeInTheDocument();
  });

  it('reveals the reason code and technical detail when expanded', async () => {
    // What the user is asked to screenshot. Both the stable reason code and the
    // OS-level token must be present; either alone leaves the report ambiguous.
    mockFetch([ct800WontStart]);
    renderSettings();
    const card = await findEngineCard('CT800');
    const toggle = await within(card).findByRole('button', { name: /show details/i });

    fireEvent.click(toggle);

    await waitFor(() =>
      expect(within(card).getByText(/OSError ENOEXEC/)).toBeInTheDocument()
    );
    expect(within(card).getByText(/incompatible_binary/)).toBeInTheDocument();
  });

  it('dismisses the notice through the API and stops showing it', async () => {
    // Dismissal is an acknowledgement the server has to remember, or the notice
    // returns on the next poll and the button looks broken.
    mockFetch([ct800WontStart]);
    renderSettings();
    const card = await findEngineCard('CT800');
    const dismiss = await within(card).findByRole('button', { name: /dismiss/i });

    fireEvent.click(dismiss);

    await waitFor(() => expect(dismissPosts).toEqual(['ct800']));
    await waitFor(() =>
      expect(within(card).queryByText(/did not start/i)).not.toBeInTheDocument()
    );
  });

  it('shows no notice for an already-dismissed failure but keeps the badge', async () => {
    // Reloading the page after dismissing must not resurrect the notice, while
    // the engine's actual state -- still unusable -- stays visible.
    mockFetch([ct800Dismissed]);
    renderSettings();
    const card = await findEngineCard('CT800');

    await waitFor(() =>
      expect(within(card).getByText(/profiles unavailable/i)).toBeInTheDocument()
    );
    expect(within(card).queryByText(/did not start/i)).not.toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument();
  });

  it('reports a failed install without implying the engine is present', async () => {
    // An install that never produced a binary is a different failure with a
    // different fix. Reusing the "installed but broken" wording here would tell
    // the user to rebuild something that was never built.
    mockFetch([arasanInstallFailed]);
    renderSettings();
    const card = await findEngineCard('Arasan');

    await waitFor(() =>
      expect(within(card).getByText(/could not be installed/i)).toBeInTheDocument()
    );
    expect(within(card).queryByText(/profiles unavailable/i)).not.toBeInTheDocument();
    expect(within(card).getByRole('button', { name: /^install$/i })).toBeInTheDocument();
  });
});
