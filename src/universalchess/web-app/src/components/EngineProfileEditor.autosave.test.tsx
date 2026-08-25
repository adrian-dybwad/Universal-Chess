// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { EngineProfileEditor } from './EngineProfileEditor';
import { AUTO_SAVE_DEBOUNCE_MS, DEFAULT_PROFILE_ID } from './engineOptions';

/**
 * The profile editor saves an existing profile as it is edited.
 *
 * Why these tests exist
 * ---------------------
 * Every other value in this product persists as it is set -- the Settings page
 * and the board menus both auto-save -- so a profile that needed a button was
 * the odd one out, and an edit left unsaved silently played the old strength.
 *
 * Three things have to hold for that to be safe, and each is a test here:
 * a burst of edits must be one write of the final values, not one per keystroke;
 * an unbounded integer cleared for retyping must not be written in its cleared
 * state, because ``toOverridePayload`` reads an empty number as "use the engine
 * default" and would drop the override the user is in the middle of changing;
 * and the reserved Default must not be written at all, since editing it forks a
 * new profile and a debounce would mint one per keystroke.
 *
 * How a regression manifests
 * --------------------------
 * A missing debounce shows up as more than one recorded POST. A missing
 * incomplete-edit gate shows up as a POST while the field is empty, whose body
 * omits the key entirely -- the exact silent reversion to the engine default.
 * A Default that auto-saves shows up as a POST to the creation route with no
 * button ever pressed.
 */

vi.mock('./LoginDialog', () => ({
  LoginDialog: () => null,
}));

const ENGINE = 'berserk';
const PROFILE_ID = 'Profile-a1b2c3';
const HASH_DEFAULT = 16;
const HASH_STORED = '64';

const DEFAULT_PROFILE = {
  id: DEFAULT_PROFILE_ID,
  label: 'Default (Unlimited)',
  values: {},
};

/** A capped profile with no user-authored name: its label is projected. */
const CUSTOM_PROFILE = {
  id: PROFILE_ID,
  label: '1400 ELO',
  values: { UCI_LimitStrength: 'true', UCI_Elo: '1400', Hash: HASH_STORED },
};

const SCHEMA_RESPONSE = {
  engine: ENGINE,
  editable: true,
  schema: [
    {
      id: 'strength',
      label: 'Strength',
      fields: [
        { key: 'UCI_LimitStrength', label: 'Limit strength', type: 'bool', default: false },
        // Bounded, so it renders as a slider: it has no invalid intermediate.
        { key: 'UCI_Elo', label: 'ELO', type: 'int', default: 2800, min: 800, max: 2800 },
      ],
    },
    {
      id: 'engine',
      label: 'Engine',
      // Unbounded, so it renders as a number input that can be emptied. This is
      // the field the cleared-value transient is about.
      fields: [{ key: 'Hash', label: 'Hash', type: 'int', default: HASH_DEFAULT }],
    },
  ],
  profiles: [DEFAULT_PROFILE, CUSTOM_PROFILE],
  case_collisions: [],
};

interface RecordedPost {
  url: string;
  body: Record<string, unknown>;
}

/** Stub fetch so the schema load succeeds and POSTs are recorded and accepted. */
function mockFetch() {
  const posts: RecordedPost[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if ((init?.method ?? 'GET') === 'GET') {
      return { ok: true, status: 200, json: async () => SCHEMA_RESPONSE };
    }
    posts.push({ url, body: JSON.parse(String(init?.body ?? '{}')) });
    return {
      ok: true,
      status: 200,
      json: async () => ({ success: true, id: PROFILE_ID, ...SCHEMA_RESPONSE }),
    };
  });
  vi.stubGlobal('fetch', fetchMock);
  return posts;
}

/** Long enough that a scheduled save has fired, plus room for the request. */
function settle(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, AUTO_SAVE_DEBOUNCE_MS * 3));
}

/** The Elo slider: the only bounded integer in the schema. */
function eloSlider(): HTMLElement {
  return screen.getAllByRole('slider')[0];
}

/**
 * The Hash box. Two number inputs are on screen -- the Elo slider carries its
 * own, in the strength card above -- so Hash is the second in document order.
 */
function hashInput(): HTMLElement {
  return screen.getAllByRole('spinbutton')[1];
}

async function renderEditorOnCustomProfile() {
  render(<EngineProfileEditor engineName={ENGINE} displayName="Berserk" onBack={() => {}} />);
  const picker = await screen.findByRole('combobox');
  fireEvent.change(picker, { target: { value: PROFILE_ID } });
  await waitFor(() => expect(hashInput()).toHaveValue(Number(HASH_STORED)));
}

describe('EngineProfileEditor auto-save', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    localStorage.clear();
    // No save path may prompt: answering false would abandon the write, and any
    // dialog at all would reappear on every debounce tick.
    vi.stubGlobal('confirm', vi.fn(() => false));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it('writes a burst of edits once, with the values it ended on', async () => {
    const posts = mockFetch();
    await renderEditorOnCustomProfile();

    fireEvent.change(eloSlider(), { target: { value: '1500' } });
    fireEvent.change(eloSlider(), { target: { value: '1600' } });
    fireEvent.change(eloSlider(), { target: { value: '1700' } });

    await waitFor(() => expect(posts).toHaveLength(1));
    await settle();
    // Still one: dragging a slider is a single write, not one per pixel.
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toBe(`/api/engines/${ENGINE}/profiles/${PROFILE_ID}`);
    expect(posts[0].body).toEqual({
      values: { UCI_LimitStrength: true, UCI_Elo: 1700, Hash: Number(HASH_STORED) },
    });
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it('leaves a cleared number unwritten until it has been retyped', async () => {
    const posts = mockFetch();
    await renderEditorOnCustomProfile();

    // Clearing the box to type a new number is the transient: the form now says
    // "no override", which is a real value the user did not ask for.
    fireEvent.change(hashInput(), { target: { value: '' } });
    await settle();
    expect(posts).toHaveLength(0);

    fireEvent.change(hashInput(), { target: { value: '40' } });

    await waitFor(() => expect(posts).toHaveLength(1));
    // 40 is written, and Hash is present: had the cleared state been saved, the
    // key would have been absent and the engine would have used its own 16.
    expect(posts[0].body).toEqual({
      values: { UCI_LimitStrength: true, UCI_Elo: 1400, Hash: 40 },
    });
  });

  it('never writes the reserved Default on its own', async () => {
    const posts = mockFetch();
    render(<EngineProfileEditor engineName={ENGINE} displayName="Berserk" onBack={() => {}} />);
    await waitFor(() => expect(screen.getAllByRole('slider')).toHaveLength(1));

    fireEvent.change(eloSlider(), { target: { value: '1200' } });
    await settle();
    expect(posts).toHaveLength(0);

    // The edit is offered as a new profile instead, which needs the press: each
    // one mints an identity, so it is the single save that cannot repeat harmlessly.
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }));

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url).toBe(`/api/engines/${ENGINE}/profiles`);
    expect(posts[0].body).toEqual({ values: { UCI_Elo: 1200 } });
  });
});
