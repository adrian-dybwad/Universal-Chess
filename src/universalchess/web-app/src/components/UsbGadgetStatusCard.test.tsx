// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';
import { UsbGadgetStatusCard } from './UsbGadgetStatusCard';

/**
 * Guards the USB gadget status readout (Connectivity card, formerly System).
 *
 * Why it exists: selecting Client/Shared/Off only records desired mode and asks
 * the helper to apply it. Without a live/prepared/expected-state readout, a
 * board that still needs a reboot after Off, or that enable_usb_gadget.py
 * prepared but never brought usb0 up, looks the same as a working link.
 *
 * A regression here either hides a mismatch (Match never shows "No"), drops
 * the reboot hint when reboot_required is true, or omits the Reboot button that
 * posts /api/system/reboot when a reboot is required.
 */

interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text?: () => Promise<string>;
}

function jsonResponse(body: unknown, status = 200): JsonResponseLike {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

interface GadgetPayload {
  desired: string;
  live: string;
  prepared: boolean;
  in_expected_state: boolean;
  reboot_required: boolean;
  attachment: string;
  ipv4: string | null;
  dhcp_lease_count: number | null;
  auto_switching: boolean | null;
}

function gadgetPayload(overrides: Partial<GadgetPayload> = {}): GadgetPayload {
  return {
    desired: 'client',
    live: 'client',
    prepared: true,
    in_expected_state: true,
    reboot_required: false,
    attachment: 'attached',
    ipv4: null,
    dhcp_lease_count: null,
    auto_switching: false,
    ...overrides,
  };
}

const apiFetchMock = vi.fn();

vi.mock('../stores/settingsStore', () => ({
  useSettingsStore: (selector: (s: { revision: number }) => unknown) =>
    selector({ revision: 0 }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => {
      const labels: Record<string, string> = {
        'settingsPage.usbGadget.title': 'USB Gadget Status',
        'settingsPage.usbGadget.description': 'Expected versus live gadget mode.',
        'settingsPage.usbGadget.unavailable': 'Could not read USB gadget status.',
        'settingsPage.usbGadget.desired': 'Desired',
        'settingsPage.usbGadget.live': 'Live',
        'settingsPage.usbGadget.match': 'Match',
        'settingsPage.usbGadget.matchYes': 'Matches',
        'settingsPage.usbGadget.matchNo': 'Does not match',
        'settingsPage.usbGadget.prepared': 'Prepared at boot',
        'settingsPage.usbGadget.preparedYes': 'Prepared',
        'settingsPage.usbGadget.preparedNo': 'Not prepared',
        'settingsPage.usbGadget.link': 'Link',
        'settingsPage.usbGadget.linkStates.connected': 'Connected',
        'settingsPage.usbGadget.linkStates.disconnected': 'Disconnected',
        'settingsPage.usbGadget.linkStates.unknown': 'Unknown',
        'settingsPage.usbGadget.address': 'Address',
        'settingsPage.usbGadget.addressNone': 'None',
        'settingsPage.usbGadget.dhcpLeases': 'DHCP leases',
        'settingsPage.usbGadget.reboot': 'Reboot',
        'settingsPage.usbGadget.rebootNeeded': 'Needed for this preference to finish applying.',
        'settingsPage.usbGadget.rebootNow': 'Reboot now',
        'settingsPage.usbGadget.rebooting': 'Rebooting…',
        'settingsPage.systemActions.rebootConfirm':
          'Reboot the board? The web interface will be unavailable until it restarts.',
        'settingsPage.usbGadget.modes.off': 'Off',
        'settingsPage.usbGadget.modes.auto': 'Auto',
        'settingsPage.usbGadget.modes.client': 'Client',
        'settingsPage.usbGadget.modes.shared': 'Shared',
        'settingsPage.usbGadget.modes.unknown': 'Unknown',
        'settingsPage.usbGadget.autoSwitching': 'Auto switching',
        'settingsPage.usbGadget.autoSwitchingStates.enabled': 'Enabled',
        'settingsPage.usbGadget.autoSwitchingStates.disabled': 'Disabled',
        'settingsPage.usbGadget.autoSwitchingStates.unknown': 'Unknown',
      };
      return labels[key] ?? key;
    },
  }),
}));

vi.mock('../utils/api', () => ({
  buildApiUrl: (path: string) => path,
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}));

vi.mock('./useAuthedAction', () => ({
  useAuthedAction: () => ({
    dialog: null,
    onUnauthorized: (retry: () => void) => retry(),
  }),
}));

