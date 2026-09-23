// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { EngineProfileEditor } from './EngineProfileEditor';
import { AUTO_SAVE_DEBOUNCE_MS, DEFAULT_PROFILE_ID } from './engineOptions';

/**
 * Guards Use shared defaults on the per-engine profile editor.
 *
 * Why these tests exist: Hash used to save into a strength profile, so
 * changing RAM on one rung overwrote another. Use shared on must hide the
 * resource fields and keep Hash out of the profile POST. Unchecking must POST
 * the engine defaults endpoint, not a profile section. Overlay Hash max is
 * the device cap from the schema, not Stockfish's advertised 2048.
 *
 * How a regression manifests: Hash is still on screen while Use shared is
 * on, the profile POST contains Hash, unchecking Use shared never hits
 * /defaults, or the Hash slider max is 1024.
 */

vi.mock('./LoginDialog', () => ({
  LoginDialog: () => null,
}));

const ENGINE = 'stockfish';
const PROFILE_ID = 'Profile-a1b2c3';

const SCHEMA_RESPONSE = {
  engine: ENGINE,
  editable: true,
  schema: [
    {
      id: 'strength',
      label: 'Strength',
      fields: [
        { key: 'UCI_LimitStrength', label: 'Limit strength', type: 'bool', default: false },
        { key: 'UCI_Elo', label: 'ELO', type: 'int', default: 2800, min: 800, max: 2800 },
      ],
    },
    {
      id: 'resources',
      label: 'Resources',
      fields: [
        { key: 'Hash', label: 'Hash', type: 'int', default: 16, min: 1, max: 16 },
      ],
    },
  ],
  profiles: [
    { id: DEFAULT_PROFILE_ID, label: 'Default (Unlimited)', values: {} },
    { id: PROFILE_ID, label: '1400 ELO', values: { UCI_LimitStrength: 'true', UCI_Elo: '1400' } },
  ],
  shared: {
    resources: { use_defaults: true, values: { Hash: '16' } },
  },
  case_collisions: [],
};

interface RecordedPost {
  url: string;
  body: Record<string, unknown>;
}

function mockFetch() {
  const posts: RecordedPost[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if ((init?.method ?? 'GET') === 'GET') {
      return { ok: true, status: 200, json: async () => SCHEMA_RESPONSE };
    }
    posts.push({ url, body: JSON.parse(String(init?.body ?? '{}')) });
    if (url.endsWith('/defaults')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          success: true,
          shared: {
            resources: {
              use_defaults: Boolean((posts[posts.length - 1].body as { use_defaults?: boolean }).use_defaults),
              values: { Hash: '16' },
            },
          },
        }),
      };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ success: true, id: PROFILE_ID, ...SCHEMA_RESPONSE }),
    };
  });
  vi.stubGlobal('fetch', fetchMock);
  return posts;
}

function settle(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, AUTO_SAVE_DEBOUNCE_MS * 3));
}

beforeEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  localStorage.clear();
  vi.stubGlobal('confirm', vi.fn(() => false));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('EngineProfileEditor Use shared defaults', () => {
  it('hides Hash while Use shared defaults is on', async () => {
    // Why: a disabled Hash slider on Stockfish's 2048 track looked like a
    // different setting than Shared engine defaults. How a regression
    // manifests: a Hash slider is still in the document while the toggle is on.
    mockFetch();
    render(<EngineProfileEditor engineName={ENGINE} displayName="Stockfish" onBack={() => {}} />);
    expect(await screen.findByRole('heading', { name: 'Resources' })).toBeInTheDocument();
    const toggle = screen.getByRole('switch', { name: 'Use shared defaults' });
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(screen.queryByText('Hash')).not.toBeInTheDocument();
    const hashSlider = screen.queryAllByRole('slider').find((el) => (el as HTMLInputElement).max === '16');
    expect(hashSlider).toBeUndefined();
  });

  it('shows a device-capped Hash slider after Use shared is unchecked', async () => {
    // Why: the overlay must use the same Hash ceiling as Shared engine
    // defaults (16 MB on a constrained board). How a regression manifests:
    // unchecking never reveals Hash, or the slider max is still 1024.
    mockFetch();
    render(<EngineProfileEditor engineName={ENGINE} displayName="Stockfish" onBack={() => {}} />);
    const toggle = await screen.findByRole('switch', { name: 'Use shared defaults' });
    fireEvent.click(toggle);
    const hashSlider = await waitFor(() => {
      const slider = screen.getAllByRole('slider').find((el) => (el as HTMLInputElement).max === '16');
      expect(slider).toBeDefined();
      return slider as HTMLElement;
    });
    expect(hashSlider).not.toBeDisabled();
    expect(screen.getByText('Hash')).toBeInTheDocument();
  });

  it('POSTs the engine defaults endpoint when Use shared is unchecked', async () => {
    const posts = mockFetch();
    render(<EngineProfileEditor engineName={ENGINE} displayName="Stockfish" onBack={() => {}} />);
    const toggle = await screen.findByRole('switch', { name: 'Use shared defaults' });
    fireEvent.click(toggle);
    await waitFor(() => {
      expect(posts.some((post) => post.url === `/api/engines/${ENGINE}/defaults`)).toBe(true);
    });
    const defaultsPost = posts.find((post) => post.url === `/api/engines/${ENGINE}/defaults`);
    expect(defaultsPost?.body).toEqual({ group: 'resources', use_defaults: false });
  });

  it('does not write Hash into a strength profile on auto-save', async () => {
    const posts = mockFetch();
    render(<EngineProfileEditor engineName={ENGINE} displayName="Stockfish" onBack={() => {}} />);
    const picker = await screen.findByRole('combobox');
    fireEvent.change(picker, { target: { value: PROFILE_ID } });
    const eloSlider = await waitFor(() => {
      const sliders = screen.getAllByRole('slider');
      const elo = sliders.find((el) => (el as HTMLInputElement).max === '2800');
      expect(elo).toBeDefined();
      return elo as HTMLElement;
    });
    fireEvent.change(eloSlider, { target: { value: '1700' } });
    await waitFor(() => {
      expect(posts.some((post) => post.url === `/api/engines/${ENGINE}/profiles/${PROFILE_ID}`)).toBe(true);
    });
    await settle();
    const profilePost = posts.find((post) => post.url === `/api/engines/${ENGINE}/profiles/${PROFILE_ID}`);
    expect(profilePost?.body).toEqual({
      values: { UCI_LimitStrength: true, UCI_Elo: 1700 },
    });
  });
});
