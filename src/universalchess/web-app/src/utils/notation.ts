/**
 * Chess move notation formatting for the move-history views.
 *
 * The board and web share a single `game.notation` setting; this module is the
 * web half that turns a chess.js verbose move into a display string in the
 * selected notation. Kept as a pure function (no chess.js or React imports) so
 * it is trivially unit-testable and reusable by any move-history component.
 */

/** The notation types offered by the `game.notation` setting. */
export type Notation = 'figurine' | 'san' | 'lan' | 'uci';

export const DEFAULT_NOTATION: Notation = 'figurine';

/** Narrow an arbitrary string (e.g. from settings) to a known notation. */
export function asNotation(value: string | null | undefined): Notation {
  if (value === 'san' || value === 'lan' || value === 'uci' || value === 'figurine') {
    return value;
  }
  return DEFAULT_NOTATION;
}

/**
 * The subset of a chess.js verbose move (`history({ verbose: true })`) needed to
 * render any supported notation. Declared locally so this module stays free of a
 * chess.js type dependency; callers pass the verbose move objects directly.
 */
export interface VerboseMove {
  /** Standard algebraic notation, e.g. "Nf3", "exd5", "e8=Q+", "O-O". */
  san: string;
  /** Origin square, e.g. "g1". */
  from: string;
  /** Destination square, e.g. "f3". */
  to: string;
  /** Lowercase moving-piece letter: p, n, b, r, q, k. */
  piece: string;
  /** Lowercase promotion piece letter when the move promotes, else undefined. */
  promotion?: string;
  /** chess.js flags string; contains 'c' or 'e' for captures, 'k'/'q' for castling. */
  flags: string;
}

/** White-outline figurine glyphs, used for both colors per common FAN usage. */
const FIGURINE_GLYPHS: Record<string, string> = {
  K: '\u2654',
  Q: '\u2655',
  R: '\u2656',
  B: '\u2657',
  N: '\u2658',
};

/** Set of the figurine glyph characters, for styling them in rendered output. */
const FIGURINE_GLYPH_SET = new Set(Object.values(FIGURINE_GLYPHS));

/** True when a character is one of the figurine piece glyphs. */
export function isFigurineGlyph(ch: string): boolean {
  return FIGURINE_GLYPH_SET.has(ch);
}

/**
 * Replace the piece letters (K, Q, R, B, N) in an algebraic string with figurine
 * glyphs. Files (a-h) and ranks (1-8) are lowercase/digits and castling uses 'O',
 * so only true piece letters are affected -- including the promotion piece in
 * "e8=Q".
 */
function toFigurine(algebraic: string): string {
  let out = '';
  for (const ch of algebraic) {
    out += FIGURINE_GLYPHS[ch] ?? ch;
  }
  return out;
}

/** True when the move captures (normal capture or en passant). */
function isCapture(move: VerboseMove): boolean {
  return move.flags.includes('c') || move.flags.includes('e');
}

/** Trailing check/checkmate marker carried by the SAN, if any. */
function checkSuffix(san: string): string {
  if (san.endsWith('#')) return '#';
  if (san.endsWith('+')) return '+';
  return '';
}

/**
 * Long algebraic notation, e.g. "Ng1-f3", "e2-e4", "Bf1xc4", "e7-e8=Q+",
 * "O-O". Castling is left as its SAN form (LAN keeps O-O / O-O-O).
 */
function toLan(move: VerboseMove): string {
  const isCastle = move.flags.includes('k') || move.flags.includes('q');
  if (isCastle) return move.san;

  const pieceLetter = move.piece === 'p' ? '' : move.piece.toUpperCase();
  const separator = isCapture(move) ? 'x' : '-';
  const promotion = move.promotion ? `=${move.promotion.toUpperCase()}` : '';
  return `${pieceLetter}${move.from}${separator}${move.to}${promotion}${checkSuffix(move.san)}`;
}

/** Pure coordinate/UCI notation, e.g. "g1f3", "e7e8q". */
function toUci(move: VerboseMove): string {
  return `${move.from}${move.to}${move.promotion ?? ''}`;
}

/**
 * Format a single move in the requested notation.
 *
 * @param move A chess.js verbose move (or the {@link VerboseMove} subset).
 * @param notation The target notation.
 */
export function formatMove(move: VerboseMove, notation: Notation): string {
  switch (notation) {
    case 'san':
      return move.san;
    case 'figurine':
      return toFigurine(move.san);
    case 'lan':
      return toLan(move);
    case 'uci':
      return toUci(move);
  }
}
