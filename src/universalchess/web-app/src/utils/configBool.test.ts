import { describe, it, expect } from 'vitest';
import { parseConfigBool } from './configBool';

/**
 * Pins the accepted spellings to configparser.getboolean, which is what writes
 * these values on the board.
 *
 * How a regression manifests
 * --------------------------
 * Dropping a spelling does not throw; it returns the default. A setting the
 * user turned on reads as off on one screen and on on another, and the symptom
 * surfaces far from here -- as a feature that "doesn't work" for the subset of
 * installs whose centaur.ini happens to use that spelling.
 */

const TRUTHY = ['true', 'True', 'TRUE', 'on', 'On', '1', 'yes', 'Yes'];
const FALSY = ['false', 'False', 'FALSE', 'off', 'Off', '0', 'no', 'No'];
const UNRECOGNISED = ['maybe', 'tru', '2', '-1', 'null'];

describe('parseConfigBool', () => {
  it.each(TRUTHY)('reads %s as true regardless of the default', (value) => {
    expect(parseConfigBool(value, false)).toBe(true);
    expect(parseConfigBool(value, true)).toBe(true);
  });

  it.each(FALSY)('reads %s as false regardless of the default', (value) => {
    expect(parseConfigBool(value, true)).toBe(false);
    expect(parseConfigBool(value, false)).toBe(false);
  });

  it.each([
    ['  true  ', true],
    ['\tyes\n', true],
    [' off ', false],
  ] as const)('ignores surrounding whitespace in %j', (value, expected) => {
    // configparser strips values, so a hand-edited ini with a stray space must
    // not fall through to the default and contradict the board.
    expect(parseConfigBool(value, !expected)).toBe(expected);
  });

  it.each(UNRECOGNISED)('falls back to the default for %s', (value) => {
    expect(parseConfigBool(value, true)).toBe(true);
    expect(parseConfigBool(value, false)).toBe(false);
  });

  it.each([undefined, null, ''])('falls back to the default for %j', (value) => {
    // The absent case: a key the board has never written must take the
    // product default, not false, or every default-on setting reads as off on
    // a fresh install.
    expect(parseConfigBool(value, true)).toBe(true);
    expect(parseConfigBool(value, false)).toBe(false);
  });
});
