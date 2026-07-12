/**
 * Web MenuContext - the browser twin of board_context.py's BoardMenuContext.
 *
 * The engine (engine.ts) is pure logic over catalog nodes; everything with a
 * side effect or a runtime data source is injected through this context. The web
 * backs it with the Settings page's form state and the live `/api/*` data the
 * page already fetches, exactly as the board backs its context with settings
 * stores and e-paper providers.
 *
 * Registries (mirroring the board):
 * - stores: named value stores a node's `bind`/condition reads and writes. The
 *   web maps catalog stores to its form state (e.g. `analysis` -> the game
 *   section's analysis_* keys), centralizing what was scattered per control.
 * - providers: named runtime option lists a provider-backed `select` renders
 *   (installed engines, coaches, configured agents, time-control presets), each
 *   backed by data the page fetches.
 * - optionSets: static option lists resolved from the catalog by name.
 */

import type { FieldValue } from '../components/CatalogField';
import type { MenuNode, MenuOption } from '../types/menuCatalog';
import type { MenuValueGetter } from './engine';

interface ValueStore {
  get: (key: string) => FieldValue | undefined;
  set: (key: string, value: FieldValue) => void;
}

export class WebMenuContext {
  private readonly stores = new Map<string, ValueStore>();
  private readonly providers = new Map<string, () => MenuOption[]>();
  private readonly placeholders = new Map<string, string>();
  private readonly resolveOptionSet: (name: string) => MenuOption[];

  /**
   * @param resolveOptionSet Resolves a static option set by name from the loaded
   *   catalog (the web's `optionSet(name)` helper). Used for nodes that carry an
   *   `optionSet` rather than a runtime `provider`.
   */
  constructor(resolveOptionSet: (name: string) => MenuOption[]) {
    this.resolveOptionSet = resolveOptionSet;
  }

  /** Register a named value store (its getter/setter back a catalog store). */
  registerStore(
    name: string,
    getter: (key: string) => FieldValue | undefined,
    setter: (key: string, value: FieldValue) => void,
  ): void {
    this.stores.set(name, { get: getter, set: setter });
  }

  /** Register a runtime option-list provider (e.g. `installed_engines`). */
  registerProvider(name: string, fn: () => MenuOption[]): void {
    this.providers.set(name, fn);
  }

  /**
   * Register a per-slot placeholder for a text node's empty state, keyed by node
   * id. Used where the hint is context-specific and cannot be a single shared
   * catalog value -- e.g. the player Name field defaults to "Player 1"/"Player 2"
   * per slot, which the board supplies via its {fn:player_name} compute.
   */
  registerPlaceholder(nodeId: string, text: string): void {
    this.placeholders.set(nodeId, text);
  }

  /**
   * The placeholder to hint for an empty text field: a per-slot override if one
   * was registered, otherwise the node's own `valueDefault` (undefined when the
   * node declares no default, e.g. field.player.name after the shared "Human"
   * default was removed in favor of per-slot context defaults).
   */
  placeholderFor(node: MenuNode): string | undefined {
    return this.placeholders.get(node.id) ?? node.valueDefault;
  }

  /** Read a bound value. Unknown stores return undefined (a gate then fails). */
  get: MenuValueGetter = (store, key) => this.stores.get(store)?.get(key);

  /** Write a bound value. A write to an unknown store is a no-op (guarded by tests). */
  set(store: string, key: string, value: FieldValue): void {
    this.stores.get(store)?.set(key, value);
  }

  /**
   * Resolve the option list a select/cycle node renders. A web-only override
   * (`webProvider`/`webOptionSet`) wins so a shared node can offer a different
   * list on the web than the board (e.g. Timezone's full runtime list vs the
   * board's curated set); otherwise the node's own runtime `provider` is used,
   * else its static `optionSet`, else an empty list. Mirrors the board's split
   * between provider-backed and option-set-backed selects, with the web override
   * layered on top.
   */
  optionsFor(node: MenuNode): MenuOption[] {
    if (node.webProvider) return this.providers.get(node.webProvider)?.() ?? [];
    if (node.webOptionSet) return this.resolveOptionSet(node.webOptionSet);
    if (node.provider) return this.providers.get(node.provider)?.() ?? [];
    if (node.optionSet) return this.resolveOptionSet(node.optionSet);
    return [];
  }
}
