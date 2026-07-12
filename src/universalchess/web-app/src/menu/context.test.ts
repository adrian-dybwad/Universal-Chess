import { describe, it, expect } from 'vitest';
import type { MenuNode, MenuOption } from '../types/menuCatalog';
import { WebMenuContext } from './context';

/**
 * Unit tests for WebMenuContext.optionsFor.
 *
 * Why these exist: a shared catalog node can need a different option list on the
 * web than on the board (e.g. Timezone offers a curated set on the e-paper but
 * the full runtime list on the web). That is expressed with a web-only
 * `webProvider`/`webOptionSet` override, which must win over the board's own
 * `provider`/`optionSet`. A regression here makes the web fall back to the
 * board's (wrong) list, so these pin the resolution order precisely.
 */

const CURATED: MenuOption[] = [{ value: 'UTC', label: 'UTC' }];
const FULL: MenuOption[] = [
  { value: 'UTC', label: 'UTC' },
  { value: 'Asia/Tokyo', label: 'Asia/Tokyo' },
];

function makeContext(): WebMenuContext {
  // resolveOptionSet stands in for the catalog's static option sets.
  const ctx = new WebMenuContext((name) => (name === 'timezones_common' ? CURATED : []));
  ctx.registerProvider('timezones', () => FULL);
  return ctx;
}

describe('WebMenuContext.optionsFor', () => {
  it('prefers webProvider over the board provider/optionSet', () => {
    // The Timezone case: board optionSet is the curated set, webProvider is the
    // full runtime list. A regression that ignored webProvider would return the
    // curated CURATED list (missing Asia/Tokyo) on the web.
    const node: MenuNode = {
      id: 'system.timezone',
      type: 'select',
      optionSet: 'timezones_common',
      webProvider: 'timezones',
    };
    expect(makeContext().optionsFor(node)).toEqual(FULL);
  });

  it('prefers webOptionSet over the board optionSet', () => {
    // A web-only static override resolves through resolveOptionSet by its own
    // name; here it names the curated set while the board optionSet is unknown,
    // proving the web name is the one consulted.
    const ctx = new WebMenuContext((name) => (name === 'timezones_common' ? CURATED : []));
    const node: MenuNode = {
      id: 'x',
      type: 'select',
      optionSet: 'does_not_exist',
      webOptionSet: 'timezones_common',
    };
    expect(ctx.optionsFor(node)).toEqual(CURATED);
  });

  it('falls back to the board provider/optionSet when no web override is set', () => {
    // Without an override the web resolves the node's own optionSet, so a shared
    // node with no per-platform difference behaves identically on both.
    const node: MenuNode = { id: 'y', type: 'select', optionSet: 'timezones_common' };
    expect(makeContext().optionsFor(node)).toEqual(CURATED);
  });
});
