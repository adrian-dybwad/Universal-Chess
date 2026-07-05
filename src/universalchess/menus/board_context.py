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
from universalchess.managers.menu import MenuResult, MenuSelection
from universalchess.menus.catalog.entry_builder import node_to_entry
from universalchess.menus.catalog.loader import get_catalog
from universalchess.menus.engine import MenuRow, build_rows, dispatch, dispatch_row

# Shared navigation-state recorder for full-depth menu restore. The application
# injects its process-wide MenuContext once (composition root) via
# :func:`set_nav_context`; the engine then records/replays the container chain
# without importing the app. Left None in isolated engine unit tests, which pass
# their own ``nav_context`` to :func:`run_engine_menu` instead.
_nav_context: Optional[Any] = None


def set_nav_context(nav_context: Any) -> None:
    """Inject the process-wide navigation MenuContext used for restore.

    Called once by the application at startup. ``run_engine_menu`` records every
    restorable container it enters onto this context and, on restore, auto-
    descends the saved chain. Passing ``None`` disables recording (the engine
    then behaves as a plain show/dispatch loop).
    """
    global _nav_context
    _nav_context = nav_context


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

    def run_action(self, name: str, arg: Optional[str] = None) -> Optional[str]:
        """Invoke a registered action, optionally with an item argument.

        ``arg`` is supplied only for actionable provider rows (e.g. a scanned
        SSID), where the handler is called with that key; ordinary action nodes
        pass no argument, so their zero-arg handlers keep working unchanged.
        """
        handler = self._actions[name]
        return handler(arg) if arg is not None else handler()

    def compute(self, name: str, node: dict) -> str:
        return self._values[name](node)


