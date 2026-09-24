// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, cleanup } from '@testing-library/react';
import { ServiceWorkerWarning } from './ServiceWorkerWarning';
import {
  startServiceWorkerUpdates,
  __resetServiceWorkerUpdates,
} from '../utils/swRegistration';

/**
 * Guards the status-bar warning that the service worker did not load.
 *
 * Why this exists: register() fails on an untrusted certificate and the page
 * keeps running with no update detection. How a regression manifests: the
 * warning icon is absent after that failure, or it is present when
 * registration succeeded.
 */

beforeEach(() => {
  __resetServiceWorkerUpdates();
});

afterEach(() => {
  cleanup();
  __resetServiceWorkerUpdates();
  vi.unstubAllGlobals();
});

function stubRegister(register: () => Promise<unknown>): void {
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: {
      controller: null,
      register,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    },
  });
}

describe('ServiceWorkerWarning', () => {
  it('shows a warning after the worker script cannot be fetched', async () => {
    stubRegister(() => Promise.reject(new Error('An SSL certificate error occurred when fetching the script.')));
    startServiceWorkerUpdates(vi.fn());
    await Promise.resolve();
    await Promise.resolve();

    const { getByRole } = render(<ServiceWorkerWarning />);
    const warning = getByRole('button', {
      name: /An SSL certificate error occurred when fetching the script/i,
    });
    expect(warning).toBeInTheDocument();
  });

  it('stays hidden once the worker has registered', async () => {
    stubRegister(async () => ({
      scope: '/',
      waiting: null,
      installing: null,
      update: vi.fn(),
      addEventListener: vi.fn(),
    }));
    startServiceWorkerUpdates(vi.fn());
    await Promise.resolve();
    await Promise.resolve();

    const { container } = render(<ServiceWorkerWarning />);
    expect(container).toBeEmptyDOMElement();
  });
});
