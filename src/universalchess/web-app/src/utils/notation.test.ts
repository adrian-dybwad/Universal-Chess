import { describe, it, expect } from 'vitest';
import { formatMove, asNotation, DEFAULT_NOTATION, type VerboseMove } from './notation';

/**
 * These tests guard the move-history notation formatting shared by the Live and
 * Analyze pages. Each case pins a distinct edge (quiet move, capture, castling,
 * promotion, check/mate, disambiguation) across all four notations, because a
 * regression in one branch (e.g. LAN dropping the capture 'x', or figurine
 * mangling the promotion piece) would otherwise surface only as a wrong glyph in
 * the UI with no other signal.
 */

// Minimal verbose-move builder; only the fields formatMove reads are supplied.
function move(partial: Partial<VerboseMove> & Pick<VerboseMove, 'san' | 'from' | 'to' | 'piece'>): VerboseMove {
  return { flags: '', ...partial };
}

describe('formatMove', () => {
  it('formats a quiet knight move in every notation', () => {
    // Nf3: the canonical piece move. Figurine swaps N->glyph; LAN inserts the
    // origin square with a '-'; UCI is pure coordinates.
    const m = move({ san: 'Nf3', from: 'g1', to: 'f3', piece: 'n', flags: 'n' });
    expect(formatMove(m, 'san')).toBe('Nf3');
    expect(formatMove(m, 'figurine')).toBe('\u2658f3');
    expect(formatMove(m, 'lan')).toBe('Ng1-f3');
    expect(formatMove(m, 'uci')).toBe('g1f3');
  });

  it('formats a pawn double-step (no piece letter)', () => {
    // e4: pawns carry no letter, so figurine equals SAN and LAN has an empty
    // piece prefix. Guards against a stray glyph or letter being prepended.
    const m = move({ san: 'e4', from: 'e2', to: 'e4', piece: 'p', flags: 'b' });
    expect(formatMove(m, 'san')).toBe('e4');
    expect(formatMove(m, 'figurine')).toBe('e4');
    expect(formatMove(m, 'lan')).toBe('e2-e4');
    expect(formatMove(m, 'uci')).toBe('e2e4');
  });

  it('formats a piece capture with the x separator in LAN', () => {
    // Bxc4: capture flag must turn the LAN separator into 'x'; a regression that
    // ignores flags would render 'Bf1-c4' and lose the capture.
    const m = move({ san: 'Bxc4', from: 'f1', to: 'c4', piece: 'b', flags: 'c' });
    expect(formatMove(m, 'san')).toBe('Bxc4');
    expect(formatMove(m, 'figurine')).toBe('\u2657xc4');
    expect(formatMove(m, 'lan')).toBe('Bf1xc4');
    expect(formatMove(m, 'uci')).toBe('f1c4');
  });

  it('formats a pawn capture and en-passant', () => {
    // exd5 (en passant flag 'e'): counts as a capture for LAN. Verifies 'e' is
    // treated like 'c' rather than being missed.
    const m = move({ san: 'exd5', from: 'e5', to: 'd6', piece: 'p', flags: 'e' });
    expect(formatMove(m, 'lan')).toBe('e5xd6');
    expect(formatMove(m, 'uci')).toBe('e5d6');
  });

  it('leaves castling as O-O across algebraic notations', () => {
    // O-O: LAN keeps the SAN castling form (no from-to expansion), and figurine
    // must not alter 'O'. A regression could expand it to 'Ke1-g1' or swap the O.
    const m = move({ san: 'O-O', from: 'e1', to: 'g1', piece: 'k', flags: 'k' });
    expect(formatMove(m, 'san')).toBe('O-O');
    expect(formatMove(m, 'figurine')).toBe('O-O');
    expect(formatMove(m, 'lan')).toBe('O-O');
    expect(formatMove(m, 'uci')).toBe('e1g1');
  });

  it('formats a promotion, applying figurine to the promoted piece', () => {
    // e8=Q+: the promotion piece must also become a glyph in figurine, and LAN
    // must carry the '=Q' plus the check suffix. UCI lowercases the promotion.
    const m = move({ san: 'e8=Q+', from: 'e7', to: 'e8', piece: 'p', promotion: 'q', flags: 'np' });
    expect(formatMove(m, 'san')).toBe('e8=Q+');
    expect(formatMove(m, 'figurine')).toBe('e8=\u2655+');
    expect(formatMove(m, 'lan')).toBe('e7-e8=Q+');
    expect(formatMove(m, 'uci')).toBe('e7e8q');
  });

  it('formats a capture-promotion to checkmate', () => {
    // dxe8=N#: combined capture + promotion + mate. Ensures LAN uses 'x', keeps
    // '=N' and the '#', and figurine converts the promoted knight.
    const m = move({ san: 'dxe8=N#', from: 'd7', to: 'e8', piece: 'p', promotion: 'n', flags: 'cp' });
    expect(formatMove(m, 'figurine')).toBe('dxe8=\u2658#');
    expect(formatMove(m, 'lan')).toBe('d7xe8=N#');
    expect(formatMove(m, 'uci')).toBe('d7e8n');
  });

  it('preserves SAN disambiguation in figurine', () => {
    // Nbd7: file disambiguation. Figurine must keep the 'b' file letter and only
    // swap the leading 'N'; LAN carries the true origin square instead.
    const m = move({ san: 'Nbd7', from: 'b8', to: 'd7', piece: 'n', flags: 'n' });
    expect(formatMove(m, 'figurine')).toBe('\u2658bd7');
    expect(formatMove(m, 'lan')).toBe('Nb8-d7');
  });
});

describe('asNotation', () => {
  it('passes through known notations and falls back to figurine otherwise', () => {
    // Guards the settings-string coercion: an unknown/empty value must default to
    // figurine (the product default) rather than crash or render blank.
    expect(asNotation('san')).toBe('san');
    expect(asNotation('lan')).toBe('lan');
    expect(asNotation('uci')).toBe('uci');
    expect(asNotation('figurine')).toBe('figurine');
    expect(asNotation('bogus')).toBe(DEFAULT_NOTATION);
    expect(asNotation(undefined)).toBe('figurine');
  });
});
