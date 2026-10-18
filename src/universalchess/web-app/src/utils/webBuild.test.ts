// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  BUILD_ID_META_NAME,
  alreadyReloadedFor,
  reloadOntoDeployedBuild,
  runningWebBuild,
  webBuildChanged,
} from './webBuild';

/**
 * Guards the one signal that tells an open tab a new frontend was published:
 * the build id this page loaded against the id in index.html on disk.
 */

const RUNNING = '1111aaaa2222bbbb';
const DEPLOYED = '3333cccc4444dddd';
const UNSTAMPED = '__BUILD_ID__';

function setBuildMeta(content: string | null): void {
  document.head.querySelector(`meta[name="${BUILD_ID_META_NAME}"]`)?.remove();
  if (content === null) return;
  const meta = document.createElement('meta');
  meta.name = BUILD_ID_META_NAME;
  meta.content = content;
  document.head.appendChild(meta);
}

const reloadMock = vi.fn();

beforeEach(() => {
  reloadMock.mockReset();
  vi.stubGlobal('location', { reload: reloadMock });
  sessionStorage.clear();
});

afterEach(() => {
  setBuildMeta(null);
  vi.unstubAllGlobals();
});

describe('webBuildChanged', () => {
  // Why: this comparison is the whole update decision, with or without a
  // service worker. How a regression manifests: an unchanged publish reads as
  // a new build (the banner for nothing), a real one is missed, or an unknown
  // side (no snapshot yet, a dev page) reloads the page.
  it.each([
    { deployed: DEPLOYED, running: RUNNING, changed: true },
    { deployed: RUNNING, running: RUNNING, changed: false },
    { deployed: null, running: RUNNING, changed: false },
    { deployed: DEPLOYED, running: null, changed: false },
    { deployed: null, running: null, changed: false },
  ])('is $changed for deployed=$deployed running=$running', ({ deployed, running, changed }) => {
    expect(webBuildChanged(deployed, running)).toBe(changed);
  });
});

describe('runningWebBuild', () => {
  it('reads the stamped build id this page loaded', () => {
    // Why: the page's own id is the left side of the comparison. How a
    // regression manifests: null here and no update is ever noticed.
    setBuildMeta(RUNNING);
    expect(runningWebBuild()).toBe(RUNNING);
  });

  it.each([
    { label: 'no meta tag', content: null },
    { label: 'the unstamped placeholder', content: UNSTAMPED },
    { label: 'an empty id', content: '' },
  ])('is null for $label', ({ content }) => {
    // Why: a dev server or an unstamped build has no id to compare. How a
    // regression manifests: the placeholder compares unequal to the board's
    // id and the page reloads on every snapshot.
    setBuildMeta(content);
    expect(runningWebBuild()).toBeNull();
  });
});

describe('reloadOntoDeployedBuild', () => {
  it('records the target and reloads, so a second attempt is recognised', () => {
    // Why: a cache in front of the board can serve the old page again. The
    // record is what turns the next attempt into a banner instead of a reload
    // loop. How a regression manifests: alreadyReloadedFor stays false after
    // the reload, or reload is not called.
    expect(alreadyReloadedFor(DEPLOYED)).toBe(false);
    reloadOntoDeployedBuild(DEPLOYED);
    expect(reloadMock).toHaveBeenCalledTimes(1);
    expect(alreadyReloadedFor(DEPLOYED)).toBe(true);
    expect(alreadyReloadedFor(RUNNING)).toBe(false);
  });
});
