"""Platform-agnostic, data-driven menu engine.

The shared catalog (``menu.json``) is the single source of truth for the whole
menu tree: structure, labels, help, icons, and -- via the schema this module
consumes -- each node's *behavior* and *data binding*. This engine turns those
node definitions into renderable rows and resolves what a selection does, so the
board and the web stop hand-coding a separate builder + dispatcher per menu.

Separation of concerns:
- This module is pure logic over node dicts plus an injected :class:`MenuContext`.
  It never imports a renderer, settings store, or framework.
- Each platform supplies a ``MenuContext`` (how to read/write values, resolve
  option sets, expand dynamic providers, run actions) and a thin loop/translator
  that shows rows and feeds the selected node back into :func:`dispatch`.

Node behavior schema (fields read here; all optional unless noted):
- ``type`` (required): ``submenu`` | ``select`` | ``toggle`` | ``cycle`` |
  ``range`` | ``set_value`` | ``dynamic`` | ``action`` | ``text`` | ``info``
  (plus container types the renderer walks). ``text`` is a free-string field
  edited via its ``action`` on the board and rendered as an input on the web.
  ``info`` is a display-only readout (typically with ``epaper.selectable`` false)
  whose label may carry ``{value}``/``{fn:NAME}`` tokens.
- ``bind``: ``{"store": <name>, "key": <name>}`` -- the value the row reads/writes.
- ``label`` (required for rendered rows): default/web label; may contain the
  ``{value}`` placeholder.
- ``boardLabel``: optional board-only label override (e.g. an e-paper-sized
  abbreviation or a ``{value}`` template). Falls back to ``label`` when absent.
- ``icon``: a static icon id, or a state map ``{str(value): icon}`` resolved
  against the bound value (e.g. ``{"true": ..., "false": ...}`` for toggles).
- ``optionSet``: name of the option set backing ``select``/``cycle``.
- ``provider``: name of the dynamic-list provider. Used by ``dynamic`` nodes and
  by provider-backed ``select`` nodes whose choices are a runtime list (installed
  engines, per-engine ELO levels) rather than a static option set; a ``select``
  carries either ``optionSet`` or ``provider``.
- ``visibleWhen``: ``{"store", "key", "in": [...] | "equals": <v> | "notEquals": <v>}``
  gating the row, or ``{"allOf": [<condition>, ...]}`` to require every subcondition (AND).
- ``enabledWhen``: same shape as ``visibleWhen``; gates the row's *enabled* flag
  (the row stays visible but is non-selectable when unmet).
- ``range``: ``{"min", "max", "step"?, "wrap"?}`` for ``range`` cyclers.
- ``value``: the fixed value a ``set_value`` (radio) row writes.
- ``valueDefault``: text shown for a ``{value}`` token when the bound value is
  unset (``None``/empty), e.g. an unnamed human renders as "Human". Keeps the
  placeholder declarative instead of faking it in the value store.
- ``action``: action name for ``action`` nodes.
- ``target``: child container id a ``submenu`` opens.
- ``itemAction``: on a ``dynamic`` node, the action run when one of its provider
  rows is selected (called with the row's key). Makes a runtime list actionable.
- ``itemBind``: on a ``dynamic`` node, ``{"store", "key"}`` making its provider
  rows a radio set -- selecting a row writes the row's key to that bound value and
  the row matching the current value is radio-marked. Mutually used in place of
  ``itemAction`` (set-a-value vs run-an-action); the provider stays pure data.
- ``selectedIcon``/``unselectedIcon``: optional radio glyphs for an ``itemBind``
  set (default ``radio_checked``/``radio_empty``).
"""

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol

# Label tokens of the form ``{token}``. ``{value}`` resolves to the node's bound
# value; ``{fn:NAME}`` resolves to the context's named compute helper. Anything
# else is left intact so a literal brace is not mangled.
_TOKEN_PATTERN = re.compile(r"\{([^{}]+)\}")
_COMPUTE_PREFIX = "fn:"


