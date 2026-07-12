import { describe, it, expect } from 'vitest';
import type { MenuCatalog, MenuNode } from '../types/menuCatalog';
import {
  appliesToWeb,
  conditionMet,
  isVisible,
  isEnabled,
  isRenderable,
  buildSections,
} from './engine';

/**
 * Unit tests for the web menu engine (engine.ts).
 *
 * Why these exist: the web now renders whole Settings sections generically from
 * the shared catalog, so the engine's condition evaluation, platform filtering,
 * and group-to-section building must match the board's engine.py exactly - a
 * drift here silently shows/hides the wrong rows or the wrong platform's rows.
 * A getter over a plain state object stands in for the injected MenuContext.
 */

function makeCatalog(nodes: MenuNode[]): MenuCatalog {
  return {
    version: 1,
    roots: [],
    sections: [],
    optionSets: {},
    nodes,
  };
}

const get =
  (state: Record<string, Record<string, unknown>>) =>
  (store: string, key: string) =>
    state[store]?.[key] as never;

describe('appliesToWeb', () => {
  // Guards the platform filter that lets a shared container hold board-only or
  // web-only rows. A regression would leak the other platform's rows onto the web.
  it('includes nodes with web or absent platforms and excludes board-only', () => {
    expect(appliesToWeb({ id: 'a', type: 'toggle' })).toBe(true);
    expect(appliesToWeb({ id: 'b', type: 'toggle', platforms: ['web'] })).toBe(true);
    expect(appliesToWeb({ id: 'c', type: 'toggle', platforms: ['board'] })).toBe(false);
    expect(appliesToWeb({ id: 'd', type: 'toggle', platforms: ['board', 'web'] })).toBe(true);
  });
});

describe('conditionMet', () => {
  // Pins parity with engine._condition_met: equals/in/notEquals/allOf and the
  // fail-open default. A regression flips a gate and mis-renders dependent rows.
  const g = get({ game: { preset: 'custom', asym: true, coach: 'off' } });

  it('matches equals, in, notEquals and requires all of allOf', () => {
    expect(conditionMet({ store: 'game', key: 'preset', equals: 'custom' }, g)).toBe(true);
    expect(conditionMet({ store: 'game', key: 'preset', equals: 'blitz' }, g)).toBe(false);
    expect(conditionMet({ store: 'game', key: 'preset', in: ['custom', 'x'] }, g)).toBe(true);
    expect(conditionMet({ store: 'game', key: 'coach', notEquals: 'off' }, g)).toBe(false);
    expect(conditionMet({ store: 'game', key: 'coach', notEquals: 'auto' }, g)).toBe(true);
    expect(
      conditionMet(
        {
          allOf: [
            { store: 'game', key: 'preset', equals: 'custom' },
            { store: 'game', key: 'asym', equals: true },
          ],
        },
        g,
      ),
    ).toBe(true);
    expect(
      conditionMet(
        {
          allOf: [
            { store: 'game', key: 'preset', equals: 'custom' },
            { store: 'game', key: 'asym', equals: false },
          ],
        },
        g,
      ),
    ).toBe(false);
  });

  it('is satisfied when no condition is given (fail-open)', () => {
    expect(conditionMet(undefined, g)).toBe(true);
  });
});

describe('isVisible / isEnabled', () => {
  // Visibility uses visibleWhen; enablement uses enabledWhen and defaults to
  // enabled. A regression would render a hidden row or disable an ungated one.
  const g = get({ game: { mode: true, coach: 'off' } });

  it('gates visibility on visibleWhen and enablement on enabledWhen', () => {
    const engineRow: MenuNode = {
      id: 'analysis.engine',
      type: 'select',
      visibleWhen: { store: 'game', key: 'mode', equals: true },
    };
    expect(isVisible(engineRow, g)).toBe(true);
    expect(isVisible({ ...engineRow, visibleWhen: { store: 'game', key: 'mode', equals: false } }, g)).toBe(false);

    const agentRow: MenuNode = {
      id: 'coach.provider',
      type: 'select',
      enabledWhen: { store: 'game', key: 'coach', notEquals: 'off' },
    };
    expect(isEnabled(agentRow, g)).toBe(false); // coach is off -> disabled
    expect(isEnabled({ id: 'x', type: 'toggle' }, g)).toBe(true); // no gate -> enabled
  });
});

