"""Board adapter for the data-driven menu engine.

Binds the platform-agnostic :mod:`universalchess.menus.engine` to the e-paper
board: a :class:`BoardMenuContext` supplies the engine's side-effect boundary
(value stores, option sets, dynamic providers, actions) and
:func:`run_engine_menu` drives the show/dispatch loop on top of the existing
``MenuManager.run_menu_loop`` (so break results, REFRESH, HELP, and cursor
tracking keep working unchanged).

Stores are registered by the application so this module stays decoupled from the
concrete settings/sound/update implementations: each store is a ``(getter,
setter)`` pair keyed by the catalog ``bind.key``. Where a platform's storage
uses different key names than the catalog (e.g. the board's sound keys), the
registered getter/setter owns that translation rather than leaking it into the
shared catalog.
"""

from typing import Any, Callable, Dict, List, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import MenuSelection
from universalchess.menus.catalog.entry_builder import node_to_entry
from universalchess.menus.catalog.loader import get_catalog
from universalchess.menus.engine import MenuRow, build_rows, dispatch


class _Store:
    """A registered value store: a getter and setter keyed by catalog bind key."""

    def __init__(self, getter: Callable[[str], Any], setter: Callable[[str, Any], None]):
        self.get = getter
        self.set = setter


class BoardMenuContext:
    """Board-side :class:`~universalchess.menus.engine.MenuContext`.

    Application code registers the stores/providers/actions the migrated menus
    need; the engine then reads and mutates board state only through these
    registrations. Option sets default to the shared catalog so callers rarely
    override them.
    """

    def __init__(self, option_set_fn: Optional[Callable[[str], List[dict]]] = None):
        self._stores: Dict[str, _Store] = {}
        self._providers: Dict[str, Callable[[], List[MenuRow]]] = {}
        self._actions: Dict[str, Callable[[], Optional[str]]] = {}
        self._values: Dict[str, Callable[[dict], str]] = {}
        self._option_set_fn = option_set_fn or (lambda name: get_catalog().option_set(name))

    def register_store(self, name: str, getter: Callable[[str], Any], setter: Callable[[str, Any], None]) -> None:
        """Register a value store backing ``bind.store == name``."""
        self._stores[name] = _Store(getter, setter)

    def register_provider(self, name: str, fn: Callable[[], List[MenuRow]]) -> None:
        """Register a dynamic-list provider producing rows for ``provider == name``."""
        self._providers[name] = fn

    def register_action(self, name: str, fn: Callable[[], Optional[str]]) -> None:
        """Register an action handler invoked for ``action == name``.

        The handler may return a result string (e.g. a break/exit token) which
        the run loop propagates.
        """
        self._actions[name] = fn

    def register_value(self, name: str, fn: Callable[[dict], str]) -> None:
        """Register a computed-label helper invoked for a ``{fn:NAME}`` token.

        The helper receives the node being rendered and returns the substituted
        text (e.g. a composed player summary). Injected per platform so the
        catalog stays free of computed-label logic.
        """
        self._values[name] = fn

    # -- MenuContext protocol --------------------------------------------

    def get(self, store: str, key: str) -> Any:
        return self._stores[store].get(key)

    def set(self, store: str, key: str, value: Any) -> None:
        self._stores[store].set(key, value)

    def options(self, name: str) -> List[dict]:
        return self._option_set_fn(name)

    def provide(self, provider: str) -> List[MenuRow]:
        return self._providers[provider]()

    def run_action(self, name: str) -> Optional[str]:
        return self._actions[name]()

    def compute(self, name: str, node: dict) -> str:
        return self._values[name](node)


def _row_to_entry(row: MenuRow) -> IconMenuEntry:
    """Convert an engine row to an e-paper entry.

    Rows backed by a catalog node reuse :func:`node_to_entry` so the node's
    ``epaper`` style block is honored; provider rows (no node) map directly.
    """
    if row.node and row.icon_image is None and row.trailing_icon is None:
        return node_to_entry(row.node, label=row.label, icon=row.icon, enabled=row.enabled)
    return IconMenuEntry(
        key=row.key,
        label=row.label,
        icon_name=row.icon,
        icon_image=row.icon_image,
        icon_mask=row.icon_mask,
        trailing_icon_name=row.trailing_icon,
        enabled=row.enabled,
        selectable=row.selectable,
        help=row.help,
    )


