import { describe, it, expect } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

/**
 * Guards the service worker's install-time precache list against referencing
 * assets that do not exist.
 *
 * `cache.addAll()` is atomic: one 404 rejects the install, the worker never
 * activates, and the app loses offline support and the update banner without
 * any build or test failing. The list is a hand-maintained copy of paths that
 * live in `public/`, so a renamed or deleted asset breaks it silently.
 *
 * The list is read from source text rather than by importing `sw.js`, because
 * that module registers `self.addEventListener` handlers at load time and has
 * no module exports; there is nothing to import in a non-worker environment.
 */

const thisDir = dirname(fileURLToPath(import.meta.url));
const webAppDir = join(thisDir, '..');
const publicDir = join(webAppDir, 'public');
const swPath = join(publicDir, 'sw.js');

/** Paths the SPA serves from its HTML entry point rather than from `public/`. */
const HTML_ENTRY_PATHS = new Set(['/', '/index.html']);

function readPrecachedPaths(): string[] {
  const source = readFileSync(swPath, 'utf-8');
  const declaration = /const STATIC_ASSETS\s*=\s*\[([\s\S]*?)\];/.exec(source);
  if (!declaration) {
    throw new Error('STATIC_ASSETS array not found in sw.js');
  }
  return [...declaration[1].matchAll(/['"]([^'"]+)['"]/g)].map((match) => match[1]);
}

describe('service worker precache', () => {
  it('declares a non-empty asset list', () => {
    // Why: every assertion below is vacuous against an empty list, so a parse
    // that silently matched nothing would make this suite pass while checking
    // nothing. How a regression manifests: the regex stops matching the
    // declaration (reformatted or renamed) and the count drops to zero.
    expect(readPrecachedPaths().length).toBeGreaterThan(0);
  });

  it('references only assets that exist on disk', () => {
    // Why: cache.addAll() is atomic, so one missing file blocks the whole
    // service worker install. How a regression manifests: an asset is renamed
    // or removed from public/ without updating sw.js, and it is listed here by
    // name instead of the empty array.
    const missing = readPrecachedPaths().filter((path) => {
      if (HTML_ENTRY_PATHS.has(path)) return !existsSync(join(webAppDir, 'index.html'));
      return !existsSync(join(publicDir, path.replace(/^\//, '')));
    });
    expect(missing).toEqual([]);
  });

  it('precaches every icon the manifest declares', () => {
    // Why: the manifest is precached, so the icons it points at should be too;
    // otherwise an install performed against an unreachable board renders the
    // app with no icon. How a regression manifests: an icon is added to the
    // manifest (as the maskable variants were) but not to sw.js, and it appears
    // in the difference below.
    const precached = new Set(readPrecachedPaths());
    const manifest = JSON.parse(readFileSync(join(publicDir, 'manifest.json'), 'utf-8'));
    const iconSources: string[] = manifest.icons.map((icon: { src: string }) => icon.src);
    expect(iconSources.filter((src) => !precached.has(src))).toEqual([]);
  });
});

describe('service worker hashed assets', () => {
  const source = readFileSync(swPath, 'utf-8');

  it('serves hashed files and packaged images from the cache before the network', () => {
    // Why: the worker handles these requests. A network-first handler downloads
    // the bundle and the logo on every page load even when a copy is stored
    // and the HTTP cache says immutable. How a regression manifests: /assets/,
    // /icons/, /images/, or /logo is missing from the cache-first condition,
    // or that function calls fetch before caches.match.
    const fetchHandler = source.split("addEventListener('fetch'")[1]?.split('function cacheFirstHashedAsset')[0] ?? '';
    expect(fetchHandler).toContain("pathname.startsWith('/assets/')");
    expect(fetchHandler).toContain("pathname.startsWith('/icons/')");
    expect(fetchHandler).toContain("pathname.startsWith('/images/')");
    expect(fetchHandler).toContain("pathname === '/logo'");
    expect(fetchHandler).toMatch(/event\.respondWith\(cacheFirstHashedAsset\(request\)\)/);
    const fn = source.split('function cacheFirstHashedAsset')[1] ?? '';
    const matchAt = fn.indexOf('caches.match');
    const fetchAt = fn.indexOf('fetch(request)');
    expect(matchAt).toBeGreaterThanOrEqual(0);
    expect(fetchAt).toBeGreaterThan(matchAt);
  });
});

describe('service worker skipWaiting', () => {
  const source = readFileSync(swPath, 'utf-8');

  it('keeps the worker alive until skipWaiting settles', () => {
    // Why: a bare self.skipWaiting() in the message handler can be killed
    // before the waiting worker activates, so Reload posts SKIP_WAITING and
    // nothing happens. How a regression manifests: the SKIP_WAITING branch
    // calls skipWaiting without event.waitUntil.
    expect(source).toMatch(/type === 'SKIP_WAITING'[\s\S]*event\.waitUntil\(\s*self\.skipWaiting\(\)\s*\)/);
  });

  it('claims clients as part of activate waitUntil', () => {
    // Why: clients.claim() outside waitUntil can run after the activate
    // event is discarded, so controllerchange never fires for auto-apply.
    // How a regression manifests: claim() appears after the waitUntil
    // callback rather than chained inside it.
    expect(source).toMatch(/event\.waitUntil\([\s\S]*self\.clients\.claim\(\)/);
    const activateHandler = source.split("self.addEventListener('activate'")[1] ?? '';
    const afterWaitUntil = activateHandler.split('event.waitUntil')[1] ?? '';
    const waitUntilBlock = afterWaitUntil.split("self.addEventListener('fetch'")[0] ?? '';
    const claimOutsideWaitUntil = /}\s*\);\s*self\.clients\.claim\(\)/.test(waitUntilBlock);
    expect(claimOutsideWaitUntil).toBe(false);
  });
});
