// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the Original Centaur tab that was split out of the System tab:
 *  - it is its own sub-nav tab and is always shown (discoverable) so the import
 *    flow is reachable even when Centaur is not installed;
 *  - strength is chosen from a profile dropdown (not free-text Elo), and the old
 *    Threads/Hash inputs are gone;
 *  - in translate mode the engine/strength group renders *before* the handover
 *    action button (the reorder), so the user configures the engine, then acts;
 *  - engine and strength auto-save on change (no Save button), and changing the
 *    engine resets strength to Default so a mismatched profile is never written;
 *  - a collapsed Troubleshooting card documents the Windows PowerShell errors
 *    that stop `make-centaur-image.ps1` (current-directory invocation and the
 *    unsigned-script execution policy).
 *
 * A regression manifests as: no "Original Centaur" tab; an "Elo"/"Threads"/"Hash"
 * input reappearing; the strength dropdown missing; the action button
 * preceding the engine group again; a Save button returning (and changes not
 * POSTing until it is clicked); or the PowerShell remedies missing / shown
 * expanded so they crowd the import steps.
 */

const menuSchema: unknown = menuSchemaFixture;

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

interface PostRecord { url: string; body: Record<string, unknown> }

// `centaurAvailable` toggles the installed vs. importer branch; the rest of the
// Centaur endpoints are stubbed to a stopped board in translate mode. Engine
// POSTs are recorded so auto-save tests can assert the dedicated endpoint body.
function installCentaurFetchMock(opts: { centaurAvailable: boolean; enginePostStatus?: number }) {
  const posts: PostRecord[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload());
    if (url === '/api/settings' && method === 'POST') {
      posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') });
      return jsonResponse({ success: true });
    }
    if (url === '/api/system/info') return jsonResponse({ centaur_available: opts.centaurAvailable });
    if (url === '/api/system/centaur-mode') return jsonResponse({ direct_mode: false });
    if (url === '/api/system/centaur-status') return jsonResponse({ running: false });
    if (url === '/api/system/centaur-engine' && method === 'POST') {
      posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') });
      const status = opts.enginePostStatus ?? 200;
      return jsonResponse(status >= 200 && status < 300 ? { success: true } : { success: false }, status);
    }
    if (url === '/api/system/centaur-engine') return jsonResponse({ engine: 'stockfish', level: '1500 ELO', options: {} });
    if (url.startsWith('/api/engines/stockfish/levels')) {
      return jsonResponse([
        { value: 'Default', label: 'Default' },
        { value: '1500 ELO', label: '1500 ELO' },
      ]);
    }
    if (url.startsWith('/api/engines/maia/levels')) {
      return jsonResponse([
        { value: 'Default', label: 'Default' },
        { value: '1100 ELO', label: '1100 ELO' },
      ]);
    }
    if (url === '/api/engines/all') {
      return jsonResponse([
        { name: 'stockfish', display_name: 'Stockfish', installed: true },
        { name: 'maia', display_name: 'Maia', installed: true },
      ]);
    }
    if (url === '/api/accounts') return jsonResponse({ accounts: [] });
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', MockEventSource);
  return { posts };
}

// Engine and Strength both render as labeled form rows; the group heading is
// also "Engine", so the row is the labeled ancestor, not getByText('Engine').
function selectInLabeledRow(label: string): HTMLSelectElement {
  const matches = screen.getAllByText(label);
  const row = matches.map((el) => el.closest('.form-row')).find((el): el is HTMLElement => el !== null);
  if (!row) throw new Error(`${label} form row not found`);
  return within(row).getByRole('combobox') as HTMLSelectElement;
}

