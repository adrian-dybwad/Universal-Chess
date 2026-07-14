/**
 * Service worker registration and update-lifecycle boundary.
 *
 * This is the thin layer that touches the `navigator.serviceWorker` browser API
 * directly. It registers the PWA service worker, watches for a newly-downloaded
 * build that is waiting to activate, and exposes a way to activate that build
 * and reload. The update *policy* (whether to reload automatically or prompt)
 * lives in the React layer (AppUpdateBanner / decideUpdateAction) and is unit
 * tested by mocking this module, keeping DOM/SW globals out of those tests.
 */

// Always-open board displays rarely navigate, and the browser's own service
// worker update check is infrequent (roughly daily). Poll so a fresh build is
// noticed within about a minute instead.
const UPDATE_POLL_INTERVAL_MS = 60_000;

let started = false;
let waitingWorker: ServiceWorker | null = null;
let applied = false;

/**
 * Register the service worker and begin watching for updates.
 *
 * `onUpdateReady` is invoked once a new build has finished downloading and is
 * waiting to take over (i.e. an update is available for an already-controlled
 * page). It may be called immediately if a waiting build already exists from a
 * prior session. Safe to call more than once; only the first call has effect.
 */
export function startServiceWorkerUpdates(onUpdateReady: () => void): void {
  if (started) return;
  started = true;

  if (!('serviceWorker' in navigator)) return;

  const notifyIfWaiting = (registration: ServiceWorkerRegistration): void => {
    // A waiting worker only means "update available" when another worker is
    // already controlling this page; otherwise it is just the initial install.
    if (registration.waiting && navigator.serviceWorker.controller) {
      waitingWorker = registration.waiting;
      onUpdateReady();
    }
  };

  navigator.serviceWorker
    .register('/sw.js')
    .then((registration) => {
      console.log('[PWA] Service Worker registered:', registration.scope);
      notifyIfWaiting(registration);

      registration.addEventListener('updatefound', () => {
        const installing = registration.installing;
        if (!installing) return;
        installing.addEventListener('statechange', () => {
          if (installing.state === 'installed') {
            notifyIfWaiting(registration);
          }
        });
      });

      const poll = (): void => {
        void registration.update();
      };
      setInterval(poll, UPDATE_POLL_INTERVAL_MS);
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') poll();
      });
    })
    .catch((error) => {
      console.error('[PWA] Service Worker registration failed:', error);
    });

  // A controller change after we asked the waiting worker to activate means the
  // new bundle is now in control; reload once so the page runs it. Guarded by
  // `applied` so the initial install's controller change (first page load) does
  // not trigger a spurious reload.
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!applied) return;
    window.location.reload();
  });
}

/**
 * Activate the waiting build and reload the page.
 *
 * Tells the waiting service worker to skip waiting; the resulting
 * `controllerchange` triggers the reload registered above. If no waiting worker
 * is tracked (e.g. service workers unsupported), falls back to a plain reload
 * so the caller's intent -- get onto the newest code -- still holds. Idempotent:
 * repeated calls (e.g. re-renders) activate at most once.
 */
export function applyServiceWorkerUpdate(): void {
  if (applied) return;
  applied = true;

  if (waitingWorker) {
    waitingWorker.postMessage({ type: 'SKIP_WAITING' });
    return;
  }

  window.location.reload();
}
