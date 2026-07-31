/**
 * Guards externalLinkHref, the scheme gate for outbound links whose URL comes
 * from device data rather than the app bundle (an AI agent's documentation link,
 * which a user-dropped agent module supplies).
 *
 * Why: such a URL is rendered as an anchor href, so a `javascript:` (or `data:`)
 * value would execute in the page's origin, with access to everything the
 * settings page can reach. How a regression shows: the function returns those
 * URLs instead of null and the link becomes script execution on click.
 */
import { describe, expect, it } from 'vitest'
import { externalLinkHref } from './externalLink'

describe('externalLinkHref', () => {
  it.each([
    ['https URL', 'https://docs.example.test/models', 'https://docs.example.test/models'],
    // http is kept: a LAN/self-hosted agent may document itself on a plain-http host,
    // and the board's own UI is served over http.
    ['http URL', 'http://192.168.1.5/docs', 'http://192.168.1.5/docs'],
    // The URL is returned as written (only trimmed), not re-serialized, so a link
    // the user configured is not silently rewritten.
    ['surrounding whitespace trimmed', '  https://docs.example.test  ', 'https://docs.example.test'],
  ])('accepts %s', (_case, input, expected) => {
    expect(externalLinkHref(input)).toBe(expected)
  })

  it.each([
    ['empty string', ''],
    ['whitespace only', '   '],
    ['undefined', undefined],
    ['javascript scheme', 'javascript:alert(document.cookie)'],
    // Mixed case must not slip past a naive lowercase-prefix check.
    ['mixed-case javascript scheme', 'JaVaScRiPt:alert(1)'],
    ['data scheme', 'data:text/html,<script>alert(1)</script>'],
    ['scheme-relative URL', '//evil.example.test/docs'],
    ['relative path', '/docs/agents'],
    ['bare host without a scheme', 'docs.example.test'],
  ])('rejects %s', (_case, input) => {
    expect(externalLinkHref(input)).toBeNull()
  })
})
