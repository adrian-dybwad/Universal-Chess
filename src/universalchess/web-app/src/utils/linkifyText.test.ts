/**
 * Guards linkifyText for read-only UCI about strings.
 *
 * Why: UCI_EngineAbout defaults often mix prose with www./https URLs; those
 * must become real anchors (new tab) without mangling surrounding text.
 * How regression shows: URL stays plain text, www. has no https href, or
 * trailing punctuation is swallowed into the link.
 */
import { describe, expect, it } from 'vitest'
import { linkifyText } from './linkifyText'

describe('linkifyText', () => {
  it('returns no segments for empty input', () => {
    expect(linkifyText('')).toEqual([])
  })

  it('keeps plain prose as a single text segment', () => {
    expect(linkifyText('No links here.')).toEqual([
      { kind: 'text', text: 'No links here.', start: 0 },
    ])
  })

  it('linkifies https URLs and bare www hosts in about-style prose', () => {
    // Why: protocol example uses www.shredderchess.com without a scheme.
    // Failure: www. segment stays kind text, or href lacks https://.
    const text =
      'Shredder by Stefan Meyer-Kahlen, see www.shredderchess.com and https://example.com/x.'
    const segments = linkifyText(text)
    const links = segments.filter((s) => s.kind === 'link')
    expect(links).toEqual([
      {
        kind: 'link',
        text: 'www.shredderchess.com',
        href: 'https://www.shredderchess.com',
        start: expect.any(Number),
      },
      {
        kind: 'link',
        text: 'https://example.com/x',
        href: 'https://example.com/x',
        start: expect.any(Number),
      },
    ])
    expect(segments.some((s) => s.kind === 'text' && s.text.includes('Shredder'))).toBe(true)
    // Trailing period after the https URL stays outside the link.
    const last = segments[segments.length - 1]
    expect(last).toEqual({ kind: 'text', text: '.', start: expect.any(Number) })
  })
})
