/**
 * Split plain text into display segments so URLs become real anchors.
 *
 * Used for read-only UCI info fields (e.g. UCI_EngineAbout) whose default may
 * mix prose with ``https://...`` or bare ``www....`` hosts. ``www.`` links get
 * an ``https://`` href so the browser can navigate; the visible label stays as
 * written. Trailing sentence punctuation is kept outside the link.
 */

export type TextSegment =
  | { kind: 'text'; text: string; start: number }
  | { kind: 'link'; href: string; text: string; start: number };

// Match http(s) URLs and bare www. hosts. Trailing punctuation common in prose
// is excluded from the URL and left for the following text segment.
const URL_PATTERN = /\b((?:https?:\/\/|www\.)[^\s<>"']+?)(?=[.,;:!?)\]}]*?(?:\s|$))/gi;

/**
 * Parse ``text`` into alternating plain and link segments.
 *
 * Empty input yields no segments. Non-URL text is preserved verbatim (including
 * surrounding spaces) so the rendered paragraph matches the engine default.
 * Each segment carries its start offset in the source string for stable React keys.
 */
export function linkifyText(text: string): TextSegment[] {
  if (!text) return [];
  const segments: TextSegment[] = [];
  let lastIndex = 0;
  const re = new RegExp(URL_PATTERN.source, URL_PATTERN.flags);
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    const raw = match[1];
    const start = match.index;
    if (start > lastIndex) {
      segments.push({ kind: 'text', text: text.slice(lastIndex, start), start: lastIndex });
    }
    const href = raw.toLowerCase().startsWith('www.') ? `https://${raw}` : raw;
    segments.push({ kind: 'link', href, text: raw, start });
    lastIndex = start + raw.length;
  }
  if (lastIndex < text.length) {
    segments.push({ kind: 'text', text: text.slice(lastIndex), start: lastIndex });
  }
  return segments;
}
