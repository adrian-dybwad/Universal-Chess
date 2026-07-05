"""Menu catalog loader and validator.

Loads the shared menu catalog (``menu.json``) and icon registry (``icons.json``)
that drive both the e-paper board menus and the React web UI. The catalog is the
single source of truth for menu structure, labels, icons, help tips, e-paper
styles, and web field metadata; this module is the only place that knows the
on-disk format.

Validation is strict and fails loudly: a malformed catalog is a build-time
authoring error, not a runtime condition to paper over. ``CatalogError`` is
raised with a precise message (which id, which dangling reference) so the
mistake is fixed at the source rather than degrading the menu silently.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional

_CATALOG_DIR = Path(__file__).parent
_MENU_FILE = _CATALOG_DIR / "menu.json"
_ICONS_FILE = _CATALOG_DIR / "icons.json"

# Web control types a node's optional ``webType`` may name. ``webType`` overrides
# the board ``type`` for the web renderer only -- used where the same node is an
# imperative ``action`` on the board (e.g. the chained engine -> ELO picker) but a
# plain control on the web. Restricting the set turns a typo into a load-time
# error instead of a silently blank web row.
_WEB_CONTROL_TYPES = frozenset({"toggle", "select", "cycle", "range", "text"})


class CatalogError(ValueError):
    """Raised when the menu catalog or icon registry is structurally invalid.

    Subclasses ``ValueError`` because an invalid catalog is bad input data. It
    is raised eagerly at load time so authoring mistakes surface in tests and on
    startup rather than as a missing/blank menu entry later.
    """


class MenuCatalog:
    """In-memory, validated view of the menu catalog.

    Provides id-keyed lookup over the flat node list plus access to the section
    list and option sets. Construct via :func:`load_catalog` (which validates);
    the constructor assumes already-parsed dicts.
    """

    def __init__(self, menu_data: dict, icons_data: dict):
        self._menu = menu_data
        self._icons = icons_data
        self._nodes_by_id: Dict[str, dict] = {n["id"]: n for n in menu_data.get("nodes", [])}

    # -- raw access -------------------------------------------------------

    def raw_menu(self) -> dict:
        """Return the raw parsed ``menu.json`` (for serving to the web UI)."""
        return self._menu

    def icon_ids(self) -> "set[str]":
        """Return the set of registered icon ids."""
        return set(self._icons.get("icons", {}).keys())

    # -- node access ------------------------------------------------------

    def get_node(self, node_id: str) -> dict:
        """Return the node with ``node_id``.

        Raises:
            KeyError: if no node has that id. Callers that pass ids from the
                catalog itself (children/roots/target) can rely on these
                resolving because :func:`load_catalog` validates every
                reference; an unknown id therefore signals a caller bug.
        """
        return self._nodes_by_id[node_id]

    def has_node(self, node_id: str) -> bool:
        """Return whether a node with ``node_id`` exists."""
        return node_id in self._nodes_by_id

    def children(self, node_id: str) -> List[dict]:
        """Return the child node dicts of a container node, in declared order."""
        node = self._nodes_by_id[node_id]
        return [self._nodes_by_id[cid] for cid in node.get("children", [])]

    def child_ids(self, node_id: str) -> List[str]:
        """Return the child ids of a container node, in declared order."""
        return list(self._nodes_by_id[node_id].get("children", []))

    def roots(self) -> List[str]:
        """Return the declared root container ids."""
        return list(self._menu.get("roots", []))

    def sections(self) -> List[dict]:
        """Return the ordered web section (tab) descriptors."""
        return list(self._menu.get("sections", []))

    def option_set(self, name: str) -> List[dict]:
        """Return the option list for an option set name."""
        return list(self._menu.get("optionSets", {}).get(name, []))

    def option_label(self, name: str, value: object, default: Optional[str] = None) -> str:
        """Return the label for ``value`` within the named option set.

        The board and web render the same choices, so both resolve a stored
        value (e.g. a player type or update channel) to its display label
        through this one source rather than each keeping a private value->label
        map. Comparison is by string form because option values are authored as
        strings while some callers hold the value as an int (e.g. time control
        minutes).

        Args:
            name: Option set name (e.g. ``"player_type"``).
            value: Stored value to look up; compared by ``str(value)``.
            default: Returned when the value is absent from the set. When
                ``None`` the stringified value itself is returned, so an
                unexpected value stays visible instead of rendering blank.
        """
        target = str(value)
        for option in self._menu.get("optionSets", {}).get(name, []):
            if str(option.get("value")) == target:
                return option["label"]
        return default if default is not None else target

    def fields_for_section(self, section_id: str) -> List[dict]:
        """Return field nodes tagged with ``section`` equal to ``section_id``.

        Order follows the node declaration order in ``menu.json``. Used by the
        web renderer to lay out a tab's rows from the catalog.
        """
        return [
            n
            for n in self._menu.get("nodes", [])
            if n.get("section") == section_id and n.get("id", "").startswith("field.")
        ]


def _validate(menu_data: dict, icons_data: dict) -> None:
    """Validate the catalog cross-references, raising :class:`CatalogError`.

    Checks performed (each guards a distinct authoring mistake):
    - node ids present and unique (duplicate ids would silently shadow);
    - every ``icon`` is registered -- a string icon, or each value of a
      state-map icon ``{state: icon}`` (typo -> board renders a blank placeholder);
    - every ``children``/``target``/``roots`` id resolves (dangling navigation);
    - every ``optionSet`` reference resolves (empty select on the web);
    - every ``section`` reference resolves (field rendered under no tab);
    - ``bind``/``visibleWhen`` (when present) carry the required ``store``/``key``
      (a typo here would otherwise surface only as a dead control at runtime).

    These checks fire only for fields that are present, so nodes not yet migrated
    to the behavior schema remain valid; strict per-type requirements are
    tightened in a later migration stage.
    """
    icon_ids = set(icons_data.get("icons", {}).keys())
    if not icon_ids:
        raise CatalogError("icon registry is empty")

    nodes = menu_data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise CatalogError("menu catalog has no nodes")

    ids: "set[str]" = set()
    for node in nodes:
        node_id = node.get("id")
        if not node_id:
            raise CatalogError(f"node missing 'id': {node!r}")
        if node_id in ids:
            raise CatalogError(f"duplicate node id: {node_id}")
        ids.add(node_id)
        if "type" not in node:
            raise CatalogError(f"node '{node_id}' missing 'type'")

    section_ids = {s.get("id") for s in menu_data.get("sections", [])}
    option_set_names = set(menu_data.get("optionSets", {}).keys())

    for node in nodes:
        node_id = node["id"]

        icon = node.get("icon")
        if isinstance(icon, dict):
            for state, icon_name in icon.items():
                if icon_name and icon_name not in icon_ids:
                    raise CatalogError(
                        f"node '{node_id}' state-map icon references unknown icon "
                        f"'{icon_name}' (state '{state}')"
                    )
        elif icon is not None and icon not in icon_ids:
            raise CatalogError(f"node '{node_id}' references unknown icon '{icon}'")

        bind = node.get("bind")
        if bind is not None and not (isinstance(bind, dict) and "store" in bind and "key" in bind):
            raise CatalogError(f"node '{node_id}' has malformed 'bind' (need store and key): {bind!r}")

        item_bind = node.get("itemBind")
        if item_bind is not None and not (
            isinstance(item_bind, dict) and "store" in item_bind and "key" in item_bind
        ):
            raise CatalogError(
                f"node '{node_id}' has malformed 'itemBind' (need store and key): {item_bind!r}"
            )

        visible_when = node.get("visibleWhen")
        if visible_when is not None and not (
            isinstance(visible_when, dict) and "store" in visible_when and "key" in visible_when
        ):
            raise CatalogError(
                f"node '{node_id}' has malformed 'visibleWhen' (need store and key): {visible_when!r}"
            )

        for child_id in node.get("children", []):
            if child_id not in ids:
                raise CatalogError(f"node '{node_id}' references unknown child '{child_id}'")

        target = node.get("target")
        if target is not None and target not in ids:
            raise CatalogError(f"node '{node_id}' references unknown target '{target}'")

        # ``restore_target`` names the container an action row opens, so full-depth
        # menu restore can auto-descend across an action boundary (e.g. Connectivity
        # -> Bluetooth). It must resolve like ``target`` so a typo fails at load
        # rather than silently breaking restore for that branch.
        restore_target = node.get("restore_target")
        if restore_target is not None and restore_target not in ids:
            raise CatalogError(
                f"node '{node_id}' references unknown restore_target '{restore_target}'"
            )

        option_set = node.get("optionSet")
        if option_set is not None and option_set not in option_set_names:
            raise CatalogError(f"node '{node_id}' references unknown optionSet '{option_set}'")

        web_type = node.get("webType")
        if web_type is not None and web_type not in _WEB_CONTROL_TYPES:
            raise CatalogError(
                f"node '{node_id}' has unknown webType '{web_type}' "
                f"(expected one of {sorted(_WEB_CONTROL_TYPES)})"
            )

        section = node.get("section")
        if section is not None and section not in section_ids:
            raise CatalogError(f"node '{node_id}' references unknown section '{section}'")

    for root_id in menu_data.get("roots", []):
        if root_id not in ids:
            raise CatalogError(f"root references unknown node '{root_id}'")

    for section in menu_data.get("sections", []):
        icon = section.get("icon")
        if icon is not None and icon not in icon_ids:
            raise CatalogError(f"section '{section.get('id')}' references unknown icon '{icon}'")


def load_catalog(
    menu_path: Optional[Path] = None,
    icons_path: Optional[Path] = None,
) -> MenuCatalog:
    """Load and validate the menu catalog.

    Args:
        menu_path: Override for ``menu.json`` (tests). Defaults to the packaged file.
        icons_path: Override for ``icons.json`` (tests). Defaults to the packaged file.

    Returns:
        A validated :class:`MenuCatalog`.

    Raises:
        CatalogError: if either file is missing, unparseable, or fails validation.
    """
    menu_path = menu_path or _MENU_FILE
    icons_path = icons_path or _ICONS_FILE

    try:
        menu_data = json.loads(Path(menu_path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"menu catalog not found: {menu_path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"menu catalog is not valid JSON: {exc}") from exc

    try:
        icons_data = json.loads(Path(icons_path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"icon registry not found: {icons_path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"icon registry is not valid JSON: {exc}") from exc

    _validate(menu_data, icons_data)
    return MenuCatalog(menu_data, icons_data)


_cached_catalog: Optional[MenuCatalog] = None


def get_catalog() -> MenuCatalog:
    """Return the process-wide cached catalog, loading it on first use.

    The catalog is static data deployed with the package, so a single validated
    instance is shared. Tests that need a fresh/overridden catalog should call
    :func:`load_catalog` directly rather than this cached accessor.
    """
    global _cached_catalog
    if _cached_catalog is None:
        _cached_catalog = load_catalog()
    return _cached_catalog