describe('isRenderable', () => {
  // Guards which node types become web controls. The subtle case is `dynamic`:
  // it is a value control (renderable) only with an itemBind (a radio that
  // writes a value, e.g. the sprite picker); an itemAction-only dynamic (a
  // scanned-network list) is an action list, not a field, and must not render as
  // a control. A regression here either drops the sprite picker or renders a
  // bogus control for an action list.
  it('renders standard field types and a dynamic node only when it has itemBind', () => {
    expect(isRenderable({ id: 't', type: 'toggle' })).toBe(true);
    expect(isRenderable({ id: 's', type: 'select' })).toBe(true);
    expect(isRenderable({ id: 'r', type: 'range' })).toBe(true);
    expect(isRenderable({ id: 'a', type: 'action' })).toBe(false);
    expect(
      isRenderable({ id: 'd', type: 'dynamic', itemBind: { store: 'game', key: 'chess_sprites' } }),
    ).toBe(true);
    expect(isRenderable({ id: 'd2', type: 'dynamic', itemAction: 'wifi_connect' })).toBe(false);
  });
});

describe('buildSections', () => {
  // The core group->card mapping. Pins: groups become sections carrying only
  // their visible/renderable web rows, in declared order; hidden rows and empty
  // groups drop; board-only nodes are excluded; ungrouped leaves form their own
  // section. A regression collapses a section, reorders rows, or renders blanks.
  const nodes: MenuNode[] = [
    {
      id: 'root',
      type: 'submenu',
      children: ['g.clock', 'lonely', 'g.analysis', 'g.boardonly'],
    },
    { id: 'g.clock', type: 'group', label: 'Clock', children: ['preset', 'base', 'boardField'] },
    { id: 'preset', type: 'select', label: 'Preset', bind: { store: 'game', key: 'preset' } },
    {
      id: 'base',
      type: 'select',
      label: 'Base',
      bind: { store: 'game', key: 'base' },
      visibleWhen: { store: 'game', key: 'preset', equals: '' },
    },
    { id: 'boardField', type: 'select', label: 'BoardOnly', platforms: ['board'] },
    { id: 'lonely', type: 'toggle', label: 'Lonely', bind: { store: 'game', key: 'lonely' } },
    { id: 'g.analysis', type: 'group', label: 'Analysis', children: ['aEnabled'] },
    {
      id: 'aEnabled',
      type: 'toggle',
      label: 'Live',
      bind: { store: 'game', key: 'mode' },
      visibleWhen: { store: 'game', key: 'never', equals: 'yes' },
    },
    { id: 'g.boardonly', type: 'group', label: 'BoardGroup', platforms: ['board'], children: [] },
  ];
  const catalog = makeCatalog(nodes);

  it('builds visible grouped and ungrouped sections in order, dropping empties', () => {
    // preset='' so the base row is visible; the analysis group's only row is
    // hidden (never==yes is false) so that group is omitted; the board-only
    // group and the board-only field never appear on the web.
    const sections = buildSections(catalog, 'root', get({ game: { preset: '', mode: true } }));

    expect(sections).toHaveLength(2); // Clock group + ungrouped Lonely; Analysis dropped (empty)
    expect(sections[0].group?.id).toBe('g.clock');
    expect(sections[0].rows.map((r) => r.id)).toEqual(['preset', 'base']); // boardField excluded
    expect(sections[1].group).toBeNull(); // ungrouped run
    expect(sections[1].rows.map((r) => r.id)).toEqual(['lonely']);
  });

  it('hides a gated row within a group when its condition fails', () => {
    // With a named preset the base row's visibleWhen (preset=='') fails, so the
    // Clock group renders only the Preset row - the regression this guards is the
    // base row leaking in beside an active preset.
    const sections = buildSections(catalog, 'root', get({ game: { preset: 'blitz' } }));
    expect(sections[0].rows.map((r) => r.id)).toEqual(['preset']);
  });
});
