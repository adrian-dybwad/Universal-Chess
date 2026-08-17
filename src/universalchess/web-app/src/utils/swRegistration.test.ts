// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  startServiceWorkerUpdates,
  applyServiceWorkerUpdate,
  subscribeAutoReloadBlocked,
  getAutoReloadBlocked,
  __resetServiceWorkerUpdates,
} from './swRegistration';

/**
 * Guards the service-worker update apply path. The banner's Reload button is
 * only useful if this module actually navigates onto the waiting build; the
 * React tests mock this boundary, so a dead apply (post SKIP_WAITING and wait
 * forever for controllerchange) would leave Reload looking like a no-op.
 */

type Listener = EventListenerOrEventListenerObject;

function listenerFn(listener: Listener): (event?: Event) => void {
  if (typeof listener === 'function') {
    return (event?: Event) => {
      listener(event as Event);
    };
  }
  return (event?: Event) => listener.handleEvent(event as Event);
}

function installServiceWorkerMock(options: {
  waiting?: { postMessage: ReturnType<typeof vi.fn>; state?: string } | null;
  controller?: object | null;
} = {}) {
  const waiting = options.waiting ?? null;
  const controller =
    options.controller === undefined ? { scriptURL: '/sw.js' } : options.controller;

  const registrationListeners = new Map<string, Listener[]>();
  const containerListeners = new Map<string, Listener[]>();

  const registration = {
    waiting,
    installing: null as { addEventListener: ReturnType<typeof vi.fn>; state: string } | null,
    scope: '/',
    update: vi.fn(),
    addEventListener: vi.fn((type: string, listener: Listener) => {
      const list = registrationListeners.get(type) ?? [];
      list.push(listener);
      registrationListeners.set(type, list);
    }),
  };

  const container = {
    controller,
    register: vi.fn(async () => registration),
    addEventListener: vi.fn((type: string, listener: Listener) => {
      const list = containerListeners.get(type) ?? [];
      list.push(listener);
      containerListeners.set(type, list);
    }),
    removeEventListener: vi.fn((type: string, listener: Listener) => {
      const list = containerListeners.get(type) ?? [];
      containerListeners.set(
        type,
        list.filter((entry) => entry !== listener),
      );
    }),
    dispatchControllerChange() {
      for (const listener of containerListeners.get('controllerchange') ?? []) {
        listenerFn(listener)();
      }
    },
  };

  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: container,
  });

  return { registration, container, waiting };
}

const reloadMock = vi.fn();

beforeEach(() => {
  __resetServiceWorkerUpdates();
  reloadMock.mockReset();
  vi.stubGlobal('location', { reload: reloadMock, href: 'http://localhost/' });
  sessionStorage.clear();
});

