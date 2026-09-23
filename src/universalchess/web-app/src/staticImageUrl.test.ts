import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { fileURLToPath } from 'node:url'

import { staticImageVersions } from 'virtual:static-image-versions'
import {
  STATIC_IMAGE_HASH_LENGTH,
  applyQuotedStaticVersions,
  versionedStaticUrl,
} from './staticImageUrl'

describe('versioned static image urls', () => {
  it('leaves a path alone when the build has no hash for it', () => {
    // Why: the live board snapshot and other unhashed images must not gain a
    // cache key. How a regression manifests: /screen.jpg comes back with ?v=
    // and the browser keeps a stale panel picture.
    expect(versionedStaticUrl('/screen.jpg', { '/icons/logo-full.png': 'abc' })).toBe('/screen.jpg')
  })

  it('appends the content hash as the cache key', () => {
    // Why: the filename does not change when the picture does, so the hash
    // has to be in the URL or an immutable response serves the old bytes.
    // How a regression manifests: the logo URL has no ?v=, or the hash is
    // not the one in the map.
    expect(versionedStaticUrl('/icons/logo-full.png', { '/icons/logo-full.png': 'abc123' }))
      .toBe('/icons/logo-full.png?v=abc123')
  })

  it('does not rewrite a longer filename that shares a prefix', () => {
    // Why: icon-192.png is a prefix of icon-192-maskable.png. A shorter
    // match would glue the hash into the middle of the maskable name and
    // the precache would 404. How a regression manifests: the maskable src
    // contains "?v=" before "-maskable".
    const versions = {
      '/icons/icon-192.png': 'aaa',
      '/icons/icon-192-maskable.png': 'bbb',
    }
    const rewritten = applyQuotedStaticVersions(
      `'${'/icons/icon-192-maskable.png'}' and "/icons/icon-192.png"`,
      versions,
    )
    expect(rewritten).toBe(
      "'/icons/icon-192-maskable.png?v=bbb' and \"/icons/icon-192.png?v=aaa\"",
    )
  })

  it('hashes the navbar logo from the file the build ships', () => {
    // Why: the version map has to be the hash of the packaged file. A
    // constant or a build timestamp would change the URL when the picture
    // did not, or miss a change when it did. How a regression manifests:
    // the map entry differs from the SHA-256 prefix of logo-full.png.
    const logoPath = fileURLToPath(new URL('../public/icons/logo-full.png', import.meta.url))
    const hash = createHash('sha256')
      .update(readFileSync(logoPath))
      .digest('hex')
      .slice(0, STATIC_IMAGE_HASH_LENGTH)
    expect(staticImageVersions['/icons/logo-full.png']).toBe(hash)
    expect(versionedStaticUrl('/icons/logo-full.png', staticImageVersions))
      .toBe(`/icons/logo-full.png?v=${hash}`)
  })

  it('does not give the live snapshot a packaged-image hash', () => {
    // Why: /screen.jpg is rewritten on the board between versions. A hash
    // in this map would mark it cacheable. How a regression manifests:
    // the key is present.
    expect(staticImageVersions['/screen.jpg']).toBeUndefined()
  })
})
