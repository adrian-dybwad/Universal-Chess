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
  /** Optional long-form description for this option. On a dropdown, shown
   *  beneath the control once selected (e.g. a time-control preset's rules).
   *  On a ``webPresentation: "described-radio"`` field, shown under every
   *  option's label so modes can be compared without opening a menu. */
  description?: string;
  /** Optional per-option icon (used by the board's option lists). */
  icon?: string;
  /** Optional per-option image thumbnail URL (web) shown next to the label, e.g.
   *  a sprite-sheet preview. The board uses `icon`/provider row glyphs instead. */
  image?: string;
  /** Optional per-option render font size in px (board option lists); lets an
   *  option preview its own effect, e.g. the Text Size choices render at their
   *  own size. */
  font_size?: number;
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
  /** Satisfied when the bound value differs from this (e.g. Agent row shown
   *  while coach is not "off"). Mirrors the board engine's `notEquals`. */
  notEquals?: string | number | boolean;
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
    | 'group'
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
  /**
   * Web-only presentation hint for option-list controls. ``described-radio``
   * renders every option as a radio with its ``description`` always visible
   * (USB Gadget Off/Auto/Client/Shared). Absent means the usual data-driven choice
   * (image / font_size / dropdown). The board ignores it.
   */
  webPresentation?: 'described-radio';
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
  /**
   * Web-only override of the option source. When set, the web resolves this
   * node's options from `webProvider` (a runtime provider) or `webOptionSet` (a
   * static set) instead of the board's `provider`/`optionSet`. Used where the two
   * platforms need different lists for the same node -- e.g. Timezone offers a
   * curated `timezones_common` on the e-paper but the full runtime list on the
   * web. The board ignores both.
   */
  webProvider?: string;
  webOptionSet?: string;
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
  /** On a `dynamic` node, the action run when one of its provider rows is
   *  selected, called with the row's key (e.g. connect to a scanned network). */
  itemAction?: string;
  /** On a `dynamic` node, makes its provider rows a radio set: selecting a row
   *  writes the row's key to this bound value. */
  itemBind?: MenuBind;
  /** Per-row icons for the option list a `select` opens. */
  selectedIcon?: string;
  unselectedIcon?: string;
  /** A `text` field whose value is a secret (never returned in cleartext,
   *  rendered as a password input on the web). */
  secret?: boolean;
  /** Board selection key (e-paper renderer). */
  key?: string;
  /** Container child ids (menu/submenu/group). */
  children?: string[];
  /** Navigation target for submenu nodes. */
  target?: string;
}

/**
 * A single parameter an online account type collects in the Add Account form.
 * The form renders one control per field, keyed by `type`.
 */
export interface AccountTypeField {
  /** Storage/form key (e.g. 'api_token', 'range'). */
  key: string;
  label: string;
  /** Control type the form renders. */
  type: 'text' | 'password';
  help?: string;
  placeholder?: string;
  required?: boolean;
  /** Secret fields are never returned by the API in cleartext (redacted to a
   *  `*_set` boolean) and render as password inputs. */
  secret?: boolean;
}

/**
 * Declarative definition of an online account type (e.g. Lichess). Drives the
 * "Add Account" form and the per-account store. An online player type is one
 * that has a matching entry here (its `id` equals a `player_type` option value).
 */
export interface AccountType {
  id: string;
  label: string;
  icon: string;
  /** Player type this account binds to when it is not itself a player type.
   *  Absent means ``id`` is the player type. */
  playerType?: string;
  /** Stored key that uniquely identifies an account of this type. */
  identityField: string;
  /** Whether the identity is user-`entered` or `resolved` after authenticating. */
  identitySource: 'entered' | 'resolved';
  fields: AccountTypeField[];
  /** Lichess plugin host list. Absent on other providers. */
  hosts?: { id: string; label: string; baseUrl: string }[];
}

export interface MenuCatalog {
  version: number;
  /** Human-readable catalog purpose (top-level metadata). */
  description?: string;
  roots: string[];
  sections: MenuSection[];
  optionSets: Record<string, MenuOption[]>;
  /** Online account type definitions; absent/empty when none are declared. */
  accountTypes?: AccountType[];
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

/**
 * Return the direct child nodes of a container (menu/submenu/group), in declared
 * order. This is the web engine's equivalent of the board's
 * MenuCatalog.children(): it walks the `children` id list rather than the
 * `field.`-prefix `section` convention, so any container - including the `game`
 * subtree whose ids are not `field.*` - can be rendered generically. Unknown
 * child ids are skipped (the backend validates references, but a stale fixture
 * should degrade rather than throw).
 */
export function childrenOf(catalog: MenuCatalog, containerId: string): MenuNode[] {
  const container = fieldById(catalog, containerId);
  if (!container?.children) return [];
  const byId = new Map(catalog.nodes.map((n) => [n.id, n]));
  return container.children
    .map((id) => byId.get(id))
    .filter((n): n is MenuNode => n !== undefined);
}