describe('UsbGadgetStatusCard', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue(jsonResponse({ success: true }));
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(gadgetPayload())),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('shows live mode and a Yes match when desired equals live', async () => {
    // Why: the happy path must surface both modes and that they agree.
    // Failure: Match row missing or still "No" when in_expected_state is true.
    render(<UsbGadgetStatusCard />);
    await waitFor(() => {
      expect(screen.getAllByText('Client').length).toBeGreaterThanOrEqual(2);
      expect(screen.getByText('Matches')).toBeInTheDocument();
    });
  });

  it('shows No when live does not match desired', async () => {
    // Why: a mismatch is the whole reason this card exists.
    // Failure: Match stays Yes / badge missing when in_expected_state is false.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          gadgetPayload({
            desired: 'client',
            live: 'off',
            in_expected_state: false,
            prepared: true,
          }),
        ),
      ),
    );
    render(<UsbGadgetStatusCard />);
    await waitFor(() => {
      expect(screen.getByText('Does not match')).toBeInTheDocument();
      expect(screen.getByText('Off')).toBeInTheDocument();
    });
  });

  it('shows Connected/Disconnected and Address using the same words as e-paper', async () => {
    // Why: web said Attached while the board said Connected, and omitted the
    // usb0 IP the user is already using. Failure: Attached/Host link still
    // shown, or Address row missing when ipv4 is set.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          gadgetPayload({
            desired: 'client',
            live: 'client',
            in_expected_state: true,
            attachment: 'attached',
            ipv4: '192.168.2.35',
          }),
        ),
      ),
    );
    render(<UsbGadgetStatusCard />);
    expect(await screen.findByText('Connected')).toBeInTheDocument();
    expect(screen.getByText('192.168.2.35')).toBeInTheDocument();
    expect(screen.queryByText('Attached')).not.toBeInTheDocument();
  });

  it('shows Disconnected and None when the USB link has no address', async () => {
    // Why: Match can still be Yes for configured Client with no lease; Address
    // must not invent an IP. Failure: Connected badge or a fabricated address.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          gadgetPayload({
            desired: 'client',
            live: 'client',
            in_expected_state: true,
            attachment: 'not_attached',
            ipv4: null,
          }),
        ),
      ),
    );
    render(<UsbGadgetStatusCard />);
    expect(await screen.findByText('Disconnected')).toBeInTheDocument();
    expect(screen.getByText('None')).toBeInTheDocument();
  });

  it('shows DHCP lease count when live mode is Shared', async () => {
    // Why: Shared looked fine with dnsmasq up and an empty lease file while the
    // host had APIPA. Failure: lease row absent on Shared, or shown for Client.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          gadgetPayload({
            desired: 'shared',
            live: 'shared',
            in_expected_state: true,
            attachment: 'attached',
            ipv4: '10.12.194.1',
            dhcp_lease_count: 0,
          }),
        ),
      ),
    );
    render(<UsbGadgetStatusCard />);
    expect(await screen.findByText('DHCP leases')).toBeInTheDocument();
    expect(screen.getByText('0')).toBeInTheDocument();
  });

  it('shows Auto as desired alongside the mode the switcher settled on', async () => {
    // Why: Auto has no live mode of its own, so the card has to show both the
    // selected Auto and the concrete Client/Shared the board currently holds --
    // that is how the user knows which address to use. Failure: Auto renders as
    // "Unknown" (missing label), or Match reads No for a healthy Auto board.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          gadgetPayload({
            desired: 'auto',
            live: 'shared',
            in_expected_state: true,
            attachment: 'attached',
            ipv4: '10.12.194.1',
            auto_switching: true,
          }),
        ),
      ),
    );
    render(<UsbGadgetStatusCard />);
    await waitFor(() => {
      expect(screen.getByText('Auto')).toBeInTheDocument();
      expect(screen.getByText('Shared')).toBeInTheDocument();
      expect(screen.getByText('Matches')).toBeInTheDocument();
    });
    expect(screen.queryByText('Unknown')).not.toBeInTheDocument();
  });

  it('reports the switcher state while Auto is selected', async () => {
    // Why: with the switcher disabled the board is pinned to whatever mode it
    // holds, so Match reads No while Desired and Live look reasonable. Without
    // this row that verdict is unexplained. Failure: the row is missing, or it
    // claims Enabled when the flag says otherwise.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          gadgetPayload({
            desired: 'auto',
            live: 'client',
            in_expected_state: false,
            auto_switching: false,
          }),
        ),
      ),
    );
    render(<UsbGadgetStatusCard />);
    expect(await screen.findByText('Auto switching')).toBeInTheDocument();
    expect(screen.getByText('Disabled')).toBeInTheDocument();
    expect(screen.getByText('Does not match')).toBeInTheDocument();
  });

  it('says the switcher state is unknown rather than guessing', async () => {
    // Why: the probe cannot always read the unit (absent, static, no systemctl).
    // Showing Disabled there would accuse a working Auto board of being pinned.
    // Failure: null renders as Disabled/Enabled or as an empty cell.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          gadgetPayload({ desired: 'auto', live: 'client', auto_switching: null }),
        ),
      ),
    );
    render(<UsbGadgetStatusCard />);
    expect(await screen.findByText('Auto switching')).toBeInTheDocument();
    expect(screen.getByText('Unknown')).toBeInTheDocument();
    expect(screen.queryByText('Disabled')).not.toBeInTheDocument();
  });

  it('omits the switcher row for the pinned modes', async () => {
    // Why: Client and Shared apply by disabling the switcher, so the row would
    // only ever restate that -- noise in a card the user reads for the link.
    // Failure: the row appears for Client, implying the mode can still change.
    render(<UsbGadgetStatusCard />);
    await waitFor(() => expect(screen.getByText('Matches')).toBeInTheDocument());
    expect(screen.queryByText('Auto switching')).not.toBeInTheDocument();
  });

  it('shows the reboot hint when reboot_required is true', async () => {
    // Why: Off + prepared (or Client + unprepared) needs a reboot; hide that and
    // the select looks applied when boot will undo or never finish it.
    // Failure: reboot row absent despite reboot_required.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          gadgetPayload({
            desired: 'off',
            live: 'off',
            prepared: true,
            in_expected_state: true,
            reboot_required: true,
          }),
        ),
      ),
    );
    render(<UsbGadgetStatusCard />);
    await waitFor(() => {
      expect(
        screen.getByText('Needed for this preference to finish applying.'),
      ).toBeInTheDocument();
    });
  });

  it('offers a Reboot now button only when reboot_required is true', async () => {
    // Why: Client/Shared without prepared boot (or Off while still prepared)
    // needs a reboot; the status text alone is easy to miss. Failure: button
    // missing when required, or shown when reboot is not required.
    const { rerender } = render(<UsbGadgetStatusCard />);
    await waitFor(() => expect(screen.getByText('Matches')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Reboot now' })).not.toBeInTheDocument();

    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          gadgetPayload({
            desired: 'client',
            live: 'off',
            prepared: false,
            in_expected_state: false,
            reboot_required: true,
          }),
        ),
      ),
    );
    rerender(<UsbGadgetStatusCard refreshKey={1} />);
    expect(await screen.findByRole('button', { name: 'Reboot now' })).toBeInTheDocument();
  });

  it('posts /api/system/reboot after confirm when Reboot now is clicked', async () => {
    // Why: the button must hit the same reboot endpoint as System -> Power.
    // Failure: no POST, wrong path, or reboot without confirmation.
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          gadgetPayload({
            desired: 'client',
            live: 'off',
            prepared: false,
            in_expected_state: false,
            reboot_required: true,
          }),
        ),
      ),
    );
    render(<UsbGadgetStatusCard />);
    await user.click(await screen.findByRole('button', { name: 'Reboot now' }));
    expect(confirmSpy).toHaveBeenCalled();
    expect(apiFetchMock).toHaveBeenCalledWith('/api/system/reboot', {
      method: 'POST',
      requiresAuth: true,
    });
    confirmSpy.mockRestore();
  });

  it('re-fetches status on an interval so a reboot does not leave stale live mode', async () => {
    // Why: after Reboot now (or a host-side reboot) the page stays mounted in
    // the browser while the board is down; without a poll, Desired/Live freeze
    // until a full reload. Failure: fetch is called only once on mount.
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => jsonResponse(gadgetPayload()));
    vi.stubGlobal('fetch', fetchMock);
    render(<UsbGadgetStatusCard />);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it('clears status when a fetch fails so unavailable is shown instead of stale rows', async () => {
    // Why: during reboot the GET fails; keeping the previous Desired/Live makes
    // Shared look current until the user manually refreshes. Failure: Match /
    // mode rows stay on screen after the failing fetch.
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(gadgetPayload({ live: 'shared', desired: 'client' })))
      .mockResolvedValueOnce(jsonResponse({ error: 'down' }, 503));
    vi.stubGlobal('fetch', fetchMock);
    const { rerender } = render(<UsbGadgetStatusCard />);
    expect(await screen.findByText('Shared')).toBeInTheDocument();
    rerender(<UsbGadgetStatusCard refreshKey={1} />);
    expect(await screen.findByText('Could not read USB gadget status.')).toBeInTheDocument();
    expect(screen.queryByText('Shared')).not.toBeInTheDocument();
  });
});
