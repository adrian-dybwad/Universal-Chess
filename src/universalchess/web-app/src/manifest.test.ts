import { describe, it, expect } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import sharp from 'sharp';
import { PRIMARY_NAV } from './config/navigation';

/**
 * Guards the PWA manifest against drifting from the app it describes.
 *
 * `public/manifest.json` is static JSON: nothing in the build resolves its URLs
 * or its icon references, so every value in it is a hand-maintained copy of
 * something declared elsewhere. Each test below pins one of those copies to its
 * real source and states how the corresponding regression surfaces.
 */

const thisDir = dirname(fileURLToPath(import.meta.url));
const publicDir = join(thisDir, '..', 'public');
const manifestPath = join(publicDir, 'manifest.json');
const indexHtmlPath = join(thisDir, '..', 'index.html');

interface ManifestIcon {
  src: string;
  sizes: string;
  type: string;
  purpose?: string;
}

interface ManifestShortcut {
  name: string;
  url: string;
  description?: string;
}

interface Manifest {
  name: string;
  short_name: string;
  description: string;
  id?: string;
  start_url: string;
  scope?: string;
  lang?: string;
  display: string;
  theme_color: string;
  background_color: string;
  icons: ManifestIcon[];
  shortcuts: ManifestShortcut[];
  screenshots?: unknown[];
}

const manifest: Manifest = JSON.parse(readFileSync(manifestPath, 'utf-8'));

/**
 * Fraction of the canvas width/height guaranteed visible under any Android
 * adaptive-icon mask. The spec reserves the central 80% as the safe zone; the
 * worst-case mask is a circle inscribed in it, so content must stay within a
 * radius of 40% of the canvas from the centre.
 */
const MASKABLE_SAFE_ZONE_FRACTION = 0.8;

/**
 * Per-channel sum-of-absolute-difference above which a pixel counts as icon
 * content rather than background. The artwork is a hard-edged black-and-white
 * drawing on a flat colour field, so anything beyond mild PNG quantisation
 * noise is real content. 30 is far below the smallest real contrast (white or
 * black against the purple field) and far above the observed noise floor.
 */
const BACKGROUND_TOLERANCE = 30;

interface ContentBounds {
  size: number;
  /** Largest distance from the canvas centre to a content pixel, in pixels. */
  maxRadius: number;
}

/**
 * Measure how far an icon's visible content extends from the canvas centre.
 *
 * The pixel at (0, 0) is taken as the background reference: a maskable icon is
 * required to be full-bleed, so its corner is background by construction.
 * Fully transparent pixels are background regardless of colour.
 */
async function measureContent(iconPath: string): Promise<ContentBounds> {
  const { data, info } = await sharp(iconPath)
    .ensureAlpha()
    .raw()
    .toBuffer({ resolveWithObject: true });
  const { width, height, channels } = info;
  const bgR = data[0];
  const bgG = data[1];
  const bgB = data[2];
  const centre = (width - 1) / 2;
  let maxRadiusSquared = 0;

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const offset = (y * width + x) * channels;
      if (data[offset + 3] === 0) continue;
      const delta =
        Math.abs(data[offset] - bgR) +
        Math.abs(data[offset + 1] - bgG) +
        Math.abs(data[offset + 2] - bgB);
      if (delta <= BACKGROUND_TOLERANCE) continue;
      const radiusSquared = (x - centre) ** 2 + (y - centre) ** 2;
      if (radiusSquared > maxRadiusSquared) maxRadiusSquared = radiusSquared;
    }
  }

  return { size: width, maxRadius: Math.sqrt(maxRadiusSquared) };
}

