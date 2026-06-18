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
  ``dynamic`` | ``action`` (plus container types the renderer walks).
- ``bind``: ``{"store": <name>, "key": <name>}`` -- the value the row reads/writes.
- ``label`` (required for rendered rows): default/web label; may contain the
  ``{value}`` placeholder.
- ``boardLabel``: optional board-only label override (e.g. an e-paper-sized
  abbreviation or a ``{value}`` template). Falls back to ``label`` when absent.
- ``icon``: a static icon id, or a state map ``{str(value): icon}`` resolved
  against the bound value (e.g. ``{"true": ..., "false": ...}`` for toggles).
- ``optionSet``: name of the option set backing ``select``/``cycle``.
- ``provider``: name of the dynamic-list provider for ``dynamic`` nodes.
- ``visibleWhen``: ``{"store", "key", "in": [...] | "equals": <v>}`` gating the row.
- ``enabledWhen``: same shape as ``visibleWhen``; gates the row's *enabled* flag
  (the row stays visible but is non-selectable when unmet).
- ``range``: ``{"min", "max", "step"?, "wrap"?}`` for ``range`` cyclers.
- ``value``: the fixed value a ``set_value`` (radio) row writes.
- ``action``: action name for ``action`` nodes.
- ``target``: child container id a ``submenu`` opens.
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol

# Placeholder substituted in a label/boardLabel with the bound value's display.
_VALUE_PLACEHOLDER = "{value}"


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
    # Optional board-only image glyph and trailing icon, used by dynamic provider
    # rows (e.g. sprite-sheet previews with a trailing radio marker). Left None
    # for ordinary catalog rows whose icon is resolved from the node.
    icon_image: Any = None
    icon_mask: Any = None
    trailing_icon: Optional[str] = None


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
    def run_action(self, name: str) -> Optional[str]: ...


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
    shows as ``"5 min (Blitz)"``); otherwise the raw value as text. Returns an
    empty string when the node has no binding, so a stray placeholder collapses
    rather than raising.
    """
    bind = node.get("bind")
    if not bind:
        return ""
    value = ctx.get(bind["store"], bind["key"])
    option_set = node.get("optionSet")
    if option_set:
        target = str(value)
        for option in ctx.options(option_set):
            if str(option.get("value")) == target:
                return option["label"]
        return target
    return str(value)


def resolve_label(node: dict, ctx: MenuContext, *, platform: str) -> str:
    """Resolve a node's display label for the given platform.

    ``boardLabel`` overrides only on the board and only when present (optional
    e-paper abbreviation/template); the web always uses ``label``. A ``{value}``
    placeholder is substituted with the bound value's display text.
    """
    template = node.get("label", "")
    if platform == "board" and node.get("boardLabel") is not None:
        template = node["boardLabel"]
    if _VALUE_PLACEHOLDER in template:
        template = template.replace(_VALUE_PLACEHOLDER, _display_value(node, ctx))
    return template


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
    """Evaluate a ``{store, key, in|equals}`` condition against bound state.

    Shared by ``visibleWhen`` and ``enabledWhen`` so the two gates cannot drift.
    An unrecognized condition shape returns True (fails open).
    """
    value = ctx.get(condition["store"], condition["key"])
    if "in" in condition:
        return value in condition["in"]
    if "equals" in condition:
        return value == condition["equals"]
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
            rows.extend(ctx.provide(child["provider"]))
            continue
        rows.append(
            MenuRow(
                key=child.get("key", child["id"]),
                label=resolve_label(child, ctx, platform=platform),
                icon=resolve_icon(child, ctx),
                enabled=is_enabled(child, ctx),
                help=child.get("help"),
                node=child,
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
        bind = node["bind"]
        return DispatchOutcome(
            kind="select",
            option_set=node["optionSet"],
            store=bind["store"],
            key=bind["key"],
            selected_icon=node.get("selectedIcon"),
            unselected_icon=node.get("unselectedIcon"),
        )

    if node_type == "dynamic":
        return DispatchOutcome(kind="dynamic", provider=node["provider"])

    if node_type == "action":
        name = node["action"]
        signal = ctx.run_action(name)
        return DispatchOutcome(kind="action", action=name, signal=signal)

    raise ValueError(f"unsupported menu node type: {node_type!r} (node {node.get('id')!r})")
