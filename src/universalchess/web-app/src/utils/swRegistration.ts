/**
 * Service worker registration boundary.
 *
 * This is the thin layer that touches the `navigator.serviceWorker` browser
 * API. It registers the PWA worker (the offline cache), records why the worker
 * failed to load so the navbar can say so, and hands a waiting worker control
 * before the page reloads onto a new build.
 *
 * It does not detect new builds. That is the build id on /events (see
 * webBuild), which also works when registration fails on an untrusted
 * certificate. Navigations are network-first in sw.js, so a plain reload
 * reaches the new index.html whichever worker is in control.
 */

let started = false;
let registrationRef: ServiceWorkerRegistration | null = null;
let registrationMissing = false;
let loadError: string | null = null;
let workerMessageHandler: ((event: MessageEvent) => void) | null = null;
const registrationMissingListeners = new Set<() => void>();
let watchedWorkers = new WeakSet<object>();

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
function watchWorker(worker: ServiceWorker | null): void {
  if (!worker || watchedWorkers.has(worker)) return;
  watchedWorkers.add(worker);
  let reachedInstalled = worker.state === 'installed' || worker.state === 'activated';
  worker.addEventListener('statechange', () => {
    if (worker.state === 'installed' || worker.state === 'activated') {
      reachedInstalled = true;
      clearLoadError();
      return;
    }
    if (worker.state === 'redundant' && !reachedInstalled && loadError === null) {
      markLoadError('The service worker stopped while installing.');
    }
  });
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

/** Register the service worker. Safe to call more than once; only the first call has effect. */
export function registerServiceWorker(): void {
  if (started) return;
  started = true;

  if (!('serviceWorker' in navigator)) {
    markLoadError('Service workers are not available in this browser.');
    return;
  }

  workerMessageHandler = onWorkerMessage;
  navigator.serviceWorker.addEventListener('message', workerMessageHandler);

  navigator.serviceWorker
    .register('/sw.js')
    .then((registration) => {
      console.log('[PWA] Service Worker registered:', registration.scope);
      clearLoadError();
      registrationRef = registration;
      watchWorker(registration.installing);
      registration.addEventListener('updatefound', () => {
        watchWorker(registration.installing);
      });
    })
    .catch((error: unknown) => {
      console.error('[PWA] Service Worker registration failed:', error);
      markLoadError(formatLoadError(error));
    });
}

/**
 * Tell a waiting worker to take over. Called just before reloading onto a
 * new build.
 *
 * A reload keeps the tab open, so a new worker would otherwise wait until
 * every board tab closes, and a changed sw.js would never run. Does nothing
 * when there is no registration or no waiting worker, and never throws, so
 * the caller's reload always runs.
 */
export function activateWaitingWorker(): void {
  const waiting = registrationRef?.waiting;
  if (!waiting) return;
  try {
    waiting.postMessage({ type: 'SKIP_WAITING' });
  } catch {
    // A redundant or terminated worker throws; the reload still matters.
  }
}

/**
 * Reset module state. Test-only: this module is a process-wide singleton, so
 * registration state otherwise leaks across cases.
 */
export function __resetServiceWorker(): void {
  if (
    workerMessageHandler &&
    typeof navigator !== 'undefined' &&
    'serviceWorker' in navigator
  ) {
    navigator.serviceWorker.removeEventListener('message', workerMessageHandler);
  }
  started = false;
  registrationRef = null;
  registrationMissing = false;
  loadError = null;
  workerMessageHandler = null;
  watchedWorkers = new WeakSet();
  registrationMissingListeners.clear();
}