afterEach(() => {
  __resetServiceWorkerUpdates();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('applyServiceWorkerUpdate', () => {
  it('reloads immediately on a user tap even while a worker is waiting', async () => {
    // Why: Reload is a user gesture. Waiting for controllerchange before
    // navigating drops that gesture, and iOS Safari (and some kiosk Chromium
    // builds) then ignore location.reload(), so the button appears to do
    // nothing. How a regression manifests: SKIP_WAITING is posted and reload
    // is never called until a later controllerchange that may never come.
    const waiting = { postMessage: vi.fn() };
    const { container } = installServiceWorkerMock({ waiting });
    const onUpdateReady = vi.fn();
    startServiceWorkerUpdates(onUpdateReady);
    await Promise.resolve();
    expect(onUpdateReady).toHaveBeenCalledTimes(1);

    applyServiceWorkerUpdate({ userInitiated: true });

    expect(waiting.postMessage).toHaveBeenCalledWith({ type: 'SKIP_WAITING' });
    expect(reloadMock).toHaveBeenCalledTimes(1);
    container.dispatchControllerChange();
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });

  it('still reloads on a user tap after a previous auto-apply that did not navigate', async () => {
    // Why: auto-apply sets an in-flight flag and posts SKIP_WAITING. If
    // controllerchange never fires, that flag used to make the later Reload
    // tap a no-op. How a regression manifests: the second call (userInitiated)
    // does not increment the reload mock.
    const waiting = { postMessage: vi.fn() };
    installServiceWorkerMock({ waiting });
    startServiceWorkerUpdates(vi.fn());
    await Promise.resolve();

    applyServiceWorkerUpdate();
    expect(reloadMock).not.toHaveBeenCalled();

    applyServiceWorkerUpdate({ userInitiated: true });
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });

  it('notifies subscribers when a second auto-apply is refused', async () => {
    // Why: the banner reads blocked via useSyncExternalStore. If apply
    // refuses without notifying, Reload never appears after a failed
    // in-page auto-apply. How a regression manifests: the listener stays at
    // 0 and getAutoReloadBlocked stays false.
    const waiting = { postMessage: vi.fn() };
    installServiceWorkerMock({ waiting });
    startServiceWorkerUpdates(vi.fn());
    await Promise.resolve();

    const listener = vi.fn();
    subscribeAutoReloadBlocked(listener);
    expect(getAutoReloadBlocked()).toBe(false);

    applyServiceWorkerUpdate();
    expect(getAutoReloadBlocked()).toBe(false);
    expect(listener).not.toHaveBeenCalled();

    applyServiceWorkerUpdate();
    expect(getAutoReloadBlocked()).toBe(true);
    expect(listener).toHaveBeenCalled();
  });

  it('auto-apply posts SKIP_WAITING and reloads once the waiting worker takes control', async () => {
    // Why: idle / backgrounded clients must still activate the waiting build
    // without a tap. Reloading before skipWaiting finishes would loop the
    // banner. How a regression manifests: reload runs before controllerchange,
    // or never runs after it.
    const waiting = { postMessage: vi.fn() };
    const { container } = installServiceWorkerMock({ waiting });
    startServiceWorkerUpdates(vi.fn());
    await Promise.resolve();

    const didReload = applyServiceWorkerUpdate();
    expect(didReload).toBe(true);
    expect(waiting.postMessage).toHaveBeenCalledWith({ type: 'SKIP_WAITING' });
    expect(reloadMock).not.toHaveBeenCalled();

    container.dispatchControllerChange();
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });

  it('auto-apply reloads after a short wait if controllerchange never fires', async () => {
    // Why: some browsers never emit controllerchange after skipWaiting, which
    // would leave an idle kiosk on the stale bundle with no banner. How a
    // regression manifests: advancing past the fallback delay leaves reload
    // at zero calls.
    vi.useFakeTimers({ toFake: ['setTimeout'] });
    const waiting = { postMessage: vi.fn() };
    installServiceWorkerMock({ waiting });
    startServiceWorkerUpdates(vi.fn());
    await Promise.resolve();

    applyServiceWorkerUpdate();
    expect(reloadMock).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1000);
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });

  it('does not auto-reload-loop when a previous attempt already reloaded this tab', async () => {
    // Why: reloading before the waiting worker activates would show the banner
    // again on the next load and auto-apply forever. How a regression
    // manifests: the second auto-apply (fresh module state, same sessionStorage)
    // returns true and calls reload again.
    vi.useFakeTimers({ toFake: ['setTimeout'] });
    const waiting = { postMessage: vi.fn() };
    installServiceWorkerMock({ waiting });
    startServiceWorkerUpdates(vi.fn());
    await Promise.resolve();

    applyServiceWorkerUpdate();
    vi.advanceTimersByTime(1000);
    expect(reloadMock).toHaveBeenCalledTimes(1);

    __resetServiceWorkerUpdates();
    startServiceWorkerUpdates(vi.fn());
    await Promise.resolve();
    reloadMock.mockClear();

    const didReload = applyServiceWorkerUpdate();
    expect(didReload).toBe(false);
    expect(reloadMock).not.toHaveBeenCalled();
  });

  it('reloads immediately when auto-applying with no waiting worker', () => {
    // Why: service workers unsupported, or the waiting reference was lost,
    // must still honour "get onto the newest code". How a regression
    // manifests: apply returns without calling reload.
    installServiceWorkerMock({ waiting: null });
    startServiceWorkerUpdates(vi.fn());

    const didReload = applyServiceWorkerUpdate();
    expect(didReload).toBe(true);
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });
});

describe('startServiceWorkerUpdates', () => {
  it('does not treat the first install as an update', async () => {
    // Why: a waiting worker with no controller is the initial install, not a
    // new build. Notifying then would flash the banner (or auto-reload) on
    // every first visit. How a regression manifests: onUpdateReady runs.
    const waiting = { postMessage: vi.fn() };
    installServiceWorkerMock({ waiting, controller: null });
    const onUpdateReady = vi.fn();
    startServiceWorkerUpdates(onUpdateReady);
    await Promise.resolve();
    expect(onUpdateReady).not.toHaveBeenCalled();
  });

  it('notifies when an installing worker reaches installed while a controller exists', async () => {
    // Why: registration.waiting can still be null in the installed statechange
    // (a Chromium race). The installing worker is already the waiting build
    // and must be the SKIP_WAITING target. How a regression manifests:
    // onUpdateReady never runs, so the banner never appears after a redeploy.
    const installing = {
      state: 'installing',
      postMessage: vi.fn(),
      addEventListener: vi.fn(),
    };
    const { registration } = installServiceWorkerMock({ waiting: null });
    registration.installing = installing;

    const stateListeners: Listener[] = [];
    installing.addEventListener.mockImplementation((type: string, listener: Listener) => {
      if (type === 'statechange') stateListeners.push(listener);
    });

    const onUpdateReady = vi.fn();
    startServiceWorkerUpdates(onUpdateReady);
    await Promise.resolve();
    expect(onUpdateReady).not.toHaveBeenCalled();

    const updatefound = (
      registration.addEventListener as ReturnType<typeof vi.fn>
    ).mock.calls.find((call: unknown[]) => call[0] === 'updatefound')?.[1] as
      | Listener
      | undefined;
    expect(updatefound).toBeDefined();
    listenerFn(updatefound!)();

    installing.state = 'installed';
    for (const listener of stateListeners) listenerFn(listener)();

    expect(onUpdateReady).toHaveBeenCalledTimes(1);
    applyServiceWorkerUpdate({ userInitiated: true });
    expect(installing.postMessage).toHaveBeenCalledWith({ type: 'SKIP_WAITING' });
  });
});
