import { describe, it, expect } from 'vitest';
import { decideUpdateAction, type UpdateAction } from './appUpdate';

/**
 * Guards the auto-reload policy for app-bundle updates. A new build reloads on
 * its own when no game is in progress and never interrupts a game. A regression
 * either strands clients on a stale build (a reload case returns prompt) or
 * reloads mid-game (the game case returns reload).
 */
describe('decideUpdateAction', () => {
  const cases: { name: string; gameInProgress: boolean; expected: UpdateAction }[] = [
    // Nothing to interrupt, so reload immediately.
    { name: 'no game in progress -> reload', gameInProgress: false, expected: 'reload' },
    // A game is never reloaded underneath the player; the banner asks instead.
    { name: 'game in progress -> prompt', gameInProgress: true, expected: 'prompt' },
  ];

  it.each(cases)('$name', ({ gameInProgress, expected }) => {
    expect(decideUpdateAction({ gameInProgress })).toBe(expected);
  });
});
