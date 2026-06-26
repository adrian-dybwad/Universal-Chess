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
  /** Optional per-option icon (used by the board's option lists). */
  icon?: string;
}

/** A data-binding reference: which store/key a node reads and writes. */
export interface MenuBind {
  store: string;
  key: string;
}

/**
 * A condition gating visibility/enablement of a node. Either a leaf condition
 * over a single bound value (`store`/`key` with `in`/`equals`), or a compound
 * `allOf` satisfied only when every subcondition holds (logical AND) -- used
 * where a row depends on more than one value (e.g. Show Graph requires both
 * analysis.mode and game.show_analysis).
 */
export interface MenuCondition {
  store?: string;
  key?: string;
  in?: string[];
  equals?: string | number | boolean;
  allOf?: MenuCondition[];
}

/** Numeric range spec for `range` cyclers. */
export interface MenuRange {
  min: number;
  max: number;
  step?: number;
  wrap?: boolean;
}

export interface MenuSection {
  id: string;
  label: string;
  icon?: string;
}

/** A single catalog node. Containers use `children`; leaf fields use the rest. */
export interface MenuNode {
  id: string;
  type:
    | 'menu'
    | 'submenu'
    | 'action'
    | 'toggle'
    | 'cycle'
    | 'select'
    | 'set_value'
    | 'dynamic'
    | 'text'
    | 'range'
    | 'info';
  label?: string;
  label_in_progress?: string;
  /**
   * Optional web-only control override. When set, the web renders this control
   * instead of `type` -- used where the board `type` is an imperative `action`
   * (e.g. the chained engine -> ELO picker) but the web wants a plain control.
   * The board ignores it.
   */
  webType?: MenuNode['type'];
  /** Optional board-only label override (e-paper abbreviation/template). */
  boardLabel?: string;
  /** Static icon id, or a state map `{ stateValue: icon }` keyed by the bound value. */
  icon?: string | Record<string, string>;
  help?: string;
  /** Platforms this node applies to. Absent means both board and web. */
  platforms?: ('board' | 'web')[];
  /** Web tab (section) this field belongs to. */
  section?: string;
  /** Named option set for select/cycle fields. */
  optionSet?: string;
  /** Value store/key this node reads and writes. */
  bind?: MenuBind;
  /** Gate the row's visibility on a bound value. */
  visibleWhen?: MenuCondition;
  /** Gate the row's enabled flag on a bound value. */
  enabledWhen?: MenuCondition;
  /** Numeric range spec for `range` cyclers. */
  range?: MenuRange;
  /** Fixed value written by a `set_value` row. */
  value?: string | number | boolean;
  /** Placeholder text for a `{value}` token when the bound value is unset. */
  valueDefault?: string;
  /** Action name for `action`/`text` nodes. */
  action?: string;
  /** Dynamic-list provider name for `dynamic` nodes. */
  provider?: string;
  /** Per-row icons for the option list a `select` opens. */
  selectedIcon?: string;
  unselectedIcon?: string;
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
