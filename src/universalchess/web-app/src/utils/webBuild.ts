/**
 * Whether the page the browser loaded is still the build on disk.
 *
 * The build scripts stamp one id, a hash of the built files, into index.html
 * (the `uc-build` meta tag) and sw.js. The board reads the id from index.html
 * on disk and sends it on /events. A different id from the one this page
 * loaded is a new frontend. This works whether or not the service worker
 * registered: with an untrusted certificate it never does.
 */

export const BUILD_ID_META_NAME = 'uc-build';

// Left in index.html by a dev server or a build that was not stamped.
const UNSTAMPED_BUILD_ID = '__BUILD_ID__';

/** The build id this page loaded, or null when it has none (dev server, unstamped build). */
export function runningWebBuild(): string | null {
  if (typeof document === 'undefined') return null;
  const content = document
    .querySelector(`meta[name="${BUILD_ID_META_NAME}"]`)
    ?.getAttribute('content')
    ?.trim();
  if (!content || content === UNSTAMPED_BUILD_ID) return null;
  return content;
}

/** True only when both ids are known and they differ. */
export function webBuildChanged(deployed: string | null, running: string | null): boolean {
  if (!deployed || !running) return false;
  return deployed !== running;
}

// Survives the reload. A page that reloads and still loads another build (a
// cache in front of the board) must not reload again.
const RELOAD_TARGET_SESSION_KEY = 'uc:web-build-reload-target';

/** True when this tab already reloaded once to reach `deployed`. */
export function alreadyReloadedFor(deployed: string): boolean {
  try {
    return sessionStorage.getItem(RELOAD_TARGET_SESSION_KEY) === deployed;
  } catch {
    return false;
  }
}

/** Reload onto the build on disk, recording it so a reload that misses shows the banner instead. */
export function reloadOntoDeployedBuild(deployed: string): void {
  try {
    sessionStorage.setItem(RELOAD_TARGET_SESSION_KEY, deployed);
  } catch {
    // Private mode / quota: the reload itself still matters.
  }
  window.location.reload();
}
