// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { DeviceClockCard } from './DeviceClockCard';

/**
 * Guards the System-tab device clock card.
 *
 * Why it exists: the board is an RTC-less Pi and, on a USB-only link, has no
 * time source at all, so its wall clock can sit minutes from the browser's with
 * nothing on screen saying so. That blind spot is what let a five-minute board
 * clock error go unnoticed until it surfaced as a bug elsewhere. This card is
 * the readout, and -- when network sync is off -- the way to correct it.
 *
 * A regression here either hides a drifting clock (the card reports "in step",
 * or never renders the offset) or offers a clock set that timedatectl is going
 * to refuse because sync owns the clock.
 */

const BROWSER_EPOCH_SECONDS = 1800000000;
const OBSERVED_BOARD_SKEW_SECONDS = 295; // 4m55s, the skew measured on a real board

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

interface TimePayload {
  epoch_seconds: number;
  timezone: string;
  ntp_enabled: boolean | null;
  ntp_synchronised: boolean | null;
}

function timePayload(overrides: Partial<TimePayload> = {}): TimePayload {
  return {
    epoch_seconds: BROWSER_EPOCH_SECONDS,
    timezone: 'UTC',
    ntp_enabled: false,
    ntp_synchronised: false,
    ...overrides,
  };
}

interface PostRecord { url: string; body: Record<string, unknown> }
let posts: PostRecord[] = [];
let getResponses: TimePayload[] = [];
let setClockStatus = 200;

function installFetch() {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/system/time' && method === 'GET') {
      // Successive GETs walk the queued payloads so a test can assert the card
      // re-reads the board after acting, with the last one repeating.
      const payload = getResponses.length > 1 ? getResponses.shift()! : getResponses[0];
      return jsonResponse(payload);
    }
    if (url === '/api/system/time' && method === 'POST') {
      posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') });
      return setClockStatus === 200
        ? jsonResponse({ success: true, applied: true })
        : jsonResponse({ error: 'Network time sync must be turned off first.' }, setClockStatus);
    }
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
}

beforeEach(() => {
  posts = [];
  setClockStatus = 200;
  getResponses = [timePayload()];
  vi.spyOn(Date, 'now').mockReturnValue(BROWSER_EPOCH_SECONDS * 1000);
  installFetch();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function findSetClockButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: /set from this browser/i }) as HTMLButtonElement;
}

describe('DeviceClockCard offset readout', () => {
  it('reports how far behind the browser the board clock is running', async () => {
    // The exact reported condition. Without this row a board minutes out of step
    // looks identical to a correct one, which is how the original problem stayed
    // hidden. Manifests as a missing offset or one reported as "in step".
    getResponses = [timePayload({ epoch_seconds: BROWSER_EPOCH_SECONDS - OBSERVED_BOARD_SKEW_SECONDS })];
    render(<DeviceClockCard />);
    expect(await screen.findByText(/4m 55s behind this browser/i)).toBeInTheDocument();
  });

  it('reports a board running ahead of the browser', async () => {
    // The mirror case points at a different cause (a clock set from something
    // wrong, rather than never set at all), so the direction must be reported
    // rather than just the magnitude.
    getResponses = [timePayload({ epoch_seconds: BROWSER_EPOCH_SECONDS + OBSERVED_BOARD_SKEW_SECONDS })];
    render(<DeviceClockCard />);
    expect(await screen.findByText(/4m 55s ahead of this browser/i)).toBeInTheDocument();
  });

  it('holds the reported difference steady as the browser clock advances', async () => {
    // Why: the board's epoch is frozen at the moment of the read, so the browser
    // epoch it is compared against has to be frozen at that same moment. Reading
    // a live clock at render time instead makes the difference climb by one
    // second per second the card is left open -- a board exactly 4m55s behind
    // would be reported as steadily falling further behind, which reads as the
    // board's clock having stopped. Manifests here as the 4m55s row being
    // replaced by a larger figure after the browser clock moves on by a minute.
    getResponses = [timePayload({ epoch_seconds: BROWSER_EPOCH_SECONDS - OBSERVED_BOARD_SKEW_SECONDS })];
    const { rerender } = render(<DeviceClockCard />);
    expect(await screen.findByText(/4m 55s behind this browser/i)).toBeInTheDocument();

    // Advance the browser clock and re-render without changing the effect's
    // dependencies, so no re-read happens and the rendered offset is the only
    // thing under test.
    vi.spyOn(Date, 'now').mockReturnValue((BROWSER_EPOCH_SECONDS + 60) * 1000);
    rerender(<DeviceClockCard />);

    expect(screen.getByText(/4m 55s behind this browser/i)).toBeInTheDocument();
    expect(screen.queryByText(/5m 55s behind this browser/i)).not.toBeInTheDocument();
  });

  it('says the clocks agree when the board is in step', async () => {
    // A healthy board must say so plainly. If this rendered an offset of "0s"
    // instead, the row would read as a fault on every correctly synced board.
    render(<DeviceClockCard />);
    expect(await screen.findByText(/in step with this browser/i)).toBeInTheDocument();
  });
});

