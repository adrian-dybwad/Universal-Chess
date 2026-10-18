// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  activateWaitingWorker,
  getServiceWorkerMissing,
  getServiceWorkerLoadError,
  registerServiceWorker,
  __resetServiceWorker,
} from './swRegistration';

/**
 * Guards the service-worker boundary: registration, the load-error text the
 * navbar shows, and handing a waiting worker control before a reload. Update
 * detection is not here; it is the build id on /events.
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
  waiting?: { postMessage: ReturnType<typeof vi.fn> } | null;
} = {}) {
  const registration = {
    waiting: options.waiting ?? null,
    installing: null as { addEventListener: ReturnType<typeof vi.fn>; state: string } | null,
    scope: '/',
    addEventListener: vi.fn(),
  };
  const container = {
    controller: null,
    register: vi.fn(async () => registration),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: container,
  });
  return { registration, container };
}

async function settleRegistration(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  __resetServiceWorker();
});

afterEach(() => {
  __resetServiceWorker();
  Reflect.deleteProperty(navigator, 'serviceWorker');
});

describe('activateWaitingWorker', () => {
  it('tells a waiting worker to take over', async () => {
    // Why: a board tab never closes, so a new worker would otherwise wait
    // forever and a changed sw.js would never run. How a regression
    // manifests: SKIP_WAITING is not posted.
    const waiting = { postMessage: vi.fn() };
    installServiceWorkerMock({ waiting });
    registerServiceWorker();
    await settleRegistration();

    activateWaitingWorker();
    expect(waiting.postMessage).toHaveBeenCalledWith({ type: 'SKIP_WAITING' });
  });

  it('does nothing when registration failed', async () => {
    // Why: with an untrusted certificate there is no registration, and the
    // caller reloads right after this. How a regression manifests: this
    // throws and the reload never runs.
    const { container } = installServiceWorkerMock();
    container.register.mockRejectedValue(new Error('An SSL certificate error occurred when fetching the script.'));
    registerServiceWorker();
    await settleRegistration();

    expect(() => activateWaitingWorker()).not.toThrow();
  });

  it('does not throw when the waiting worker is already gone', async () => {
    // Why: a redundant worker throws on postMessage. How a regression
    // manifests: the exception escapes and the caller's reload never runs.
    const waiting = {
      postMessage: vi.fn(() => {
        throw new Error('InvalidStateError');
      }),
    };
    installServiceWorkerMock({ waiting });
    registerServiceWorker();
    await settleRegistration();

    expect(() => activateWaitingWorker()).not.toThrow();
  });
});

describe('registerServiceWorker', () => {
  it('records a failed registration so the navbar can warn', async () => {
    // Why: an untrusted certificate rejects register() after the page has
    // loaded, and nothing else shows it. How a regression manifests:
    // getServiceWorkerMissing stays false after register rejects.
    const { container } = installServiceWorkerMock();
    container.register.mockRejectedValue(new Error('An SSL certificate error occurred when fetching the script.'));
    registerServiceWorker();
    await settleRegistration();
    expect(getServiceWorkerMissing()).toBe(true);
    expect(getServiceWorkerLoadError()).toBe(
      'An SSL certificate error occurred when fetching the script.',
    );
  });

  it('keeps a worker install error posted to the page', async () => {
    // Why: cache.addAll rejects after register() has already resolved, and
    // the page otherwise has no text for that failure. How a regression
    // manifests: getServiceWorkerLoadError stays null after SW_LOAD_ERROR.
    const { container } = installServiceWorkerMock();
    registerServiceWorker();
    await settleRegistration();
    expect(getServiceWorkerLoadError()).toBeNull();

    const onMessage = (
      container.addEventListener as ReturnType<typeof vi.fn>
    ).mock.calls.find((call: unknown[]) => call[0] === 'message')?.[1] as
      | ((event: MessageEvent) => void)
      | undefined;
    expect(onMessage).toBeDefined();
    onMessage!({ data: { type: 'SW_LOAD_ERROR', message: 'Failed to fetch /icons/icon-192.png' } } as MessageEvent);
    expect(getServiceWorkerLoadError()).toBe('Failed to fetch /icons/icon-192.png');
  });

  it('records an installing worker that never reaches installed', async () => {
    // Why: a script that parses but dies in install goes redundant without
    // rejecting register(). How a regression manifests: the missing flag
    // stays false after that state change.
    const listeners: Listener[] = [];
    const installing = {
      state: 'installing',
      addEventListener: vi.fn((type: string, listener: Listener) => {
        if (type === 'statechange') listeners.push(listener);
      }),
    };
    const { registration } = installServiceWorkerMock();
    registration.installing = installing;
    registerServiceWorker();
    await settleRegistration();

    installing.state = 'redundant';
    for (const listener of listeners) listenerFn(listener)();
    expect(getServiceWorkerLoadError()).toBe('The service worker stopped while installing.');
  });

  it('registers only once', async () => {
    // Why: StrictMode and remounts call this more than once. How a
    // regression manifests: register is called twice.
    const { container } = installServiceWorkerMock();
    registerServiceWorker();
    registerServiceWorker();
    await settleRegistration();
    expect(container.register).toHaveBeenCalledTimes(1);
  });
});