@dataclass
class MenuRow:
    """A resolved, platform-neutral menu row.

    The board adapter converts this to an ``IconMenuEntry`` and the web adapter
    to a control; both consume the same resolved fields so a row looks and
    behaves consistently across platforms. ``node`` is the originating catalog
    node so the adapter can dispatch the selection back through :func:`dispatch`.
    """

    key: str
    label: str
    icon: str
    enabled: bool = True
    help: Optional[str] = None
    node: dict = field(default_factory=dict)
    # Whether the row can be focused/selected. False for display-only rows
    # (e.g. About's Version and telemetry readouts), which the cursor skips and
    # which therefore are never dispatched.
    selectable: bool = True
    # Optional board-only image glyph and trailing icon, used by dynamic provider
    # rows (e.g. sprite-sheet previews with a trailing radio marker). Left None
    # for ordinary catalog rows whose icon is resolved from the node.
    icon_image: Any = None
    icon_mask: Any = None
    trailing_icon: Optional[str] = None
    # Optional secondary line rendered below the icon+label (board vertical
    # layout). Used by the WiFi/Bluetooth merged status button to show the
    # radio's Enabled/Disabled state next to a checkbox (the trailing_icon),
    # making it read as a toggle. None for ordinary rows.
    description: Optional[str] = None
    # Action to run when this row is selected, with the row's ``key`` passed as
    # the argument. Set on ``dynamic`` provider rows whose container node declares
    # an ``itemAction`` so a runtime-listed item (e.g. a scanned WiFi network) can
    # act on its own identity. ``None`` for ordinary rows, which instead dispatch
    # through their catalog ``node``.
    action: Optional[str] = None


@dataclass
class DispatchOutcome:
    """What a platform adapter should do after a node is selected.

    ``kind`` is the instruction; the remaining fields carry its payload:
    - ``stay``: value mutated in place (toggle/cycle); redraw the same menu.
    - ``submenu``: open ``target`` container.
    - ``select``: open the ``option_set`` list and write the chosen value to
      ``store``/``key``.
    - ``dynamic``: open the list produced by ``provider``.
    - ``action``: ``action`` was invoked via the context; ``signal`` carries any
      string the action returned (e.g. a break/exit token) for the adapter to act on.
    """

    kind: str
    target: Optional[str] = None
    option_set: Optional[str] = None
    store: Optional[str] = None
    key: Optional[str] = None
    provider: Optional[str] = None
    action: Optional[str] = None
    signal: Optional[str] = None
    # Optional per-node icons for the option list a ``select`` opens; when unset
    # the adapter falls back to radio glyphs.
    selected_icon: Optional[str] = None
    unselected_icon: Optional[str] = None


class MenuContext(Protocol):
    """Platform-supplied side-effect boundary the engine depends on.

    Keeping these behind a protocol is what makes the engine reusable: the board
    backs them with its settings/sound/update stores and e-paper providers; the
    web backs them with its settings API and data sources.
    """

    def get(self, store: str, key: str) -> Any: ...
    def set(self, store: str, key: str, value: Any) -> None: ...
    def options(self, name: str) -> List[dict]: ...
    def provide(self, provider: str) -> List["MenuRow"]: ...
    def run_action(self, name: str, arg: Optional[str] = None) -> Optional[str]: ...
    def compute(self, name: str, node: dict) -> str: ...