def _run_select(outcome, ctx, menu_manager) -> Optional[MenuSelection]:
    """Show an option list for a ``select`` outcome; persist the chosen value.

    The active value is marked, and picking a row writes that value to the
    bound store and exits the list (returning to the parent, which redraws with
    the new value). Break results propagate.

    Marking style depends on the option set: when options carry their own
    ``icon`` (e.g. color -> white/black piece, player type -> per-type glyph)
    the option icon is kept and the active row is marked with a leading ``"* "``;
    otherwise a radio glyph (``selected``/``unselected`` icon) marks the choice.
    """
    selected_icon = outcome.selected_icon or "radio_checked"
    unselected_icon = outcome.unselected_icon or "radio_empty"

    def build_entries() -> List[IconMenuEntry]:
        current = str(ctx.get(outcome.store, outcome.key))
        entries: List[IconMenuEntry] = []
        for option in ctx.options(outcome.option_set):
            value = option["value"]
            selected = str(value) == current
            option_icon = option.get("icon")
            if option_icon:
                icon_name = option_icon
                label = f"* {option['label']}" if selected else option["label"]
            else:
                icon_name = selected_icon if selected else unselected_icon
                label = option["label"]
            entries.append(
                IconMenuEntry(
                    key=str(value),
                    label=label,
                    icon_name=icon_name,
                    enabled=True,
                )
            )
        return entries

    def handle_selection(selection: MenuSelection) -> Optional[MenuSelection]:
        ctx.set(outcome.store, outcome.key, selection.key)
        return MenuSelection.from_key("BACK")  # one pick, then return to parent

    return menu_manager.run_menu_loop(build_entries, handle_selection, initial_index=0)


def render_container(
    container_id: str,
    ctx: BoardMenuContext,
    *,
    platform: str = "board",
    catalog=None,
) -> List[IconMenuEntry]:
    """Build a container's child rows as e-paper entries, without a dispatch loop.

    For callers that render data-driven entries through the engine but keep their
    own bespoke dispatch (e.g. the board Settings list, whose surrounding loop
    owns app-state/navigation-stack semantics that do not belong in the catalog).
    The entry keys are the catalog node keys, so the caller's key-based dispatch
    keeps working unchanged. Use :func:`run_engine_menu` instead when the engine
    can own the whole show/dispatch loop.
    """
    catalog = catalog or get_catalog()
    rows = build_rows(container_id, ctx, platform=platform, catalog=catalog)
    return [_row_to_entry(row) for row in rows]


def run_engine_menu(
    container_id: str,
    ctx: BoardMenuContext,
    menu_manager,
    *,
    platform: str = "board",
    initial_index: int = 0,
    catalog=None,
) -> Optional[MenuSelection]:
    """Run a catalog container as a board menu via the shared engine.

    Builds rows from the container's children each iteration (so toggles/values
    redraw with fresh state), shows them, and dispatches the selected node:
    ``stay`` redraws in place, ``submenu`` recurses into the target container,
    ``select`` opens an option list, ``action`` runs the bound action. Break
    results from any depth propagate to the caller, matching the board's
    existing nested-menu unwinding.

    Args:
        catalog: Catalog to read children from; defaults to the shared cached
            catalog. Injectable so the loop can be tested with synthetic nodes.

    Returns:
        The break/exit :class:`MenuSelection` that ended the loop, or None.
    """
    catalog = catalog or get_catalog()
    state: Dict[str, List[MenuRow]] = {"rows": []}

    def build_entries() -> List[IconMenuEntry]:
        rows = build_rows(container_id, ctx, platform=platform, catalog=catalog)
        state["rows"] = rows
        return [_row_to_entry(row) for row in rows]

    def handle_selection(selection: MenuSelection) -> Optional[MenuSelection]:
        node = next((row.node for row in state["rows"] if row.key == selection.key and row.node), None)
        if node is None:
            return None  # provider row without behavior, or unknown key -> redraw
        outcome = dispatch(node, ctx)

        if outcome.kind == "stay":
            return None
        if outcome.kind == "submenu":
            sub = run_engine_menu(outcome.target, ctx, menu_manager, platform=platform, catalog=catalog)
            return sub if (sub is not None and sub.is_break) else None
        if outcome.kind == "select":
            sub = _run_select(outcome, ctx, menu_manager)
            return sub if (sub is not None and sub.is_break) else None
        if outcome.kind == "action":
            # An action that returns a signal exits this loop and propagates it
            # (a break token unwinds all menus; a navigation token like
            # START_GAME is handled by the caller). Returning None keeps the
            # menu open and redraws with any state the action changed.
            if outcome.signal is not None:
                return MenuSelection.from_key(outcome.signal)
            return None
        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection, initial_index=initial_index)


def run_node(
    node: dict,
    ctx: BoardMenuContext,
    menu_manager,
    *,
    platform: str = "board",
    catalog=None,
) -> Optional[MenuSelection]:
    """Dispatch a single catalog node and run its resulting interaction.

    Entry point for invoking one node's behavior outside a container loop -- used
    while a parent menu (e.g. the board Settings list) still dispatches by key but
    its child (e.g. Time Control) is a data-driven ``select``/``submenu``/``action``.
    Returns the break/exit :class:`MenuSelection` to propagate, or None.
    """
    outcome = dispatch(node, ctx)
    if outcome.kind == "select":
        sub = _run_select(outcome, ctx, menu_manager)
        return sub if (sub is not None and sub.is_break) else None
    if outcome.kind == "submenu":
        return run_engine_menu(outcome.target, ctx, menu_manager, platform=platform, catalog=catalog)
    if outcome.kind == "action":
        if outcome.signal is not None:
            return MenuSelection.from_key(outcome.signal)
        return None
    return None
