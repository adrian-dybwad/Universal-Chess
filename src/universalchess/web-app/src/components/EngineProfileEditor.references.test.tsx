// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { EngineProfileEditor } from './EngineProfileEditor';

/**
 * Saving is prompt-free, and a mutation reports the settings it moved.
 *
 * Why these tests exist
 * ---------------------
 * The editor used to fire `window.confirm` from inside `save` whenever a rung's
 * Elo no longer matched its name, so every save after moving the Elo slider
 * raised a dialog. The name is no longer an identity, and the label is projected
 * from the values, so there is nothing left to drift and no reason to prompt --
 * which is the precondition for the debounced auto-save that now carries these
 * writes, and is why editing a field is all these tests do to trigger one.
 *
 * A profile is the foreign key the player strength settings and the Centaur level
 * store, and mutations now move those references and report it. Reporting is the
 * whole point: a setting left naming a removed profile resolves to the engine's
 * own defaults at game start, so the change was previously undiscoverable except
 * by playing a game at the wrong strength.
 *
 * How a regression manifests
 * --------------------------
 * If a confirmation returns to the save path, the save test sees a `confirm` call
 * (and, with the stub answering false, no POST at all). If a response's
 * `repointed` list is ignored, the notice omits the moved settings entirely and
 * the delete looks like it changed nothing.
 */

vi.mock('./LoginDialog', () => ({
  LoginDialog: () => null,
}));

const ENGINE = 'berserk';
const PROFILE_ID = 'Profile-a1b2c3';
const ELO_FIELD = 'UCI_Elo';

/** A capped profile with no user-authored name: its label is projected. */
const PROFILE = {
  id: PROFILE_ID,
  label: '1400 ELO',
  values: { UCI_LimitStrength: 'true', [ELO_FIELD]: '1400' },
};

function schemaResponse(profile: typeof PROFILE) {
  return {
    engine: ENGINE,
    editable: true,
    schema: [
      {
        id: 'strength',
        label: 'Strength',
        fields: [
          { key: 'UCI_LimitStrength', label: 'Limit strength', type: 'bool', default: false },
          { key: ELO_FIELD, label: 'ELO', type: 'int', default: 2800, min: 800, max: 2800 },
        ],
      },
    ],
    profiles: [profile],
    case_collisions: [],
  };
}

interface RecordedPost {
  url: string;
  body: Record<string, unknown>;
}

/**
 * Stub fetch so the schema load returns ``PROFILE`` and POSTs succeed with
 * ``postBody`` merged into the response. Returns the recorded POSTs.
 */
function mockFetch(postBody: Record<string, unknown> = {}) {
  const posts: RecordedPost[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if ((init?.method ?? 'GET') === 'GET') {
      return { ok: true, status: 200, json: async () => schemaResponse(PROFILE) };
    }
    posts.push({ url, body: JSON.parse(String(init?.body ?? '{}')) });
    return {
      ok: true,
      status: 200,
      json: async () => ({ success: true, ...schemaResponse(PROFILE), ...postBody }),
    };
  });
  vi.stubGlobal('fetch', fetchMock);
  return posts;
}

function renderEditor() {
  return render(
    <EngineProfileEditor engineName={ENGINE} displayName="Berserk" onBack={() => {}} />,
  );
}

describe('EngineProfileEditor saves and reference notices', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it('saves a moved Elo in place without prompting', async () => {
    // The prompt is answered false, so if the save path asks anything, the
    // request is abandoned and the POST assertion fails -- exactly the behaviour
    // that made debounced auto-save impossible.
    const confirmSpy = vi.fn(() => false);
    vi.stubGlobal('confirm', confirmSpy);
    const posts = mockFetch();
    renderEditor();

    const field = await screen.findByRole('spinbutton');
    fireEvent.change(field, { target: { value: '1600' } });

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(confirmSpy).not.toHaveBeenCalled();
    // Addressed by id: the profile's label changes with the Elo, and no rename
    // rides along with the save.
    expect(posts[0].url).toBe(`/api/engines/${ENGINE}/profiles/${PROFILE_ID}`);
    expect(posts[0].body).toEqual({
      values: { UCI_LimitStrength: true, [ELO_FIELD]: 1600 },
    });
  });

  it('sends a name the user typed, without changing which profile is written', async () => {
    // The name is an ordinary value: naming a profile must not create a second
    // one. Failure: a POST to /profiles (the create route) instead of this id,
    // which is what naming used to mean.
    vi.stubGlobal('confirm', () => false);
    const posts = mockFetch();
    renderEditor();

    fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'Club Player' } });

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url).toBe(`/api/engines/${ENGINE}/profiles/${PROFILE_ID}`);
    expect(posts[0].body).toEqual({
      values: { UCI_LimitStrength: true, [ELO_FIELD]: 1400, Name: 'Club Player' },
    });
  });

  it('reports the settings a delete moved off the profile', async () => {
    // Delete is the case the whole repair exists for: the reference is gone from
    // under the setting, and without the notice the strength change is silent.
    vi.stubGlobal('confirm', () => true);
    mockFetch({
      repointed: [{ setting: 'PlayerOne.elo', from: PROFILE_ID, to: 'Default' }],
    });
    renderEditor();

    fireEvent.click(await screen.findByRole('button', { name: /^delete$/i }));

    const notice = await screen.findByText(/Player 1 strength/);
    expect(notice).toHaveTextContent('Default');
  });

  it('reports the settings a reset moved, alongside the reset notice', async () => {
    // Reset discards every custom profile, so it dangles referrers wholesale.
    // Failure: only "Profiles reset" appears and the moved settings are lost.
    vi.stubGlobal('confirm', () => true);
    mockFetch({
      repointed: [{ setting: 'centaur_engine.level', from: PROFILE_ID, to: 'Default' }],
    });
    renderEditor();

    fireEvent.click(await screen.findByRole('button', { name: /reset profiles/i }));

    const notice = await screen.findByText(/Original Centaur level/);
    expect(notice).toHaveTextContent('Default');
  });
});
