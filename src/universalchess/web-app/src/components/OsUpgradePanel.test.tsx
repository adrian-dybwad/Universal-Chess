// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';
import { OsUpgradePanel } from './OsUpgradePanel';

/**
 * Guards the Operating system subsection of Settings -> Software Updates.
 *
 * Why: Universal Chess OTA and ``apt upgrade`` are different products. This
 * panel must not claim the OS is current before a check, must POST check/apply
 * to the OS-upgrade endpoints (not /api/updates/*), and must offer Reboot now
 * only when reboot_required is true.
 */

interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
}

function jsonResponse(body: unknown, status = 200): JsonResponseLike {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

interface OsPayload {
  is_checking: boolean;
  is_applying: boolean;
  upgradable_count: number | null;
  upgradable: string[];
  last_check: string | null;
  reboot_required: boolean;
  error: string | null;
}

function osPayload(overrides: Partial<OsPayload> = {}): OsPayload {
  return {
    is_checking: false,
    is_applying: false,
    upgradable_count: null,
    last_check: null,
    upgradable: [],
    reboot_required: false,
    error: null,
    ...overrides,
  };
}

let status = osPayload();
const posts: string[] = [];
const apiFetchMock = vi.fn();

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: { count?: number; time?: string }) => {
      const labels: Record<string, string> = {
        'settingsPage.updates.osTitle': 'Operating system',
        'settingsPage.updates.osIntro': 'Refresh Raspberry Pi OS packages.',
        'settingsPage.updates.osCheck': 'Check for OS updates',
        'settingsPage.updates.osChecking': 'Checking OS packages...',
        'settingsPage.updates.osApply': 'Update operating system',
        'settingsPage.updates.osApplying': 'Updating operating system…',
        'settingsPage.updates.osConfirm': 'Update the operating system now?',
        'settingsPage.updates.osUpToDate': 'The operating system is up to date.',
        'settingsPage.updates.osInProgressTitle': 'Operating system update in progress…',
        'settingsPage.updates.osInProgressBody': 'Package upgrades are installing.',
        'settingsPage.updates.osRebootNeeded': 'A reboot is required to finish the operating system update.',
        'settingsPage.updates.osRebootNow': 'Reboot now',
        'settingsPage.updates.osRebooting': 'Rebooting…',
        'settingsPage.updates.osCheckFailed': 'Could not check for OS updates.',
        'settingsPage.updates.osApplyFailed': 'Could not start the OS update.',
        'settingsPage.updates.osBusy': 'An operating system update is already running.',
        'settingsPage.updates.osLastChecked': `Last OS check: ${opts?.time ?? ''}`,
        'settingsPage.updates.networkError': 'Network error',
        'settingsPage.updates.errorLabel': 'Error:',
        'settingsPage.systemActions.rebootConfirm': 'Reboot the board?',
        'settingsPage.systemActions.actionFailed': 'Action failed',
      };
      if (key === 'settingsPage.updates.osAvailable' && opts?.count != null) {
        return opts.count === 1
          ? `${opts.count} package can be upgraded.`
          : `${opts.count} packages can be upgraded.`;
      }
      return labels[key] ?? key;
    },
  }),
}));

vi.mock('../utils/api', () => ({
  buildApiUrl: (path: string) => path,
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}));

vi.mock('./useLoginRetry', () => ({
  useLoginRetry: () => ({
    requireLogin: () => false,
    loginDialog: null,
  }),
}));