describe('DeviceClockCard sync status', () => {
  it.each([
    ['sync on and working', { ntp_enabled: true, ntp_synchronised: true }, /^Synchronised$/i],
    ['sync on but unreachable', { ntp_enabled: true, ntp_synchronised: false }, /not synchronised/i],
    ['sync switched off', { ntp_enabled: false, ntp_synchronised: false }, /^Off$/i],
    ['sync state unreadable', { ntp_enabled: null, ntp_synchronised: null }, /^Unknown$/i],
  ])('shows %s distinctly', async (_label, flags, expected) => {
    // "Switched on" and "actually reached a server" are different facts, and the
    // board this was built for reports on/not-synchronised. Showing a single
    // on/off state would tell that user their clock is fine while it drifts.
    getResponses = [timePayload(flags)];
    render(<DeviceClockCard />);
    expect(await screen.findByText(expected)).toBeInTheDocument();
  });
});

describe('DeviceClockCard set-from-browser action', () => {
  it('posts the browser clock and then re-reads the board', async () => {
    // The path that actually corrects a board with no time source. The re-read
    // matters as much as the post: without it the card would keep showing the
    // stale offset and the user could not tell whether the set worked.
    getResponses = [
      timePayload({ epoch_seconds: BROWSER_EPOCH_SECONDS - OBSERVED_BOARD_SKEW_SECONDS }),
      timePayload({ epoch_seconds: BROWSER_EPOCH_SECONDS }),
    ];
    render(<DeviceClockCard />);
    fireEvent.click(await waitFor(findSetClockButton));

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({
      url: '/api/system/time',
      body: { epoch_seconds: BROWSER_EPOCH_SECONDS },
    });
    expect(await screen.findByText(/in step with this browser/i)).toBeInTheDocument();
  });

  it.each([
    ['sync is enabled', { ntp_enabled: true, ntp_synchronised: true }],
    ['the sync state is unknown', { ntp_enabled: null, ntp_synchronised: null }],
  ])('disables the action while %s', async (_label, flags) => {
    // timedatectl refuses to step a clock it is synchronising, and "unknown" is
    // not evidence that it is not. Offering the button anyway produces a failure
    // the user cannot act on. Manifests as an enabled button here.
    getResponses = [timePayload(flags)];
    render(<DeviceClockCard />);
    await waitFor(() => expect(findSetClockButton()).toBeDisabled());
    expect(posts).toHaveLength(0);
  });

  it('explains the refusal when the board rejects the set because sync is on', async () => {
    // Covers the race the disabled button cannot: sync switched on between the
    // card's read and the click. The 409 carries the only actionable part of the
    // message, so it must reach the user rather than becoming a generic failure.
    getResponses = [timePayload({ ntp_enabled: false })];
    setClockStatus = 409;
    render(<DeviceClockCard />);
    fireEvent.click(await waitFor(findSetClockButton));
    expect(await screen.findByText(/turn network time off first/i)).toBeInTheDocument();
  });
});
