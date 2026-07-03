import type { ReactNode } from 'react';
import { isFigurineGlyph } from './notation';
import './figurineText.css';

/**
 * Render a string, wrapping figurine piece glyphs in a styled span so the pieces
 * show larger and bolder than the surrounding coordinates or prose.
 *
 * Shared by every place a move can appear (the move list and the coach panel) so
 * figurine styling stays identical across the app -- the single source of truth
 * for how a piece glyph is rendered. Non-figurine text contains no glyphs and so
 * renders unchanged.
 */
export function renderFigurineText(text: string): ReactNode {
  const parts: ReactNode[] = [];
  let run = '';
  let glyphKey = 0;
  const flushRun = () => {
    if (run) {
      parts.push(run);
      run = '';
    }
  };
  for (const ch of text) {
    if (isFigurineGlyph(ch)) {
      flushRun();
      parts.push(
        <span key={`glyph-${glyphKey++}`} className="figurine-piece">
          {ch}
        </span>
      );
    } else {
      run += ch;
    }
  }
  flushRun();
  return parts;
}