beforeEach(() => {
  posts.length = 0;
  status = osPayload();
  apiFetchMock.mockImplementation(async (url: string) => {
    posts.push(url);
    return jsonResponse({ success: true });
  });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url === '/api/system/os-upgrade') return jsonResponse(status);
      return jsonResponse({});
    }),
  );
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('OsUpgradePanel', () => {
  it('does not claim the OS is up to date before any check', async () => {
    // Why: never-checked (null count) must not look like a completed "0
    // packages" result. Failure: the up-to-date card appears on first paint.
    render(<OsUpgradePanel />);
    expect(await screen.findByRole('heading', { name: 'Operating system' })).toBeInTheDocument();
    expect(screen.queryByText('The operating system is up to date.')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Check for OS updates' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Update operating system' })).not.toBeInTheDocument();
  });

  it('posts /api/system/os-upgrade/check from the check button', async () => {
    // Why: this must not reuse /api/updates/check (GitHub OTA). Failure: the
    // recorded POST is the UC endpoint, so apt never runs.
    const user = userEvent.setup();
    render(<OsUpgradePanel />);
    await screen.findByRole('button', { name: 'Check for OS updates' });
    await user.click(screen.getByRole('button', { name: 'Check for OS updates' }));
    await waitFor(() => {
      expect(posts).toContain('/api/system/os-upgrade/check');
    });
    expect(posts.some((p) => p === '/api/updates/check')).toBe(false);
  });

  it('offers Update operating system when packages are pending', async () => {
    // Why: the apply button is the whole feature. Failure: count is shown but
    // there is no apply control, so the user can see work and cannot start it.
    status = osPayload({
      upgradable_count: 3,
      last_check: '2026-08-15T12:00:00Z',
      upgradable: ['openssl', 'linux-image-rpi', 'bluez'],
    });
    render(<OsUpgradePanel />);
    expect(await screen.findByText('3 packages can be upgraded.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Update operating system' })).toBeInTheDocument();
  });

  it('posts apply only after confirm, to the OS-upgrade endpoint', async () => {
    // Why: apply is apt-get upgrade as root. Skipping confirm, or posting
    // /api/updates/install, would either surprise the user or install the UC
    // .deb instead. Failure: confirm never called, or the POST path is OTA.
    const user = userEvent.setup();
    status = osPayload({
      upgradable_count: 1,
      last_check: '2026-08-15T12:00:00Z',
    });
    render(<OsUpgradePanel />);
    await user.click(await screen.findByRole('button', { name: 'Update operating system' }));
    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(posts).toContain('/api/system/os-upgrade/apply');
    });
    expect(posts.some((p) => p === '/api/updates/install')).toBe(false);
  });

  it('does not apply when confirm is cancelled', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    const user = userEvent.setup();
    status = osPayload({
      upgradable_count: 1,
      last_check: '2026-08-15T12:00:00Z',
    });
    render(<OsUpgradePanel />);
    await user.click(await screen.findByRole('button', { name: 'Update operating system' }));
    expect(posts).not.toContain('/api/system/os-upgrade/apply');
  });

  it('shows the up-to-date card only after a completed check with count 0', async () => {
    // Why: last_check + 0 is the honest "looked, nothing pending" state.
    // Failure: the card is missing here, or appeared in the never-checked test.
    status = osPayload({
      upgradable_count: 0,
      last_check: '2026-08-15T12:00:00Z',
    });
    render(<OsUpgradePanel />);
    expect(await screen.findByText('The operating system is up to date.')).toBeInTheDocument();
  });

  it('offers Reboot now only when reboot_required is true', async () => {
    // Why: kernel/firmware upgrades need a reboot; showing the button always
    // makes Power's reboot redundant and scary. Failure: button present on
    // the default (false) payload, or missing when the flag is true.
    render(<OsUpgradePanel />);
    await screen.findByRole('button', { name: 'Check for OS updates' });
    expect(screen.queryByRole('button', { name: 'Reboot now' })).not.toBeInTheDocument();
    cleanup();
    status = osPayload({
      upgradable_count: 0,
      last_check: '2026-08-15T12:00:00Z',
      reboot_required: true,
    });
    render(<OsUpgradePanel />);
    expect(await screen.findByRole('button', { name: 'Reboot now' })).toBeInTheDocument();
  });

  it('posts /api/system/reboot after confirm from Reboot now', async () => {
    const user = userEvent.setup();
    status = osPayload({
      last_check: '2026-08-15T12:00:00Z',
      upgradable_count: 0,
      reboot_required: true,
    });
    render(<OsUpgradePanel />);
    await user.click(await screen.findByRole('button', { name: 'Reboot now' }));
    await waitFor(() => {
      expect(posts).toContain('/api/system/reboot');
    });
  });
});
