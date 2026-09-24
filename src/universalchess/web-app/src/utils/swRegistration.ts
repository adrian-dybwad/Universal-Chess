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
let registrationMissing = false;
let loadError: string | null = null;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let autoFallbackTimer: ReturnType<typeof setTimeout> | null = null;
let controllerChangeHandler: (() => void) | null = null;
let visibilityHandler: (() => void) | null = null;
const autoReloadBlockedListeners = new Set<() => void>();
const registrationMissingListeners = new Set<() => void>();
let watchedWorkers = new WeakSet<object>();

let workerMessageHandler: ((event: MessageEvent) => void) | null = null;

function onWorkerMessage(event: MessageEvent): void {
  const data: unknown = event.data;
  if (
    typeof data === 'object' &&
    data !== null &&
    'type' in data &&
    data.type === 'SW_LOAD_ERROR' &&
    'message' in data &&
    typeof data.message === 'string' &&
    data.message.trim()
  ) {
    markLoadError(data.message.trim());
  }
}

/**
 * An install that never reaches `installed` failed to load. A worker that
 * already installed can later become redundant when a newer one activates;
 * that is not a load failure.
 */
function watchWorker(
  worker: ServiceWorker | null,
  onInstalled: (worker: ServiceWorker) => void,
): void {
  if (!worker || watchedWorkers.has(worker)) return;
  watchedWorkers.add(worker);
  let reachedInstalled = worker.state === 'installed' || worker.state === 'activated';
  worker.addEventListener('statechange', () => {
    if (worker.state === 'installed') {
      reachedInstalled = true;
      clearLoadError();
      onInstalled(worker);
      return;
    }
    if (worker.state === 'activated') {
      reachedInstalled = true;
      clearLoadError();
      return;
    }
    if (worker.state === 'redundant' && !reachedInstalled && loadError === null) {
      markLoadError('The service worker stopped while installing.');
    }
  });
}

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

function formatLoadError(error: unknown): string {
  if (error instanceof Error) {
    const detail = error.message.trim();
    if (error.name && error.name !== 'Error' && detail) return `${error.name}: ${detail}`;
    return detail || error.name || 'Unknown error';
  }
  if (typeof error === 'string' && error.trim()) return error.trim();
  return 'Unknown error';
}

function notifyRegistrationMissing(): void {
  for (const listener of registrationMissingListeners) listener();
}

function markLoadError(message: string): void {
  registrationMissing = true;
  if (loadError === message) return;
  loadError = message;
  notifyRegistrationMissing();
}

function clearLoadError(): void {
  if (!registrationMissing && loadError === null) return;
  registrationMissing = false;
  loadError = null;
  notifyRegistrationMissing();
}

/**
 * Subscribe to the service worker failing to load.
 *
 * The navbar warning reads this with useSyncExternalStore. It stays false
 * until registration has actually failed, so the icon does not flash while
 * the first register() is still in flight.
 */
export function subscribeServiceWorkerMissing(onStoreChange: () => void): () => void {
  registrationMissingListeners.add(onStoreChange);
  return () => {
    registrationMissingListeners.delete(onStoreChange);
  };
}

/** Whether `/sw.js` could not be registered, so this page has no worker. */
export function getServiceWorkerMissing(): boolean {
  return registrationMissing;
}

/** The browser's message for the failed load, or null while the worker is fine. */
export function getServiceWorkerLoadError(): string | null {
  return loadError;
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

  if (!('serviceWorker' in navigator)) {
    markLoadError('Service workers are not available in this browser.');
    return;
  }

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
      clearLoadError();
      registrationRef = registration;
      notifyIfWaiting(registration);
      watchWorker(registration.installing, (worker) => notifyIfWaiting(registration, worker));

      registration.addEventListener('updatefound', () => {
        watchWorker(registration.installing, (worker) => notifyIfWaiting(registration, worker));
      });

      const poll = (): void => {
        void Promise.resolve(registration.update()).catch((error: unknown) => {
          console.error('[PWA] Service Worker update failed:', error);
          markLoadError(formatLoadError(error));
        });
      };
      pollTimer = setInterval(poll, UPDATE_POLL_INTERVAL_MS);
      visibilityHandler = () => {
        if (document.visibilityState === 'visible') poll();
      };
      document.addEventListener('visibilitychange', visibilityHandler);
    })
    .catch((error: unknown) => {
      console.error('[PWA] Service Worker registration failed:', error);
      markLoadError(formatLoadError(error));
    });

  workerMessageHandler = onWorkerMessage;
  navigator.serviceWorker.addEventListener('message', workerMessageHandler);

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
  registrationMissing = false;
  loadError = null;
  watchedWorkers = new WeakSet();
  autoReloadBlockedListeners.clear();
  registrationMissingListeners.clear();
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (autoFallbackTimer !== null) {
    clearTimeout(autoFallbackTimer);
    autoFallbackTimer = null;
  }
  if (typeof navigator !== 'undefined' && 'serviceWorker' in navigator) {
    if (controllerChangeHandler) {
      navigator.serviceWorker.removeEventListener('controllerchange', controllerChangeHandler);
    }
    if (workerMessageHandler) {
      navigator.serviceWorker.removeEventListener('message', workerMessageHandler);
    }
  }
  controllerChangeHandler = null;
  workerMessageHandler = null;
  if (visibilityHandler) {
    document.removeEventListener('visibilitychange', visibilityHandler);
    visibilityHandler = null;
  }
}
