/**
 * Types for the shared menu catalog served by GET /api/menu-schema.
 *
 * The catalog is the single source of truth shared with the e-paper board. The
 * web Settings UI renders its tabs, rows, labels, help tips, and select options
 * from it. Mirrors src/universalchess/menus/catalog/menu.json.
 */

export interface MenuOption {
  value: string;
  label: string;
}

export interface MenuSection {
  id: string;
  label: string;
  icon?: string;
}

/** A single catalog node. Containers use `children`; leaf fields use the rest. */
export interface MenuNode {
  id: string;
  type: 'menu' | 'submenu' | 'action' | 'toggle' | 'select' | 'text' | 'range' | 'info';
  label?: string;
  label_in_progress?: string;
  icon?: string;
  help?: string;
  /** Platforms this node applies to. Absent means both board and web. */
  platforms?: ('board' | 'web')[];
  /** Web tab (section) this field belongs to. */
  section?: string;
  /** Named option set for select fields. */
  optionSet?: string;
  /** Board selection key (e-paper renderer). */
  key?: string;
  /** Container child ids. */
  children?: string[];
  /** Navigation target for submenu nodes. */
  target?: string;
}

export interface MenuCatalog {
  version: number;
  roots: string[];
  sections: MenuSection[];
  optionSets: Record<string, MenuOption[]>;
  nodes: MenuNode[];
}

/** Return the field nodes for a section, in catalog declaration order. */
export function fieldsForSection(catalog: MenuCatalog, sectionId: string): MenuNode[] {
  return catalog.nodes.filter(
    (n) => n.section === sectionId && n.id.startsWith('field.')
  );
}

/** Look up a field node by id, or undefined if absent. */
export function fieldById(catalog: MenuCatalog, id: string): MenuNode | undefined {
  return catalog.nodes.find((n) => n.id === id);
}
