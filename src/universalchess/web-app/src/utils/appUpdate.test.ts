import { describe, it, expect } from 'vitest';
import { decideUpdateAction, type UpdateAction } from './appUpdate';

/**
 * Guards the auto-reload policy for app-bundle updates. The whole point of the
 * feature is that a new build reloads on its own when safe but never interrupts
 * a live game the user is watching. A regression here would either strand
 * clients on a stale build (too conservative) or reload mid-move (too eager).
 */
describe('decideUpdateAction', () => {
  const cases: {
    name: string;
    gameInProgress: boolean;
    documentHidden: boolean;
    expected: UpdateAction;
  }[] = [
    {
      // Idle board, tab in view: nothing to interrupt, so reload immediately.
      name: 'no game in progress, visible -> reload',
      gameInProgress: false,
      documentHidden: false,
      expected: 'reload',
    },
    {
      // The one case that must defer: an active game the user is watching.
      name: 'game in progress, visible -> prompt',
      gameInProgress: true,
      documentHidden: false,
      expected: 'prompt',
    },
    {
      // Backgrounded tab: no one is watching even during a game, so a reload
      // (which re-syncs game state from the server on load) is safe.
      name: 'game in progress, hidden -> reload',
      gameInProgress: true,
      documentHidden: true,
      expected: 'reload',
    },
    {
      // Both safe conditions hold; still a reload.
      name: 'no game in progress, hidden -> reload',
      gameInProgress: false,
      documentHidden: true,
      expected: 'reload',
    },
  ];

  it.each(cases)('$name', ({ gameInProgress, documentHidden, expected }) => {
    expect(decideUpdateAction({ gameInProgress, documentHidden })).toBe(expected);
  });
});
