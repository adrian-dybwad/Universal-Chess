/**
 * MenuContainer - renders a catalog container's sections as web controls.
 *
 * This is the web's thin renderer over the shared menu engine: it asks the
 * engine (engine.ts) for the visible, grouped sections of a container and draws
 * each `group` as a titled Card, delegating every leaf to CatalogField. Values,
 * option lists, and gating all come from the injected WebMenuContext, so the
 * page no longer hand-builds one <Card>/<CatalogField> per field - a node added
 * or re-gated in menu.json appears here (and on the board) automatically.
 *
 * The container is re-derived on every render from the context's getters (which
 * read live form state), so visibility/enablement track edits immediately.
 */

import { Card, CardHeader } from '../components/ui';
import type { MenuCatalog } from '../types/menuCatalog';
import { buildSections } from './engine';
import { renderCatalogRow } from './renderCatalogRow';
import type { WebMenuContext } from './context';

interface MenuContainerProps {
  catalog: MenuCatalog;
  /** Catalog id of the container to render (e.g. `settings.game`). */
  containerId: string;
  ctx: WebMenuContext;
}

export function MenuContainer({ catalog, containerId, ctx }: MenuContainerProps) {
  const sections = buildSections(catalog, containerId, ctx.get);
  return (
    <>
      {sections.map((section, index) => {
        const rows = section.rows.map((node) => renderCatalogRow(node, ctx));
        if (section.group) {
          return (
            <Card key={section.group.id} className="mb-6">
              <CardHeader title={section.group.label ?? section.group.id} />
              {rows}
            </Card>
          );
        }
        // Ungrouped leaves: render inside a plain card so spacing matches the
        // grouped sections. Keyed by position since the run has no node of its
        // own (its rows carry their own stable keys).
        return (
          <Card key={`ungrouped-${index}`} className="mb-6">
            {rows}
          </Card>
        );
      })}
    </>
  );
}