beforeEach(() => {
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderCentaurTab() {
  return render(
    <MemoryRouter initialEntries={['/settings/centaur']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('Original Centaur tab', () => {
  it('is always shown in the sub-nav even when Centaur is not installed', async () => {
    // Why: the tab must be discoverable so a user can find and start the import
    // flow before Centaur exists. A regression that gates the tab on
    // centaur_available drops the tab entirely and hides the importer.
    installCentaurFetchMock({ centaurAvailable: false });
    renderCentaurTab();

    // The sub-nav tab carries the exact label "Original Centaur".
    expect(await screen.findByText('Original Centaur')).toBeInTheDocument();
    // The not-installed branch shows the importer, not the engine controls.
    expect(
      await screen.findByText(/The original DGT Centaur software is not installed yet/i)
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Download script \(macOS\/Linux\)/i })).toBeInTheDocument();
  });

  it('chooses strength from a profile dropdown with no Elo/Threads/Hash inputs', async () => {
    // Why: the free-text Elo and the Threads/Hash inputs were replaced by a
    // single strength dropdown sourced from the engine's profiles. A regression
    // reintroduces the raw numeric inputs or drops the dropdown.
    installCentaurFetchMock({ centaurAvailable: true });
    renderCentaurTab();

    // The strength dropdown is present and pre-selects the saved level.
    await screen.findByText('Strength');
    expect(selectInLabeledRow('Strength').value).toBe('1500 ELO');

    // The removed controls must not be present anywhere on the tab.
    expect(screen.queryByText('Elo')).toBeNull();
    expect(screen.queryByText('Threads')).toBeNull();
    expect(screen.queryByText('Hash (MB)')).toBeNull();
  });

  it('renders the engine/strength group before the handover action button', async () => {
    // Why: the reorder puts engine configuration first and the "Switch to
    // Original Centaur" action at the bottom. A regression restores the old order
    // (action button above the engine group), which this catches via DOM order.
    installCentaurFetchMock({ centaurAvailable: true });
    renderCentaurTab();

    const engineHeading = await screen.findByRole('heading', { name: 'Engine' });
    const switchButton = screen.getByRole('button', { name: 'Switch to Original Centaur' });

    // DOCUMENT_POSITION_FOLLOWING means switchButton comes after the engine group.
    const relation = engineHeading.compareDocumentPosition(switchButton);
    expect(relation & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('auto-saves a strength change and shows no Save engine button', async () => {
    // Why: engine/strength used an explicit Save while every other value setting
    // (and Direct Mode on this card) persist on change. Leaving the tab after a
    // dropdown edit discarded the choice. A regression restores the button and
    // leaves lastEnginePost null until it is clicked.
    const { posts } = installCentaurFetchMock({ centaurAvailable: true });
    renderCentaurTab();

    await screen.findByText('Strength');
    expect(screen.queryByRole('button', { name: 'Save engine settings' })).toBeNull();
    expect(screen.getByText(/apply the next time Centaur launches/i)).toBeInTheDocument();

    fireEvent.change(selectInLabeledRow('Strength'), { target: { value: 'Default' } });

    await waitFor(() => {
      expect(posts.some((p) => p.url === '/api/system/centaur-engine')).toBe(true);
    });
    const enginePost = posts.find((p) => p.url === '/api/system/centaur-engine');
    expect(enginePost?.body).toEqual({ engine: 'stockfish', level: 'Default' });
    expect(posts.some((p) => p.url === '/api/settings')).toBe(false);
  });

  it('resets strength to Default and auto-saves when the engine changes', async () => {
    // Why: an engine's profiles are engine-specific. Carrying "1500 ELO" onto
    // Maia would persist a level that engine may not have (Players already
    // resets to Default). A regression that saves the old level, or that does
    // not POST at all, is this test failing.
    const { posts } = installCentaurFetchMock({ centaurAvailable: true });
    renderCentaurTab();

    const engineSelect = await waitFor(() => {
      const select = selectInLabeledRow('Engine');
      if (![...select.options].some((o) => o.value === 'maia')) {
        throw new Error('maia option not loaded');
      }
      return select;
    });
    fireEvent.change(engineSelect, { target: { value: 'maia' } });

    expect(selectInLabeledRow('Strength').value).toBe('Default');
    await waitFor(() => {
      expect(posts.some((p) => p.url === '/api/system/centaur-engine')).toBe(true);
    });
    expect(posts.find((p) => p.url === '/api/system/centaur-engine')?.body).toEqual({
      engine: 'maia',
      level: 'Default',
    });
  });

  it('does not POST engine settings on load', async () => {
    // Why: populating the dropdowns from GET must not write the loaded values
    // back. A useEffect-on-state auto-save would POST stockfish/1500 ELO on
    // every visit, which this asserts by requiring an empty post log after
    // the controls have rendered.
    const { posts } = installCentaurFetchMock({ centaurAvailable: true });
    renderCentaurTab();

    await screen.findByText('Strength');
    expect(selectInLabeledRow('Strength').value).toBe('1500 ELO');
    expect(posts.filter((p) => p.url === '/api/system/centaur-engine')).toEqual([]);
  });

  it('shows an inline error when the engine auto-save fails', async () => {
    // Why: Direct Mode reports a failed persist in place and does not pretend
    // the change stuck. A silent failure would leave the dropdown showing a
    // value the proxy will not use. Manifests as the engineSaveFailed copy
    // missing after a 500.
    const { posts } = installCentaurFetchMock({ centaurAvailable: true, enginePostStatus: 500 });
    renderCentaurTab();

    await screen.findByText('Strength');
    fireEvent.change(selectInLabeledRow('Strength'), { target: { value: 'Default' } });

    await waitFor(() => {
      expect(posts.some((p) => p.url === '/api/system/centaur-engine')).toBe(true);
    });
    expect(
      await screen.findByText('Failed to save the Centaur engine settings.')
    ).toBeInTheDocument();
  });

  it('keeps Windows PowerShell troubleshooting collapsed until opened', async () => {
    // Why: importing Original Centaur from Windows is blocked by two PowerShell
    // errors (current-directory invocation, then unsigned-script policy). Those
    // remedies belong on this tab, collapsed so they do not crowd the import
    // steps. A regression drops the card, leaves the commands visible by
    // default, or omits Bypass / Unblock-File / RemoteSigned so a stuck user
    // has no copy-pasteable fix.
    installCentaurFetchMock({ centaurAvailable: false });
    renderCentaurTab();

    const cardTitle = await screen.findByRole('heading', { name: 'Troubleshooting' });
    const card = cardTitle.closest('.card') as HTMLElement;
    const expand = within(card).getByRole('button', { name: 'Show details' });
    expect(expand).toHaveAttribute('aria-expanded', 'false');
    expect(within(card).queryByText(/ExecutionPolicy Bypass/)).toBeNull();
    expect(within(card).queryByText('Unblock-File .\\make-centaur-image.ps1')).toBeNull();

    await userEvent.click(expand);

    expect(expand).toHaveAttribute('aria-expanded', 'true');
    expect(within(card).getByText(/does not run scripts from the current folder/i)).toBeInTheDocument();
    expect(within(card).getByText(/not digitally signed/i)).toBeInTheDocument();
    expect(within(card).getByText('powershell -ExecutionPolicy Bypass -File .\\make-centaur-image.ps1')).toBeInTheDocument();
    expect(within(card).getByText('Unblock-File .\\make-centaur-image.ps1')).toBeInTheDocument();
    expect(within(card).getByText('Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned')).toBeInTheDocument();
    expect(within(card).getByText(/elevated \(Administrator\) PowerShell/i)).toBeInTheDocument();
  });

  it('shows the Troubleshooting card when Centaur is already installed', async () => {
    // Why: the PowerShell errors happen on the computer holding the SD card,
    // independent of whether Centaur is already on the board. Burying the
    // section inside Re-import would hide it after the first install. A
    // regression that gates the card on !centaurAvailable drops the help
    // exactly when a re-image is being attempted.
    installCentaurFetchMock({ centaurAvailable: true });
    renderCentaurTab();

    expect(await screen.findByText('Re-import from SD')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Troubleshooting' })).toBeInTheDocument();
  });
});
