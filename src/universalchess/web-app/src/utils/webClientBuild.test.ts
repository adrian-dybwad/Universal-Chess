// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { runningWebClient, webClientChanged } from './webClientBuild';

/**
 * Guards the fallback that notices a redeployed bundle without a service worker.
 *
 * Why: an untrusted certificate rejects the worker, so the waiting-worker
 * signal never fires. How a regression manifests: webClientChanged is false
 * when the streamed script src differs from the one this page loaded, or
 * true when either side is still unknown.
 */

describe('webClientChanged', () => {
  it('is a change only when both script srcs are known and differ', () => {
    expect(webClientChanged('/assets/index-new.js', '/assets/index-old.js')).toBe(true);
    expect(webClientChanged('/assets/index-old.js', '/assets/index-old.js')).toBe(false);
    expect(webClientChanged(null, '/assets/index-old.js')).toBe(false);
    expect(webClientChanged('/assets/index-new.js', null)).toBe(false);
  });

  it('reads the module script src this document loaded', () => {
    document.body.innerHTML = '<script type="module" src="/assets/index-old.js"></script>';
    expect(runningWebClient()).toBe('/assets/index-old.js');
  });
});
