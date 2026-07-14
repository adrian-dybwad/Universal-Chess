/**
 * How the client should apply a newly-downloaded app bundle once one is waiting.
 *
 * - `reload`: activate the new bundle and reload the page now.
 * - `prompt`: leave the running page alone and surface a banner so the user
 *   chooses when to reload.
 */
export type UpdateAction = 'reload' | 'prompt';

/**
 * Decide whether a waiting app update may be applied automatically.
 *
 * Auto-reload is only safe when no one is mid-play on this tab: either there is
 * no game in progress, or the tab is backgrounded (nobody is watching). In any
 * other case the page belongs to an active game in the foreground, so the
 * decision defers to the user via a prompt rather than yanking the board out
 * from under them.
 *
 * Callers must only invoke this once an update is actually waiting; it does not
 * itself track update availability.
 */
export function decideUpdateAction(input: {
  gameInProgress: boolean;
  documentHidden: boolean;
}): UpdateAction {
  if (!input.gameInProgress || input.documentHidden) {
    return 'reload';
  }
  return 'prompt';
}
