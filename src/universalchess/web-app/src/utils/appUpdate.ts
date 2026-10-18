/**
 * How the client should apply a newly published web build.
 *
 * - `reload`: reload the page onto the new build now.
 * - `prompt`: leave the running page alone and surface a banner so the user
 *   chooses when to reload.
 */
export type UpdateAction = 'reload' | 'prompt';

/**
 * Decide whether a new web build may be loaded automatically.
 *
 * Auto-reload happens only when there is no game in progress. A game in
 * progress always gets the prompt, whether or not the tab is visible: a hidden
 * tab used to count as safe, and switching to another window during a game
 * reloaded the board with no banner.
 *
 * Callers must only invoke this once a new build is known; it does not itself
 * track update availability.
 */
export function decideUpdateAction(input: { gameInProgress: boolean }): UpdateAction {
  return input.gameInProgress ? 'prompt' : 'reload';
}