def _row_to_entry(row: MenuRow) -> IconMenuEntry:
    """Convert an engine row to an e-paper entry.

    Rows backed by a real catalog node (one with an ``id``) reuse
    :func:`node_to_entry` so the node's ``epaper`` style block is honored, with
    the row's dynamic label/icon and any enable-state footer (``description`` +
    ``trailing_icon`` checkbox) forwarded as overrides. Provider rows without a
    catalog node -- scan/device previews (``icon_image``) and radio-marked
    ``itemBind`` rows (whose synthetic ``set_value`` node has no ``id``/``epaper``)
    -- map directly.
    """
    if row.node and row.node.get("id"):
        return node_to_entry(
            row.node,
            label=row.label,
            icon=row.icon,
            enabled=row.enabled,
            description=row.description,
            trailing_icon=row.trailing_icon,
            icon_image=row.icon_image,
            icon_mask=row.icon_mask,
        )
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

    The choices come from a static option set (``outcome.option_set``) or, for a
    provider-backed select (engine/ELO/analysis-engine), from a runtime provider
    (``outcome.provider``) that yields :class:`MenuRow`s keyed by the value to
    store. Marking style: when an option/row carries its own ``icon`` (color ->
    piece glyph, engine -> ``engine`` icon) the icon is kept and the active row is
    marked with a leading ``"* "``; otherwise a radio glyph
    (``selected``/``unselected`` icon) marks the choice. Provider rows always have
    an icon, so they are starred rather than radio-marked.
    """
    selected_icon = outcome.selected_icon or "radio_checked"
    unselected_icon = outcome.unselected_icon or "radio_empty"

    def _choices() -> List[tuple]:
        """Return ``(value, label, icon, font_size)`` choices from provider/option set.

        Provider rows are keyed by the value to persist (``row.key``), always carry
        an icon, and have no per-row font size (``None``). Static option-set entries
        may omit an icon (radio-marked) and may carry an optional ``font_size`` (px)
        so a row can preview its own effect (the Text Size options render Small/
        Medium/Large at small/medium/large text).
        """
        if outcome.provider:
            return [(row.key, row.label, row.icon, None) for row in ctx.provide(outcome.provider)]
        return [
            (opt["value"], opt["label"], opt.get("icon"), opt.get("font_size"))
            for opt in ctx.options(outcome.option_set)
        ]

    def build_entries() -> List[IconMenuEntry]:
        current = str(ctx.get(outcome.store, outcome.key))
        entries: List[IconMenuEntry] = []
        for value, option_label, option_icon, option_font_size in _choices():
            selected = str(value) == current
            if option_icon:
                icon_name = option_icon
                label = f"* {option_label}" if selected else option_label
            else:
                icon_name = selected_icon if selected else unselected_icon
                label = option_label
            # Pass font_size only when the option declares one, so options without
            # it keep IconMenuEntry's default rather than being pinned to a guess.
            extra = {"font_size": option_font_size} if option_font_size is not None else {}
            entries.append(
                IconMenuEntry(
                    key=str(value),
                    label=label,
                    icon_name=icon_name,
                    enabled=True,
                    **extra,
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


class _Descent:
    """Result of a restore auto-descent from a container.

    ``result`` is the break/exit selection the deeper level returned (or None
    when it backed out normally); ``focus_index`` is the index of the row this
    container descended through, so it is re-focused when control returns.
    """

    __slots__ = ("result", "focus_index")

    def __init__(self, result: Optional[MenuSelection], focus_index: int):
        self.result = result
        self.focus_index = focus_index


def _auto_descend_restore(nav, container_id, state, build_entries, handle_selection) -> Optional[_Descent]:
    """Dispatch the row leading to the next saved container, if any.

    Peeks the saved path one level below this container. When a child row leads
    there -- a ``submenu`` whose ``target`` matches, or an ``action`` node whose
    declarative ``restore_target`` matches (the action-driven descents into
    WiFi/Bluetooth/About) -- that row is selected programmatically, which
    recurses via :func:`run_engine_menu` and replays the rest of the chain.

    Returns a :class:`_Descent` when a descent was performed, or ``None`` when
    the saved chain is exhausted or the next token has no static child here
    (a removed dynamic item) -- in which case the caller shows this list.
    """
    next_token = nav.next_restore_token()
    if next_token is None:
        return None

    build_entries()  # populate state["rows"] so handle_selection can dispatch
    for index, row in enumerate(state["rows"]):
        node = row.node or {}
        if node.get("target") == next_token or node.get("restore_target") == next_token:
            result = handle_selection(MenuSelection.from_key(row.key))
            return _Descent(result=result, focus_index=index)
    return None


def run_engine_menu(
    container_id: str,
    ctx: BoardMenuContext,
    menu_manager,
    *,
    platform: str = "board",
    initial_index: int = 0,
    initial_key: Optional[str] = None,
    catalog=None,
    nav_context: Any = None,
) -> Optional[MenuSelection]:
    """Run a catalog container as a board menu via the shared engine.

    Builds rows from the container's children each iteration (so toggles/values
    redraw with fresh state), shows them, and dispatches the selected node:
    ``stay`` redraws in place, ``submenu`` recurses into the target container,
    ``select`` opens an option list, ``action`` runs the bound action. Break
    results from any depth propagate to the caller, matching the board's
    existing nested-menu unwinding.

    Full-depth restore: unless the container is flagged ``restorable: false``,
    the loop records itself onto the shared :class:`MenuContext`
    (``enter_menu``/``leave_menu``) so the live navigation path always reflects
    every engine level the user is in. On restore, if the saved path descends
    further from this container, the row leading to the next saved container is
    dispatched automatically (matched by a ``submenu`` ``target`` or an action
    node's ``restore_target``), recursing until the saved chain is exhausted so
    the user lands back in the exact deepest submenu. A saved token that no
    static child leads to (a removed dynamic item) stops the descent at this
    list.

    Args:
        initial_index: Row index to focus first. Ignored when ``initial_key``
            resolves to a row.
        initial_key: Focus the first *selectable* row with this key instead of a
            fixed index. Preferred when the target row sits at a runtime-variable
            position (e.g. the Bluetooth Devices entry, which follows a variable
            number of dynamic status/readout rows) so focus tracks the row rather
            than a brittle index. Falls back to ``initial_index`` when no
            selectable row matches. On restore the saved index wins over both.
        catalog: Catalog to read children from; defaults to the shared cached
            catalog. Injectable so the loop can be tested with synthetic nodes.
        nav_context: MenuContext used to record/replay the navigation path.
            Defaults to the process-wide context injected via
            :func:`set_nav_context`; tests pass their own. ``None`` disables
            recording so the loop is a plain show/dispatch loop.

    Returns:
        The break/exit :class:`MenuSelection` that ended the loop, or None.
    """
    catalog = catalog or get_catalog()
    nav = nav_context if nav_context is not None else _nav_context

    # A container opts out of restore (recording + auto-descent) via
    # ``restorable: false`` -- dynamic item screens (device/player detail),
    # scan lists, and confirm dialogs, which must reopen at their parent list
    # rather than a stale item. Absent the flag (or absent a nav context) the
    # container records normally.
    recordable = False
    if nav is not None:
        node = catalog.get_node(container_id) if catalog.has_node(container_id) else {}
        recordable = node.get("restorable", True)

    state: Dict[str, List[MenuRow]] = {"rows": []}

    def build_entries() -> List[IconMenuEntry]:
        rows = build_rows(container_id, ctx, platform=platform, catalog=catalog)
        state["rows"] = rows
        return [_row_to_entry(row) for row in rows]

    if initial_key is not None:
        # Resolve the key to the index of the first selectable matching row, so
        # focus lands on the intended control regardless of how many dynamic
        # rows precede it. A non-selectable or absent match leaves initial_index.
        for index, row in enumerate(build_rows(container_id, ctx, platform=platform, catalog=catalog)):
            if row.key == initial_key and row.selectable:
                initial_index = index
                break

    def handle_selection(selection: MenuSelection) -> Optional[MenuSelection]:
        row = next((r for r in state["rows"] if r.key == selection.key), None)
        if row is None or (not row.action and not row.node):
            # Unknown key (e.g. an injected WIFI_REFRESH) or a display-only
            # provider row (a readout): nothing to dispatch, so redraw.
            return None
        outcome = dispatch_row(row, ctx)

        if outcome.kind == "stay":
            return None
        if outcome.kind == "submenu":
            sub = run_engine_menu(
                outcome.target, ctx, menu_manager, platform=platform, catalog=catalog, nav_context=nav
            )
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

    if not recordable:
        return menu_manager.run_menu_loop(build_entries, handle_selection, initial_index=initial_index)

    focus_index = nav.enter_menu(container_id, initial_index)
    result: Optional[MenuSelection] = None
    try:
        descended = _auto_descend_restore(nav, container_id, state, build_entries, handle_selection)
        if descended is not None:
            if descended.result is not None and descended.result.is_break:
                result = descended.result
                return result
            # We came back from the deeper level; focus the row we drilled
            # through so backing out lands where the user descended.
            focus_index = descended.focus_index
        else:
            # Restore stopped here (saved chain exhausted or the next saved
            # container is a removed dynamic item): drop any stale deeper path
            # so this list is the truthful persisted position.
            nav.truncate_below_current()
        # Persist the cursor on every move (not just on exit): a bare restart
        # (SIGTERM) interrupts the blocked menu wait, so a save-on-exit would
        # lose the live position. While this level's loop runs it is the active
        # (deepest) menu, so nav's depth matches and update_index writes this
        # container's index.
        result = menu_manager.run_menu_loop(
            build_entries, handle_selection, initial_index=focus_index,
            on_index_change=nav.update_index,
        )
        return result
    finally:
        # Pop this container from the persisted path only on an ordinary back-out.
        # A SHUTDOWN is latched by the MenuManager so every nested level returns
        # it during the unwind; a break (PLAY/resume) unwinds every menu to enter
        # a game. In both cases popping here would collapse the deep path
        # (e.g. Settings/connectivity/bluetooth back to Settings) before it is
        # persisted, so a restart could only restore to the top level. Leave the
        # path intact for those exits: a restart then reopens exactly where the
        # user was, and game entry clears the menu path on its own. An exception
        # (result stays None, e.g. WebCommandInterrupt) still pops to keep the
        # enter/leave stack balanced for the handler that catches it.
        if not (result is not None and (result.result_type == MenuResult.SHUTDOWN or result.is_break)):
            nav.leave_menu()


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
