/**
 * Primary navigation destinations, in display order.
 *
 * Single source of truth for the top-level pages the user can reach directly.
 * The navbar renders its links from this list, and the PWA manifest's
 * `shortcuts` (which cannot import TypeScript, being static JSON served from
 * `public/`) are checked against it by `manifest.test.ts`.
 *
 * The duplication in `public/manifest.json` was previously unguarded: when the
 * live board moved from `/` to `/board` the navbar was updated but the manifest
 * shortcut was not, so the installed app's "Live Board" shortcut opened the
 * welcome page. Adding or moving a destination means updating both this list
 * and the manifest; the test fails until they agree.
 */
export interface NavDestination {
  /** Route path, exactly as declared in `AppRoutes`. */
  path: string;
  /** i18n key under `nav.` for the navbar link label. */
  labelKey: string;
}

export const PRIMARY_NAV: readonly NavDestination[] = [
  { path: '/board', labelKey: 'nav.liveBoard' },
  { path: '/games', labelKey: 'nav.games' },
  { path: '/positions', labelKey: 'nav.positions' },
  { path: '/settings', labelKey: 'nav.settings' },
] as const;