function iconFilePath(icon: ManifestIcon): string {
  return join(publicDir, icon.src.replace(/^\//, ''));
}

function purposes(icon: ManifestIcon): string[] {
  return (icon.purpose ?? 'any').split(/\s+/).filter(Boolean);
}

describe('PWA manifest shortcuts', () => {
  it('lists exactly the primary navigation destinations, in nav order', () => {
    // Why: the shortcuts are a hand-copied duplicate of the navbar's
    // destinations. How a regression manifests: moving or renaming a route (as
    // when the live board moved from "/" to "/board") updates PRIMARY_NAV but
    // not the manifest, so this comparison reports the stale URL. An installed
    // app's shortcut would otherwise silently open the wrong page.
    expect(manifest.shortcuts.map((shortcut) => shortcut.url)).toEqual(
      PRIMARY_NAV.map((destination) => destination.path)
    );
  });

  it('gives every shortcut a distinct, non-empty name and description', () => {
    // Why: the OS shortcut menu shows only these strings; a blank or duplicated
    // one is unusable. How a regression manifests: an added shortcut copied from
    // a sibling entry leaves the name unchanged, so the uniqueness check fails.
    const names = manifest.shortcuts.map((shortcut) => shortcut.name);
    const descriptions = manifest.shortcuts.map((shortcut) => shortcut.description);
    expect(names.every((name) => name.trim().length > 0)).toBe(true);
    expect(new Set(names).size).toBe(names.length);
    expect(descriptions.every((text) => (text ?? '').trim().length > 0)).toBe(true);
  });
});

describe('PWA manifest identity', () => {
  it('scopes and starts the app at the site root Flask serves the SPA from', () => {
    // Why: Flask serves the built SPA from "/" via a catch-all, so any narrower
    // scope would push in-app navigation out of the installed window and into a
    // browser tab. How a regression manifests: start_url or scope changes to a
    // sub-path and these equality checks fail.
    expect(manifest.start_url).toBe('/');
    expect(manifest.scope).toBe('/');
  });

  it('pins an explicit id matching the historical start_url-derived identity', () => {
    // Why: with no `id`, the browser derives app identity from start_url, so a
    // later start_url change would register as a different app and orphan
    // existing installs. Pinning "/" freezes today's derived identity. How a
    // regression manifests: `id` is dropped or set to another value here.
    expect(manifest.id).toBe('/');
  });

  it('declares the language its own strings are written in', () => {
    // Why: the app UI localizes, but the manifest strings are static English;
    // without `lang` a non-English OS may mis-render or mis-announce them. How a
    // regression manifests: `lang` is absent, so this reads undefined.
    expect(manifest.lang).toBe('en');
  });

  it('omits empty optional collections rather than declaring them empty', () => {
    // Why: `"screenshots": []` states the app has no screenshots, which is the
    // same as omitting the member but implies it was considered and left blank.
    // How a regression manifests: an empty array is reintroduced here.
    expect(manifest.screenshots).toBeUndefined();
  });

  it('agrees with the index.html theme-color meta tag', () => {
    // Why: the browser applies the meta tag before the manifest loads and the
    // manifest value afterwards; a mismatch flashes one colour then the other in
    // the address bar and splash screen. How a regression manifests: one of the
    // two is recoloured and this comparison reports the divergent pair.
    const html = readFileSync(indexHtmlPath, 'utf-8');
    const match = /<meta\s+name="theme-color"\s+content="([^"]+)"/i.exec(html);
    expect(match).not.toBeNull();
    expect(manifest.theme_color.toLowerCase()).toBe(match![1].toLowerCase());
  });
});

describe('PWA manifest icons', () => {
  it('references icon files that exist at their declared pixel size', () => {
    // Why: a manifest icon URL is resolved only by the browser at install time,
    // so a renamed or regenerated asset fails silently in the field. How a
    // regression manifests: the file is missing (existence check) or was resized
    // without updating `sizes`, so the declared and actual dimensions differ.
    for (const icon of manifest.icons) {
      const path = iconFilePath(icon);
      expect(existsSync(path), `${icon.src} is missing from public/`).toBe(true);
      const [declaredWidth, declaredHeight] = icon.sizes.split('x').map(Number);
      const header = readFileSync(path);
      // PNG IHDR: 8-byte signature, 4-byte length, 4-byte type, then width and
      // height as big-endian uint32.
      expect(header.readUInt32BE(16)).toBe(declaredWidth);
      expect(header.readUInt32BE(20)).toBe(declaredHeight);
    }
  });

  it('keeps every maskable icon inside the adaptive-icon safe zone', async () => {
    // Why: declaring `maskable` tells Android it may crop the icon to an
    // arbitrary shape, so anything outside the central 80% circle is discarded.
    // How a regression manifests: an icon whose artwork bleeds to the canvas
    // edge (the horse's ears and chin do) is tagged maskable, and its measured
    // content radius exceeds the safe radius below - on a device that shows as
    // a cropped, decapitated logo, which no unit test would otherwise catch.
    const maskable = manifest.icons.filter((icon) => purposes(icon).includes('maskable'));
    for (const icon of maskable) {
      const { size, maxRadius } = await measureContent(iconFilePath(icon));
      const safeRadius = (size * MASKABLE_SAFE_ZONE_FRACTION) / 2;
      expect(
        maxRadius,
        `${icon.src} content reaches ${maxRadius.toFixed(1)}px from centre, ` +
          `outside the ${safeRadius.toFixed(1)}px maskable safe radius`
      ).toBeLessThanOrEqual(safeRadius);
    }
  });

  it('provides an unmasked icon at both install sizes', async () => {
    // Why: a manifest holding only maskable icons leaves platforms that do not
    // mask (desktop installs, older browsers) to render the padded maskable art,
    // which appears small and off-centre. How a regression manifests: an icon
    // loses its "any" purpose and its size disappears from this set.
    const anySizes = manifest.icons
      .filter((icon) => purposes(icon).includes('any'))
      .map((icon) => icon.sizes);
    expect(new Set(anySizes)).toEqual(new Set(['192x192', '512x512']));
  });
});
