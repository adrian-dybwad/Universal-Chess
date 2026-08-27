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
 *    unsigned-script execution policy);
 *  - an Imported app display driver card reports SPI, GPIO, and panel class
 *    scanned from the uploaded original Centaur tree (not UC's epdconfig), and
 *    does not invent BCM pin numbers when that build only stores the names.
 *    Pins used at runtime come from the last Translate Mode launch.
 *
 * A regression manifests as: no "Original Centaur" tab; an "Elo"/"Threads"/"Hash"
 * input reappearing; the strength dropdown missing; the action button
 * preceding the engine group again; a Save button returning (and changes not
 * POSTing until it is clicked); the PowerShell remedies missing / shown
 * expanded so they crowd the import steps; or the display-driver card missing,
 * showing Universal Chess pins, or filling BCM numbers the uploaded build does
 * not contain.
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

// The stored level is a profile id; what the user reads is its projected label.
const STOCKFISH_RUNG = 'Profile-a1b2c3';
const STOCKFISH_RUNG_LABEL = '1500 ELO';

function emptyCentaurDisplayDiagnostics(centaurAvailable: boolean) {
  if (!centaurAvailable) {
    return {
      installed: false,
      scanned: false,
      panel_driver: null,
      driver_modules: [],
      controller_family: null,
      spi_devices: [],
      spi_path_template: null,
      spi_library: null,
      gpio_numbering: null,
      gpio_backend: null,
      pin_identifiers: [],
      pins: { rst: null, dc: null, busy: null, cs: null },
      observed_gpio_pins: [],
      observed_spi_devices: [],
    };
  }
  // Default installed payload matches an official Nuitka build: class and
  // pin *names* are in the binary, BCM integers and a literal /dev/spidevN.M
  // usually are not.
  return {
    installed: true,
    scanned: true,
    panel_driver: 'EPaperT5D',
    driver_modules: ['dgt_epaper.py', 'epaperDef.py', 'epaperT5D.py'],
    controller_family: 'UC8151D',
    spi_devices: [],
    spi_path_template: '/dev/spidev%d.%d',
    spi_library: 'spidev',
    gpio_numbering: 'BCM',
    gpio_backend: 'RPi.GPIO',
    pin_identifiers: ['EPAPER_BUSY', 'EPAPER_DC', 'EPAPER_RESET'],
    pins: { rst: null, dc: null, busy: null, cs: null },
    observed_gpio_pins: [],
    observed_spi_devices: [],
  };
}

// `centaurAvailable` toggles the installed vs. importer branch; the rest of the
// Centaur endpoints are stubbed to a stopped board, in translate mode unless
// `directMode` says otherwise. Engine POSTs are recorded so auto-save tests can
// assert the dedicated endpoint body, and the handover POST is recorded too so
// the success-message tests can act on a launch that reported success.
function installCentaurFetchMock(opts: {
  centaurAvailable: boolean;
  enginePostStatus?: number;
  directMode?: boolean;
  displayDiagnostics?: Record<string, unknown>;
}) {
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
    if (url === '/api/system/centaur-mode') return jsonResponse({ direct_mode: opts.directMode ?? false });
    if (url === '/api/system/centaur-status') return jsonResponse({ running: false });
    if (url === '/api/system/run-centaur' && method === 'POST') {
      posts.push({ url, body: {} });
      return jsonResponse({ success: true });
    }
    if (url === '/api/system/centaur-engine' && method === 'POST') {
      posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') });
      const status = opts.enginePostStatus ?? 200;
      return jsonResponse(status >= 200 && status < 300 ? { success: true } : { success: false }, status);
    }
    if (url === '/api/system/centaur-engine') return jsonResponse({ engine: 'stockfish', level: STOCKFISH_RUNG, options: {} });
    if (url === '/api/system/centaur-display-diagnostics') {
      return jsonResponse(opts.displayDiagnostics ?? emptyCentaurDisplayDiagnostics(opts.centaurAvailable));
    }
    // Picker rows as /levels reports them: the value is the profile's generated
    // id (what the level setting stores) and the label is projected from the
    // profile's own option values, so the two are deliberately different here.
    if (url.startsWith('/api/engines/stockfish/levels')) {
      return jsonResponse([
        { value: 'Default', label: 'Default (Unlimited)' },
        { value: STOCKFISH_RUNG, label: STOCKFISH_RUNG_LABEL },
      ]);
    }
    if (url.startsWith('/api/engines/maia/levels')) {
      return jsonResponse([
        { value: 'Default', label: 'Default (1500 ELO)' },
        { value: 'Profile-d4e5f6', label: '1100 ELO' },
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

    // The strength dropdown is present and pre-selects the saved level by its
    // id, while showing that profile's label -- a row keyed by the label instead
    // would leave the stored id unmatched and the dropdown showing the wrong
    // profile.
    await screen.findByText('Strength');
    const strength = selectInLabeledRow('Strength');
    expect(strength.value).toBe(STOCKFISH_RUNG);
    expect(strength.selectedOptions[0].textContent).toBe(STOCKFISH_RUNG_LABEL);

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
    // Why: an engine's profiles are engine-specific -- a profile id belongs to
    // one engine's config file. Carrying stockfish's rung onto Maia would persist
    // a level that engine does not have at all (Players already
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
    expect(selectInLabeledRow('Strength').value).toBe(STOCKFISH_RUNG);
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

  // Confirms the handover and returns the success banner's text. The dialog is
  // stubbed to accept because these tests are about what is said afterwards.
  async function switchToCentaur(): Promise<HTMLElement> {
    vi.stubGlobal('confirm', vi.fn(() => true));
    await userEvent.click(await screen.findByRole('button', { name: 'Switch to Original Centaur' }));
    return await screen.findByText(/Launching the original Centaur software/i);
  }

  it('tells translate-mode users they can also hold BACK on the board', async () => {
    // Why: the web page is the only place the handover is explained, and until
    // now it named just one way back -- this browser tab. A user who closes it,
    // or walks up to the board without a phone, then has no way to know the
    // board itself can exit. Translate mode runs the serial tap, which watches
    // for a held BACK, so the gesture genuinely works and belongs in the copy.
    //
    // A regression manifests as the banner reverting to the Return-only wording,
    // leaving the board-side exit undocumented everywhere the user can read it.
    installCentaurFetchMock({ centaurAvailable: true, directMode: false });
    renderCentaurTab();

    const banner = await switchToCentaur();

    expect(banner).toHaveTextContent(/hold BACK on the board/i);
    expect(banner).toHaveTextContent(/Return to Universal Chess/i);
  });

  it('omits the BACK gesture in direct mode, where nothing watches for it', async () => {
    // Why: the exit gesture is implemented by the serial tap, and direct mode has
    // no tap -- the board port is handed to Centaur outright. Repeating the hint
    // here would send the user to hold a button that is not being listened to,
    // stranding them at a board that never responds when the web tab was in fact
    // their only way back. The two modes must therefore say different things.
    //
    // A regression manifests as one shared string for both modes, so direct mode
    // advertises an exit it cannot perform.
    installCentaurFetchMock({ centaurAvailable: true, directMode: true });
    renderCentaurTab();

    const banner = await switchToCentaur();

    // Matched on the gesture phrase, not the bare word: both messages end in
    // "come back", so a /BACK/i check could never pass and would test nothing.
    expect(banner).not.toHaveTextContent(/hold BACK/i);
    expect(banner).toHaveTextContent(/Return to Universal Chess/i);
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

  it('shows display-driver facts extracted from the imported original app', async () => {
    // Why: the card must report the uploaded Centaur build's SPI device, GPIO
    // pins, and panel class -- not Universal Chess's epdconfig. A regression
    // that drops the card or shows UC pins instead of the payload hides the
    // only in-app view of what that original version actually drives.
    installCentaurFetchMock({
      centaurAvailable: true,
      displayDiagnostics: {
        installed: true,
        scanned: true,
        panel_driver: 'EPaperT5D',
        driver_modules: ['epaperT5D.py', 'dgt_epaper.py'],
        controller_family: 'UC8151D',
        spi_devices: ['/dev/spidev1.0'],
        spi_path_template: '/dev/spidev%d.%d',
        spi_library: 'spidev',
        gpio_numbering: 'BCM',
        gpio_backend: 'RPi.GPIO',
        pin_identifiers: ['EPAPER_BUSY', 'EPAPER_CS', 'EPAPER_DC', 'EPAPER_RESET'],
        pins: { rst: 12, dc: 16, busy: 7, cs: 18 },
        observed_gpio_pins: [],
        observed_spi_devices: [],
      },
    });
    renderCentaurTab();

    expect(await screen.findByText('EPaperT5D')).toBeInTheDocument();
    expect(screen.getByText('UC8151D')).toBeInTheDocument();
    expect(screen.getByText('/dev/spidev1.0')).toBeInTheDocument();
    expect(screen.getByText(/RST BCM 12 \(EPAPER_RESET\)/)).toBeInTheDocument();
    expect(screen.getByText(/DC BCM 16 \(EPAPER_DC\)/)).toBeInTheDocument();
    expect(screen.getByText(/BUSY BCM 7 \(EPAPER_BUSY\)/)).toBeInTheDocument();
    expect(screen.getByText(/CS BCM 18 \(EPAPER_CS\)/)).toBeInTheDocument();
  });

  it('does not invent pin numbers when the uploaded build only names the lines', async () => {
    // Why: official Nuitka builds store EPAPER_RESET as a name without a
    // readable BCM integer. Filling in 12/16/7/18 from UC would describe the
    // wrong program when a different original version is uploaded.
    installCentaurFetchMock({
      centaurAvailable: true,
      displayDiagnostics: {
        installed: true,
        scanned: true,
        panel_driver: 'EPaperT5D',
        driver_modules: [],
        controller_family: 'UC8151D',
        spi_devices: [],
        spi_path_template: '/dev/spidev%d.%d',
        spi_library: 'spidev',
        gpio_numbering: 'BCM',
        gpio_backend: 'RPi.GPIO',
        pin_identifiers: ['EPAPER_RESET', 'EPAPER_DC', 'EPAPER_BUSY'],
        pins: { rst: null, dc: null, busy: null, cs: null },
        observed_gpio_pins: [],
        observed_spi_devices: [],
      },
    });
    renderCentaurTab();

    expect(await screen.findByText('EPaperT5D')).toBeInTheDocument();
    expect(screen.getByText(/does not store pin numbers/i)).toBeInTheDocument();
    expect(screen.getByText(/Not recorded yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/RST BCM 12/)).toBeNull();
  });

  it('shows GPIO pins recorded from the last Translate Mode run', async () => {
    // Why: official Nuitka builds do not store BCM integers. The card must
    // show the pins that process actually opened, with this build's pin names
    // on the same line, persisted after Universal Chess restarts -- not UC's
    // epdconfig. Direct Mode cannot record them.
    //
    // A regression manifests as the runtime row staying on the empty-capture
    // copy, or as RST BCM 12 appearing from Universal Chess rather than the
    // observed list.
    installCentaurFetchMock({
      centaurAvailable: true,
      displayDiagnostics: {
        installed: true,
        scanned: true,
        panel_driver: 'EPaperT5D',
        driver_modules: [],
        controller_family: 'UC8151D',
        spi_devices: [],
        spi_path_template: '/dev/spidev%d.%d',
        spi_library: 'spidev',
        gpio_numbering: 'BCM',
        gpio_backend: 'RPi.GPIO',
        pin_identifiers: ['EPAPER_RESET', 'EPAPER_DC', 'EPAPER_BUSY'],
        pins: { rst: null, dc: null, busy: null, cs: null },
        observed_gpio_pins: [7, 12, 16, 18],
        observed_spi_devices: ['/dev/spidev1.0'],
      },
    });
    renderCentaurTab();

    expect(await screen.findByText('EPaperT5D')).toBeInTheDocument();
    const runtimeValue = screen.getByText('Pins used at runtime').nextElementSibling;
    expect(runtimeValue).toHaveTextContent(
      'BCM 7, 12, 16, 18 (EPAPER_RESET, EPAPER_DC, EPAPER_BUSY)',
    );
    expect(screen.getByText('/dev/spidev1.0')).toBeInTheDocument();
    expect(screen.getByText(/last time Original Centaur ran in Translate Mode/i)).toBeInTheDocument();
    expect(screen.queryByText(/Not recorded yet/i)).toBeNull();
    // Names must not be assigned to specific BCM numbers when the build did
    // not store that pairing -- RST BCM 12 would be Universal Chess's map.
    expect(runtimeValue).not.toHaveTextContent('RST BCM 12');
  });

  it('labels an observed pin with its name only when this build maps that number', async () => {
    // Why: a readable EPAPER_RESET = 12 in the uploaded tree is a real pairing.
    // An extra BCM the process also wiggled must still appear, unlabeled,
    // so a stray LED or CS line is visible instead of dropped.
    //
    // A regression that applies RST/DC/BUSY/CS to every observed pin from UC,
    // or that hides BCM 25, is this test failing.
    installCentaurFetchMock({
      centaurAvailable: true,
      displayDiagnostics: {
        installed: true,
        scanned: true,
        panel_driver: 'EPaperT5D',
        driver_modules: [],
        controller_family: 'UC8151D',
        spi_devices: [],
        spi_path_template: null,
        spi_library: 'spidev',
        gpio_numbering: 'BCM',
        gpio_backend: 'RPi.GPIO',
        pin_identifiers: ['EPAPER_RESET', 'EPAPER_DC', 'EPAPER_BUSY'],
        pins: { rst: 12, dc: 16, busy: 7, cs: null },
        observed_gpio_pins: [7, 12, 16, 25],
        observed_spi_devices: ['/dev/spidev1.0'],
      },
    });
    renderCentaurTab();

    const runtimeValue = (await screen.findByText('Pins used at runtime')).nextElementSibling;
    expect(runtimeValue).toHaveTextContent(
      'BUSY BCM 7 (EPAPER_BUSY), RST BCM 12 (EPAPER_RESET), DC BCM 16 (EPAPER_DC), BCM 25',
    );
  });

  it('tells the user to import Centaur before display-driver facts exist', async () => {
    // Why: the tab is reachable before an import. The card must stay visible
    // and say the scan needs an uploaded app, not show an empty grid or UC pins.
    installCentaurFetchMock({ centaurAvailable: false });
    renderCentaurTab();

    expect(await screen.findByRole('heading', { name: 'Imported app display driver' })).toBeInTheDocument();
    expect(
      await screen.findByText(/Import the original Centaur software to inspect the display driver/i)
    ).toBeInTheDocument();
  });
});
