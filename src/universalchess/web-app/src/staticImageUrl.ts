// Hex digits kept from the SHA-256 of a packaged image. Long enough that two
// shipped images do not share a cache key; short enough to sit in a URL.
export const STATIC_IMAGE_HASH_LENGTH = 10

/**
 * URL of a packaged image, with its content hash in the query.
 *
 * The path stays stable (`/icons/logo-full.png`). The query changes when the
 * bytes change, which is what makes a year-long immutable cache safe: the
 * browser's cache key is the full URL, so the previous file is a different
 * entry and is never served as this one.
 *
 * An unknown path is returned unchanged. Callers that build a URL for
 * something this map does not cover (the live board snapshot) must not be
 * rewritten into a cached address.
 */
export function versionedStaticUrl(
  path: string,
  versions: Readonly<Record<string, string>>,
): string {
  const version = versions[path]
  if (!version) return path
  return `${path}?v=${version}`
}

/**
 * Rewrite quoted occurrences of packaged image paths to their versioned URLs.
 *
 * Used on the built HTML, manifest, and service worker, which name those
 * files as string literals rather than through the version map. The closing
 * quote is part of the match so `/icons/icon-192.png` cannot rewrite the
 * longer `/icons/icon-192-maskable.png`.
 */
export function applyQuotedStaticVersions(
  text: string,
  versions: Readonly<Record<string, string>>,
): string {
  const paths = Object.keys(versions).sort((left, right) => right.length - left.length)
  let rewritten = text
  for (const path of paths) {
    const versioned = versionedStaticUrl(path, versions)
    rewritten = rewritten.replaceAll(`"${path}"`, `"${versioned}"`)
    rewritten = rewritten.replaceAll(`'${path}'`, `'${versioned}'`)
  }
  return rewritten
}
