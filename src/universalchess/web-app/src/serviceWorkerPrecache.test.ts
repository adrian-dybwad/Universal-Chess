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
