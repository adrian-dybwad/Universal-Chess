/**
 * Web menu engine - the browser twin of the board's engine.py.
 *
 * The shared catalog (menu.json) is the single source of truth for menu
 * structure, labels, help, binding, and gating. The board turns catalog nodes
 * into e-paper rows via engine.py; this module turns the same nodes into the
 * data a React renderer needs, so a field added or re-gated in the catalog
 * appears on both platforms without hand-editing each Settings tab.
 *
 * It is pure logic over catalog nodes plus an injected getter: it never imports
 * React or a settings store. The web supplies a MenuContext (see context.ts)
 * that reads/writes values and resolves option lists; MenuContainer.tsx walks
 * the sections this module produces and delegates each leaf to CatalogField.
 *
 * Parity with the board: `group` nodes are transparent structural containers
 * (the board inlines their children as flat rows; the web wraps each in a Card),
 * and `platforms` filters which renderer shows a node. Condition evaluation
 * (equals/in/notEquals/allOf) mirrors engine._condition_met exactly.
 */

import type { FieldValue } from '../components/CatalogField';
import type { MenuCatalog, MenuCondition, MenuNode } from '../types/menuCatalog';
import { childrenOf } from '../types/menuCatalog';

/** Reads the current value a node/condition binds to. Mirrors engine.MenuContext.get. */
export type MenuValueGetter = (store: string, key: string) => FieldValue | undefined;

/**
 * A contiguous run of rendered rows. `group` is the group container node when the
 * run came from one (rendered as a titled Card) or null for top-level leaves that
 * sit directly under the container (rendered without a card). `rows` are the leaf
 * nodes to render, already filtered for platform and visibility.
 */
export interface MenuSectionGroup {
  group: MenuNode | null;
  rows: MenuNode[];
}

/** Leaf node types the web renders as a control (via CatalogField). */
const RENDERABLE_TYPES: ReadonlySet<string> = new Set([
  'toggle',
  'select',
  'cycle',
  'range',
  'text',
]);

/** Web is one of the platforms a node applies to (absent `platforms` means both). */
export function appliesToWeb(node: MenuNode): boolean {
  return (node.platforms ?? ['board', 'web']).includes('web');
}

/**
 * Evaluate a `visibleWhen`/`enabledWhen` condition against current state.
 *
 * Mirrors engine._condition_met so the two platforms cannot drift: a compound
 * `allOf` requires every subcondition (logical AND); a leaf matches `in`
 * (membership), `equals`, or `notEquals`. An unrecognized shape returns true
 * (fails open), matching the board.
 */
export function conditionMet(cond: MenuCondition | undefined, get: MenuValueGetter): boolean {
  if (!cond) return true;
  if (cond.allOf) return cond.allOf.every((sub) => conditionMet(sub, get));
  const current = get(cond.store ?? '', cond.key ?? '');
  if (cond.in) return cond.in.includes(String(current));
  if (cond.equals !== undefined) return current === cond.equals;
  if (cond.notEquals !== undefined) return current !== cond.notEquals;
  return true;
}

/** Whether a node's `visibleWhen` gate is satisfied (absent gate = visible). */
export function isVisible(node: MenuNode, get: MenuValueGetter): boolean {
  return conditionMet(node.visibleWhen, get);
}

/**
 * Whether a node is enabled (selectable/editable). `enabledWhen` (when present)
 * gates it from another bound value - e.g. the coach Agent row is enabled only
 * while a coach is selected. Without it the row is enabled.
 */
export function isEnabled(node: MenuNode, get: MenuValueGetter): boolean {
  if (node.enabledWhen) return conditionMet(node.enabledWhen, get);
  return true;
}

/**
 * Whether a leaf node renders as a web control.
 *
 * A `dynamic` node is a provider-backed list: on the web it renders as a value
 * control (a radio/select over its provider rows) ONLY when it carries an
 * `itemBind` -- i.e. selecting a row writes a value (e.g. the piece-sprite
 * picker). A `dynamic` node backed by an `itemAction` (e.g. a scanned Wi-Fi
 * list, where a row triggers a connect) is not a value control and is not
 * rendered here.
 */
export function isRenderable(node: MenuNode): boolean {
  const effective = node.webType ?? node.type;
  if (effective === 'dynamic') return Boolean(node.itemBind);
  return RENDERABLE_TYPES.has(effective);
}

/**
 * Build the ordered, visible sections for a container, grouping by `group` nodes.
 *
 * Walks the container's children in declared order. A visible `group` becomes a
 * section carrying its visible renderable rows (a Card on the web). Consecutive
 * top-level leaves (not inside a group) are collected into anonymous sections in
 * place, so a container may freely mix grouped and ungrouped rows and the order
 * is preserved. Nodes not applicable to the web, hidden by `visibleWhen`, or not
 * renderable are dropped. Empty groups (all rows hidden) are omitted so no blank
 * Card renders.
 */
export function buildSections(
  catalog: MenuCatalog,
  containerId: string,
  get: MenuValueGetter,
): MenuSectionGroup[] {
  const sections: MenuSectionGroup[] = [];
  let pending: MenuNode[] = [];

  const flushPending = () => {
    if (pending.length > 0) {
      sections.push({ group: null, rows: pending });
      pending = [];
    }
  };

  for (const child of childrenOf(catalog, containerId)) {
    if (!appliesToWeb(child)) continue;
    if (child.type === 'group') {
      if (!isVisible(child, get)) continue;
      const rows = childrenOf(catalog, child.id).filter(
        (n) => appliesToWeb(n) && isVisible(n, get) && isRenderable(n),
      );
      if (rows.length === 0) continue;
      flushPending();
      sections.push({ group: child, rows });
      continue;
    }
    if (!isVisible(child, get) || !isRenderable(child)) continue;
    pending.push(child);
  }
  flushPending();
  return sections;
}
