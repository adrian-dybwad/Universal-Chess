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

// Some browsers never emit `controllerchange` after skipWaiting. Auto-apply
// waits this long, then reloads anyway so an idle kiosk does not sit on a
// stale bundle with no banner.
const AUTO_RELOAD_FALLBACK_MS = 500;

// Survives the reload so a waiting worker that did not activate cannot
// auto-apply in a loop. Cleared when registration finds no waiting build.
const AUTO_RELOAD_SESSION_KEY = 'uc:sw-update-auto-reload';

let started = false;
let waitingWorker: ServiceWorker | null = null;
let registrationRef: ServiceWorkerRegistration | null = null;
let autoApplyStarted = false;
let autoReloadBlocked = false;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let autoFallbackTimer: ReturnType<typeof setTimeout> | null = null;
let controllerChangeHandler: (() => void) | null = null;
let visibilityHandler: (() => void) | null = null;
const autoReloadBlockedListeners = new Set<() => void>();

function currentWaitingWorker(): ServiceWorker | null {
  return registrationRef?.waiting ?? waitingWorker;
}

function postSkipWaiting(worker: ServiceWorker | null): void {
  if (!worker) return;
  try {
    worker.postMessage({ type: 'SKIP_WAITING' });
  } catch {
    // A redundant or terminated worker throws; the caller still reloads.
  }
}

function writeAutoReloadAttempt(): void {
  try {
    sessionStorage.setItem(AUTO_RELOAD_SESSION_KEY, '1');
  } catch {
    // Private mode / quota: the reload itself still matters.
  }
}

function clearAutoReloadAttempt(): void {
  try {
    sessionStorage.removeItem(AUTO_RELOAD_SESSION_KEY);
  } catch {
    // Ignore: a leftover flag only delays the next auto-apply, never bricks it.
  }
  markAutoReloadBlocked(false);
}

function autoReloadAlreadyAttempted(): boolean {
  try {
    return sessionStorage.getItem(AUTO_RELOAD_SESSION_KEY) === '1';
  } catch {
    return false;
  }
}

function markAutoReloadBlocked(value: boolean): void {
  if (autoReloadBlocked === value) return;
  autoReloadBlocked = value;
  for (const listener of autoReloadBlockedListeners) listener();
}

/**
 * Subscribe to auto-apply being blocked for this tab.
 *
 * The banner reads this with useSyncExternalStore so a refused auto-apply
 * can surface Reload without setState inside an effect.
 */
export function subscribeAutoReloadBlocked(onStoreChange: () => void): () => void {
  autoReloadBlockedListeners.add(onStoreChange);
  return () => {
    autoReloadBlockedListeners.delete(onStoreChange);
  };
}

/**
 * Whether auto-apply has already tried (and must not loop) on this tab.
 *
 * True after a refused auto-apply, or when a previous load recorded an
 * attempt in sessionStorage and the waiting worker is still there.
 */
export function getAutoReloadBlocked(): boolean {
  return autoReloadBlocked || autoReloadAlreadyAttempted();
}

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

  const notifyIfWaiting = (
    registration: ServiceWorkerRegistration,
    worker?: ServiceWorker | null,
  ): void => {
    // A waiting worker only means "update available" when another worker is
    // already controlling this page; otherwise it is just the initial install.
    // `registration.waiting` can still be null in the installing->installed
    // statechange (a Chromium race); the installing worker is then the waiting
    // build and must be the SKIP_WAITING target.
    const waiting = worker ?? registration.waiting;
    if (waiting && navigator.serviceWorker.controller) {
      waitingWorker = waiting;
      onUpdateReady();
      return;
    }
    if (!waiting) {
      clearAutoReloadAttempt();
    }
  };

  navigator.serviceWorker
    .register('/sw.js')
    .then((registration) => {
      console.log('[PWA] Service Worker registered:', registration.scope);
      registrationRef = registration;
      notifyIfWaiting(registration);

      registration.addEventListener('updatefound', () => {
        const installing = registration.installing;
        if (!installing) return;
        installing.addEventListener('statechange', () => {
          if (installing.state === 'installed') {
            notifyIfWaiting(registration, installing);
          }
        });
      });

      const poll = (): void => {
        void registration.update();
      };
      pollTimer = setInterval(poll, UPDATE_POLL_INTERVAL_MS);
      visibilityHandler = () => {
        if (document.visibilityState === 'visible') poll();
      };
      document.addEventListener('visibilitychange', visibilityHandler);
    })
    .catch((error) => {
      console.error('[PWA] Service Worker registration failed:', error);
    });

  // A controller change after we asked the waiting worker to activate means the
  // new bundle is now in control. Auto-apply reloads here; a user tap reloads
  // in the click handler itself (iOS Safari ignores reload() from this event
  // because it is not a user gesture).
  controllerChangeHandler = () => {
    if (!autoApplyStarted) return;
    writeAutoReloadAttempt();
    window.location.reload();
  };
  navigator.serviceWorker.addEventListener('controllerchange', controllerChangeHandler);
}

/**
 * Activate the waiting build and reload the page.
 *
 * Tells the waiting service worker to skip waiting. A user-initiated call
 * (the banner's Reload tap) reloads in the same turn so the navigation counts
 * as a user gesture -- waiting for `controllerchange` is what made Reload
 * appear to do nothing on iOS Safari. Auto-apply still waits for that event
 * (with a short fallback) so a reload cannot race skipWaiting and loop.
 *
 * Returns whether a reload was started. `false` means auto-apply refused
 * (already tried this tab) so the caller should surface the prompt instead.
 */
export function applyServiceWorkerUpdate(options?: { userInitiated?: boolean }): boolean {
  const worker = currentWaitingWorker();
  postSkipWaiting(worker);

  if (options?.userInitiated) {
    // Record the attempt so a reload that races skipWaiting cannot then
    // auto-apply in a loop on the next load; the prompt stays available.
    writeAutoReloadAttempt();
    window.location.reload();
    return true;
  }

  if (autoApplyStarted) {
    markAutoReloadBlocked(true);
    return false;
  }
  autoApplyStarted = true;

  if (autoReloadAlreadyAttempted()) {
    markAutoReloadBlocked(true);
    return false;
  }

  const reload = (): void => {
    writeAutoReloadAttempt();
    window.location.reload();
  };

  if (!worker) {
    reload();
    return true;
  }

  autoFallbackTimer = setTimeout(reload, AUTO_RELOAD_FALLBACK_MS);
  return true;
}

/**
 * Reset module state. Test-only: the registration flags and timers otherwise
 * leak across cases because this module is a process-wide singleton.
 */
export function __resetServiceWorkerUpdates(): void {
  started = false;
  waitingWorker = null;
  registrationRef = null;
  autoApplyStarted = false;
  autoReloadBlocked = false;
  autoReloadBlockedListeners.clear();
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (autoFallbackTimer !== null) {
    clearTimeout(autoFallbackTimer);
    autoFallbackTimer = null;
  }
  if (controllerChangeHandler && typeof navigator !== 'undefined' && 'serviceWorker' in navigator) {
    navigator.serviceWorker.removeEventListener('controllerchange', controllerChangeHandler);
  }
  controllerChangeHandler = null;
  if (visibilityHandler) {
    document.removeEventListener('visibilitychange', visibilityHandler);
    visibilityHandler = null;
  }
}
