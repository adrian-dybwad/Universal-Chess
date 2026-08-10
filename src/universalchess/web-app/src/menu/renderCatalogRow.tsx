/**
 * One catalog leaf, drawn as its web control.
 *
 * Lives beside MenuContainer rather than inside it because it is called as a
 * function, not mounted as a component, and a module that exports both loses
 * Fast Refresh. MenuContainer draws whole containers with it; a page that
 * supplies its own card shell (e.g. the Players tab's per-slot cards) calls it
 * directly so its rows are identical to the ones MenuContainer produces.
 */

import { CatalogField } from '../components/CatalogField';
import type { MenuNode } from '../types/menuCatalog';
import { isEnabled } from './engine';
import type { WebMenuContext } from './context';

export function renderCatalogRow(
  node: MenuNode,
  ctx: WebMenuContext,
  opts?: {
    /**
     * Force the control disabled regardless of the node's `enabledWhen`. Used
     * when a page renders a row inline under a transient condition the catalog
     * does not model (e.g. the update settings disabled while a check/install is
     * in flight). ORed with the node's own gating, so it can only add disabling.
     */
    disabled?: boolean;
  },
) {
  // A `dynamic` value control (e.g. the sprite picker) binds through `itemBind`
  // -- the value written when one of its provider rows is chosen -- whereas
  // ordinary fields bind through `bind`. Fall back accordingly so both render
  // from one path.
  const bind = node.bind ?? (node.type === 'dynamic' ? node.itemBind : undefined);
  // A renderable leaf without a bind cannot read/write a value; skip it rather
  // than render a control wired to nothing (the engine only yields renderable
  // nodes, but a mis-authored node without a bind should fail visibly-absent
  // rather than crash).
  if (!bind) return null;
  return (
    <CatalogField
      key={node.id}
      node={node}
      value={ctx.get(bind.store, bind.key) ?? ''}
      options={ctx.optionsFor(node)}
      placeholder={ctx.placeholderFor(node)}
      disabled={Boolean(opts?.disabled) || !isEnabled(node, ctx.get)}
      onChange={(value) => ctx.set(bind.store, bind.key, value)}
    />
  );
}