def _icon_state_key(value: Any) -> str:
    """Return the state-map lookup key for a bound value.

    Booleans map to ``"true"``/``"false"`` (the natural authoring form in JSON);
    everything else uses its string form so numeric/string option values can key
    their own icons.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _display_value(node: dict, ctx: MenuContext) -> str:
    """Resolve the bound value's display text for a ``{value}`` label.

    Uses the node's option set label when present (so a stored value like ``5``
    shows as ``"5 min (Blitz)"``). For a provider-backed ``select`` (which has a
    ``provider`` instead of an ``optionSet``, e.g. ELO/Engine), the label is
    resolved from the provider's rows -- the same source the submenu renders --
    so the parent ``{value}`` button cannot drift from the submenu (an uncapped
    ELO "Default" section shows as "Unlimited" in both). Falls back to the raw
    value as text when no label is found. Returns an empty string when the node
    has no binding, so a stray placeholder collapses rather than raising.

    When the bound value is unset (``None`` or empty string) and the node
    declares ``valueDefault``, that default is shown instead. This keeps the
    placeholder text declarative in the catalog (e.g. an unnamed human shows
    "Human") rather than fabricating it in the value store -- the store stays
    truthful and returns the real empty value, which other consumers (the
    keyboard prefill, the game's PGN name) rely on.
    """
    bind = node.get("bind")
    if not bind:
        return ""
    value = ctx.get(bind["store"], bind["key"])
    if value is None or value == "":
        default = node.get("valueDefault")
        if default is not None:
            return default
    target = str(value)
    option_set = node.get("optionSet")
    if option_set:
        for option in ctx.options(option_set):
            if str(option.get("value")) == target:
                return option["label"]
        return target
    provider = node.get("provider")
    if provider:
        for row in ctx.provide(provider):
            if str(row.key) == target:
                return row.label
        return target
    return str(value)


def resolve_label(node: dict, ctx: MenuContext, *, platform: str) -> str:
    """Resolve a node's display label for the given platform.

    ``boardLabel`` overrides only on the board and only when present (optional
    e-paper abbreviation/template); the web always uses ``label``. Tokens in the
    chosen template are substituted:
    - ``{value}`` -> the node's bound value display (option-set label or raw);
    - ``{fn:NAME}`` -> ``ctx.compute(NAME, node)`` for genuinely computed text
      (composed summaries) that no single bound value can express.

    Keeping the surrounding template in the catalog while delegating only the
    computed token to an injected helper avoids both a bespoke per-label function
    and a label mini-language. An unrecognized token is left intact.
    """
    template = node.get("label", "")
    if platform == "board" and node.get("boardLabel") is not None:
        template = node["boardLabel"]

    def _replace(match: "re.Match") -> str:
        token = match.group(1)
        if token == "value":  # noqa: S105  # nosec B105 - a template placeholder name, not a credential
            return _display_value(node, ctx)
        if token.startswith(_COMPUTE_PREFIX):
            return ctx.compute(token[len(_COMPUTE_PREFIX):], node)
        return match.group(0)

    return _TOKEN_PATTERN.sub(_replace, template)


def resolve_icon(node: dict, ctx: MenuContext) -> str:
    """Resolve a node's icon, honoring a state-mapped icon for bound values.

    A string icon is returned as-is. A dict icon is treated as a state map keyed
    by the bound value (``true``/``false`` for booleans), with an optional
    ``default`` entry; an unmatched value yields an empty icon rather than raising.
    """
    icon = node.get("icon", "")
    if isinstance(icon, dict):
        bind = node.get("bind")
        if not bind:
            return icon.get("default", "")
        state_key = _icon_state_key(ctx.get(bind["store"], bind["key"]))
        return icon.get(state_key, icon.get("default", ""))
    return icon


def is_visible(node: dict, ctx: MenuContext) -> bool:
    """Return whether a node's ``visibleWhen`` condition is satisfied.

    Nodes without a condition are always visible. The condition reads a bound
    value and matches it against ``in`` (membership) or ``equals`` (equality);
    an unrecognized condition shape is treated as visible so a malformed gate
    fails open rather than hiding a control silently.
    """
    condition = node.get("visibleWhen")
    if not condition:
        return True
    return _condition_met(condition, ctx)


def _condition_met(condition: dict, ctx: MenuContext) -> bool:
    """Evaluate a ``{store, key, in|equals|notEquals}`` condition against bound state.

    Shared by ``visibleWhen`` and ``enabledWhen`` so the two gates cannot drift.
    A compound ``{"allOf": [<condition>, ...]}`` is satisfied only when *every*
    subcondition holds (logical AND), letting a row depend on more than one bound
    value -- e.g. 'Show Graph' requires both the master analysis compute
    (analysis.mode) and the analysis widget (game.show_analysis). An unrecognized
    condition shape returns True (fails open).
    """
    if "allOf" in condition:
        return all(_condition_met(sub, ctx) for sub in condition["allOf"])
    value = ctx.get(condition["store"], condition["key"])
    if "in" in condition:
        return value in condition["in"]
    if "equals" in condition:
        return value == condition["equals"]
    if "notEquals" in condition:
        return value != condition["notEquals"]
    return True


def is_enabled(node: dict, ctx: MenuContext) -> bool:
    """Return whether a node is selectable.

    ``enabledWhen`` (when present) gates the enabled flag from another bound
    value -- e.g. 'Show Graph' is selectable only while 'Show Analysis' is on.
    Without it, the static ``enabled`` flag applies (default True). Unlike
    ``visibleWhen``, a disabled row still renders so the user sees the option and
    why it is unavailable.
    """
    condition = node.get("enabledWhen")
    if condition:
        return _condition_met(condition, ctx)
    return node.get("enabled", True)


def build_rows(container_id: str, ctx: MenuContext, *, platform: str, catalog) -> List[MenuRow]:
    """Build the resolved rows for a container's children, in declared order.

    The generic constructor that replaces the per-menu builders: it filters
    hidden rows (``visibleWhen``), expands ``dynamic`` nodes via their provider,
    and resolves each remaining node's label/icon/enabled for the platform.

    Args:
        container_id: Catalog id of the container whose children to render.
        ctx: Platform context supplying values, option sets, and providers.
        platform: ``"board"`` or ``"web"`` (selects board label override).
        catalog: Object exposing ``children(container_id) -> list[node]``.
    """
    rows: List[MenuRow] = []
    for child in catalog.children(container_id):
        if not is_visible(child, ctx):
            continue
        if child.get("type") == "dynamic":
            # A dynamic node may declare an ``itemAction`` run when one of its
            # provider rows is selected, with the row's key as the argument (e.g.
            # connecting to a scanned WiFi network). Tag each row that does not
            # already carry its own action so selection routes there; rows without
            # an item action (display-only readouts) stay inert.
            #
            # Alternatively it may declare an ``itemBind`` (a {store,key}) to make
            # the provider rows a radio set: selecting a row writes the row's key
            # to that bound value, and the row matching the current value is
            # radio-marked. The engine owns both the per-row ``set_value`` behavior
            # and the marker so the provider stays a pure data source (it returns
            # only the list + any preview glyphs, not dispatch/marking logic).
            item_action = child.get("itemAction")
            item_bind = child.get("itemBind")
            if item_bind is not None:
                current = str(ctx.get(item_bind["store"], item_bind["key"]))
                selected_icon = child.get("selectedIcon", "radio_checked")
                unselected_icon = child.get("unselectedIcon", "radio_empty")
            for row in ctx.provide(child["provider"]):
                if item_action and row.action is None:
                    row.action = item_action
                if item_bind is not None:
                    if not row.node:
                        row.node = {"type": "set_value", "bind": item_bind, "value": row.key}
                    if row.trailing_icon is None:
                        row.trailing_icon = selected_icon if row.key == current else unselected_icon
                rows.append(row)
            continue
        rows.append(
            MenuRow(
                key=child.get("key", child["id"]),
                label=resolve_label(child, ctx, platform=platform),
                icon=resolve_icon(child, ctx),
                enabled=is_enabled(child, ctx),
                help=child.get("help"),
                node=child,
                selectable=child.get("epaper", {}).get("selectable", True),
            )
        )
    return rows


def _next_option_value(option_set: List[dict], current: Any) -> Any:
    """Return the option value after ``current``, wrapping at the end.

    Used by in-place cyclers. When the current value is not found, starts at the
    first option so a drifted/unset value still advances predictably.
    """
    values = [option["value"] for option in option_set]
    if not values:
        return current
    target = str(current)
    for index, value in enumerate(values):
        if str(value) == target:
            return values[(index + 1) % len(values)]
    return values[0]


def dispatch(node: dict, ctx: MenuContext) -> DispatchOutcome:
    """Perform the effect of selecting ``node`` and return what to do next.

    Pure routing over the node's ``type``: ``toggle``/``cycle`` mutate the bound
    value in place (``stay``); ``submenu``/``select``/``dynamic`` return a
    descriptor for the adapter to open; ``action`` runs the named action through
    the context. An unsupported type raises so an unmigrated/typo'd node fails
    loudly instead of becoming a dead row.
    """
    node_type = node.get("type")

    if node_type == "toggle":
        bind = node["bind"]
        ctx.set(bind["store"], bind["key"], not ctx.get(bind["store"], bind["key"]))
        return DispatchOutcome(kind="stay")

    if node_type == "cycle":
        bind = node["bind"]
        nxt = _next_option_value(ctx.options(node["optionSet"]), ctx.get(bind["store"], bind["key"]))
        ctx.set(bind["store"], bind["key"], nxt)
        return DispatchOutcome(kind="stay")

    if node_type == "range":
        bind = node["bind"]
        spec = node["range"]
        step = spec.get("step", 1)
        nxt = int(ctx.get(bind["store"], bind["key"])) + step
        if nxt > spec["max"]:
            nxt = spec["min"] if spec.get("wrap", True) else spec["max"]
        ctx.set(bind["store"], bind["key"], nxt)
        return DispatchOutcome(kind="stay")

    if node_type == "set_value":
        bind = node["bind"]
        ctx.set(bind["store"], bind["key"], node["value"])
        return DispatchOutcome(kind="stay")

    if node_type == "submenu":
        return DispatchOutcome(kind="submenu", target=node.get("target"))

    if node_type == "select":
        # A select sources its choices from either a static ``optionSet`` or a
        # runtime ``provider`` (e.g. installed engines / per-engine ELO levels).
        # Both are carried on the outcome so the adapter knows where to read the
        # list from; exactly one is set for a given node.
        bind = node["bind"]
        return DispatchOutcome(
            kind="select",
            option_set=node.get("optionSet"),
            provider=node.get("provider"),
            store=bind["store"],
            key=bind["key"],
            selected_icon=node.get("selectedIcon"),
            unselected_icon=node.get("unselectedIcon"),
        )

    if node_type == "info":
        # Display-only row (e.g. About's Version/telemetry). It renders
        # non-selectable, so this is reached only if a caller dispatches it
        # directly; treat it as a no-op rather than raising.
        return DispatchOutcome(kind="stay")

    if node_type == "dynamic":
        return DispatchOutcome(kind="dynamic", provider=node["provider"])

    if node_type in ("action", "text"):
        # ``text`` is a free-string field: the web renders an input, while the
        # board edits it through a named action (a keyboard widget). Both share
        # one catalog node; selecting it on the board runs that action. The
        # action name is required here because a board-dispatched text node must
        # declare how it is edited (web-only text fields are never dispatched).
        name = node["action"]
        signal = ctx.run_action(name)
        return DispatchOutcome(kind="action", action=name, signal=signal)

    raise ValueError(f"unsupported menu node type: {node_type!r} (node {node.get('id')!r})")


def dispatch_row(row: MenuRow, ctx: MenuContext) -> DispatchOutcome:
    """Resolve selecting a built row, supporting actionable provider items.

    A row that carries an ``action`` (set from its container's ``itemAction``) is
    a runtime-listed item -- e.g. a scanned WiFi network. Selecting it runs that
    action with the row's ``key`` as the argument (the item's identity) and
    propagates any returned signal, so the engine can act on data that has no
    static catalog node of its own. Every other row dispatches through its catalog
    ``node`` exactly as :func:`dispatch` defines.

    Callers must not pass display-only rows (no ``action`` and no ``node``); those
    are non-selectable and have no behavior to resolve.
    """
    if row.action:
        signal = ctx.run_action(row.action, row.key)
        return DispatchOutcome(kind="action", action=row.action, signal=signal)
    return dispatch(row.node, ctx)
