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

import copy
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

_CATALOG_DIR = Path(__file__).parent
_MENU_FILE = _CATALOG_DIR / "menu.json"
_ICONS_FILE = _CATALOG_DIR / "icons.json"

# Option/help copy may name this board's mDNS URL. Authored as ``{mdns_url}`` and
# filled at serve/render time from the live hostname -- never a hardcoded
# ``http://dgt.local/`` example, which is wrong on every board that is not named
# ``dgt`` (the same class of bug the TLS cert once had).
# The security linters read the ``_TOKEN`` name as a credential; this is a
# substitution placeholder that ships in translated copy, not a secret.
MDNS_URL_TOKEN = "{mdns_url}"  # noqa: S105  # nosec B105 - substitution placeholder

# Optional clause for boards that have Wi-Fi (onboard or dongle). Authored as
# ``{wifi_or_ethernet_reach: Reach the chess board only over Wi-Fi or Ethernet.}``
# so each locale embeds its own sentence; stripped entirely on a plain Pi Zero
# with no wireless hardware (see board.wireless_capability).
_WIFI_REACH_TOKEN_RE = re.compile(r"\{wifi_or_ethernet_reach:([^}]*)\}")


def fill_runtime_placeholders(
    text: str,
    *,
    mdns_name: Optional[str] = None,
    has_wifi: Optional[bool] = None,
) -> str:
    """Replace runtime tokens in catalog strings with this device's values.

    - ``{mdns_url}`` -> ``http://<short-hostname>.local/`` (lowercased).
    - ``{wifi_or_ethernet_reach:...}`` -> the clause when the board has Wi-Fi
      (or when ``has_wifi`` is unknown -- fail open), else empty. A plain Pi Zero
      without a dongle must not promise Wi-Fi reachability.

    ``mdns_name`` / ``has_wifi`` are injectable for tests; production reads
    :func:`tls.current_mdns_name` and :func:`get_wireless_capability`.
    """
    if not text:
        return text
    if MDNS_URL_TOKEN in text:
        if mdns_name is None:
            from universalchess.tls import current_mdns_name
            mdns_name = current_mdns_name()
        text = text.replace(MDNS_URL_TOKEN, f"http://{mdns_name.lower()}/")
    if _WIFI_REACH_TOKEN_RE.search(text):
        # None (unread) fails open: keep the Wi-Fi clause rather than hide
        # reachability advice from a board that has Wi-Fi.
        include_reach = has_wifi is not False
        text = _WIFI_REACH_TOKEN_RE.sub(
            (lambda m: m.group(1) if include_reach else ""),
            text,
        )
    return text


def fill_option_runtime_placeholders(
    options: List[dict],
    *,
    mdns_name: Optional[str] = None,
    has_wifi: Optional[bool] = None,
) -> List[dict]:
    """Return option dicts with ``description`` (and ``help``) tokens filled.

    Shallow-copies only options that need a rewrite so callers can mutate the
    returned list without touching the cached catalog. When ``has_wifi`` is
    omitted, the live wireless capability is read once for the whole list.
    """
    wifi = has_wifi
    if wifi is None:
        try:
            from universalchess.board.wireless_capability import get_wireless_capability
            wifi = get_wireless_capability().has_wifi
        except Exception as exc:  # noqa: BLE001 - probe must not break menus
            log.debug("catalog placeholders: wireless capability unread (%s)", exc)
            wifi = None

    filled: List[dict] = []
    for option in options:
        description = option.get("description")
        help_text = option.get("help")
        new_description = (
            fill_runtime_placeholders(description, mdns_name=mdns_name, has_wifi=wifi)
            if isinstance(description, str)
            else description
        )
        new_help = (
            fill_runtime_placeholders(help_text, mdns_name=mdns_name, has_wifi=wifi)
            if isinstance(help_text, str)
            else help_text
        )
        if new_description is description and new_help is help_text:
            filled.append(option)
            continue
        rewritten = dict(option)
        if new_description is not description:
            rewritten["description"] = new_description
        if new_help is not help_text:
            rewritten["help"] = new_help
        filled.append(rewritten)
    return filled

# Per-locale translation overlays keyed by node id / option-set name / section
# id. English is the authored source in ``menu.json`` and needs no overlay; each
# other locale supplies only the strings it translates, and any key absent from
# the overlay falls back to the English original. Kept as sidecar files (rather
# than inline in menu.json) so the structural catalog stays a single untouched
# source of truth and adding a language is one new file, no schema churn.
_TRANSLATIONS_DIR = _CATALOG_DIR / "translations"

# The source locale, authored directly in ``menu.json``; it has no overlay and
# ``localize_catalog`` returns the catalog unchanged for it. Matches
# ``language_service.DEFAULT`` but is duplicated here to keep the loader free of
# a hard dependency on the service for the common (English) path.
_SOURCE_LOCALE = "en"

# Web control types a node's optional ``webType`` may name. ``webType`` overrides
# the board ``type`` for the web renderer only -- used where the same node is an
# imperative ``action`` on the board (e.g. the chained engine -> ELO picker) but a
# plain control on the web. Restricting the set turns a typo into a load-time
# error instead of a silently blank web row.
_WEB_CONTROL_TYPES = frozenset({"toggle", "select", "cycle", "range", "text"})

# Control types an ``accountTypes`` field may use. The definition-driven Add
# Account form renders each field by this type, so an unknown value would leave a
# blank, unfillable row; restricting the set turns that into a load-time error.
_ACCOUNT_FIELD_TYPES = frozenset({"text", "password"})

# How an account's unique identity is obtained. ``entered`` means it is one of
# the user-typed fields; ``resolved`` means it is derived after the fact (e.g. a
# Lichess username resolved by authenticating the token) and so is not a field.
_IDENTITY_SOURCES = frozenset({"resolved", "entered"})


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
        """Return the option list for an option set name.

        Runtime tokens in option ``description``/``help`` (e.g. ``{mdns_url}``)
        are filled here so the board help dialog and any other option_set
        consumer name this device, not a generic example host.
        """
        return fill_option_runtime_placeholders(
            list(self._menu.get("optionSets", {}).get(name, []))
        )

    # -- account types ----------------------------------------------------

    def account_types(self) -> List[dict]:
        """Return the declared online account type definitions, in order.

        Each entry describes an online player type's account: the fields the Add
        Account form collects, which field/derived value is the unique identity,
        and which fields are secrets. The board, the web UI, and the account
        store all build from this one definition. Empty when none are declared.
        """
        return list(self._menu.get("accountTypes", []))

    def has_account_type(self, type_id: str) -> bool:
        """Return whether an account type with ``type_id`` is declared."""
        return any(entry.get("id") == type_id for entry in self._menu.get("accountTypes", []))

    def account_type(self, type_id: str) -> dict:
        """Return the account type definition for ``type_id``.

        Raises:
            KeyError: if no account type has that id. Ids come from the catalog
                itself (player_type values / stored account sections), which
                :func:`load_catalog` validates, so an unknown id signals a caller
                bug rather than user data.
        """
        for entry in self._menu.get("accountTypes", []):
            if entry.get("id") == type_id:
                return entry
        raise KeyError(type_id)

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


def _validate_condition(node_id: str, gate: str, condition: dict) -> None:
    """Validate a ``visibleWhen``/``enabledWhen`` condition shape.

    A leaf condition must carry ``store`` and ``key`` (the value it reads); a
    compound ``{"allOf": [...]}`` must carry a non-empty list of leaf conditions,
    each validated recursively. This mirrors what :func:`engine._condition_met`
    can evaluate, so a shape the engine cannot read (a missing key, or an ``allOf``
    that is not a list of conditions) fails at load rather than as a row that
    never shows/hides at runtime.
    """
    if not isinstance(condition, dict):
        raise CatalogError(f"node '{node_id}' has malformed '{gate}' (not an object): {condition!r}")
    if "allOf" in condition:
        subs = condition["allOf"]
        if not isinstance(subs, list) or not subs:
            raise CatalogError(
                f"node '{node_id}' has malformed '{gate}' allOf (need a non-empty list): {subs!r}"
            )
        for sub in subs:
            _validate_condition(node_id, gate, sub)
        return
    if "store" not in condition or "key" not in condition:
        raise CatalogError(
            f"node '{node_id}' has malformed '{gate}' (need store and key): {condition!r}"
        )


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

        for gate in ("visibleWhen", "enabledWhen"):
            condition = node.get(gate)
            if condition is not None:
                _validate_condition(node_id, gate, condition)

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

    _validate_account_types(menu_data, icon_ids)

    for root_id in menu_data.get("roots", []):
        if root_id not in ids:
            raise CatalogError(f"root references unknown node '{root_id}'")

    for section in menu_data.get("sections", []):
        icon = section.get("icon")
        if icon is not None and icon not in icon_ids:
            raise CatalogError(f"section '{section.get('id')}' references unknown icon '{icon}'")


def _validate_account_types(menu_data: dict, icon_ids: "set[str]") -> None:
    """Validate the optional ``accountTypes`` block, raising :class:`CatalogError`.

    Each entry defines an online player type's account. Checks guard the exact
    ways a malformed entry would break the Add Account form or the account store:
    - ids present and unique (a duplicate would shadow a definition);
    - the id is also a ``player_type`` option value (the online-player-type link,
      so a slot can actually select this account type);
    - ``label`` present and ``icon`` registered (blank/placeholder chrome);
    - ``fields`` non-empty, each with a unique ``key``, a ``label``, and a
      supported control ``type`` (an unfillable/blank row otherwise);
    - ``identitySource`` is a known mode and, when identity is ``entered``, the
      ``identityField`` names a real field (else uniqueness has no value to key).

    The block is optional; a catalog without ``accountTypes`` is valid.
    """
    account_types = menu_data.get("accountTypes")
    if account_types is None:
        return
    if not isinstance(account_types, list):
        raise CatalogError("'accountTypes' must be a list")

    player_type_values = {
        o.get("value") for o in menu_data.get("optionSets", {}).get("player_type", [])
    }

    seen_ids: "set[str]" = set()
    for entry in account_types:
        type_id = entry.get("id")
        if not type_id:
            raise CatalogError(f"account type missing 'id': {entry!r}")
        if type_id in seen_ids:
            raise CatalogError(f"duplicate account type id: {type_id}")
        seen_ids.add(type_id)

        if type_id not in player_type_values:
            raise CatalogError(
                f"account type '{type_id}' has no matching value in the "
                f"'player_type' option set (an online player type must exist)"
            )

        if not entry.get("label"):
            raise CatalogError(f"account type '{type_id}' missing 'label'")

        icon = entry.get("icon")
        if not icon or icon not in icon_ids:
            raise CatalogError(f"account type '{type_id}' references unknown icon '{icon}'")

        fields = entry.get("fields")
        if not isinstance(fields, list) or not fields:
            raise CatalogError(f"account type '{type_id}' must declare a non-empty 'fields' list")

        field_keys: "set[str]" = set()
        for fld in fields:
            key = fld.get("key")
            if not key:
                raise CatalogError(f"account type '{type_id}' has a field missing 'key': {fld!r}")
            if key in field_keys:
                raise CatalogError(f"account type '{type_id}' has duplicate field key '{key}'")
            field_keys.add(key)
            if not fld.get("label"):
                raise CatalogError(f"account type '{type_id}' field '{key}' missing 'label'")
            field_type = fld.get("type")
            if field_type not in _ACCOUNT_FIELD_TYPES:
                raise CatalogError(
                    f"account type '{type_id}' field '{key}' has unsupported control "
                    f"type '{field_type}' (expected one of {sorted(_ACCOUNT_FIELD_TYPES)})"
                )

        identity_source = entry.get("identitySource")
        if identity_source not in _IDENTITY_SOURCES:
            raise CatalogError(
                f"account type '{type_id}' has unknown identitySource "
                f"'{identity_source}' (expected one of {sorted(_IDENTITY_SOURCES)})"
            )
        identity_field = entry.get("identityField")
        if not identity_field:
            raise CatalogError(f"account type '{type_id}' missing 'identityField'")
        if identity_source == "entered" and identity_field not in field_keys:
            raise CatalogError(
                f"account type '{type_id}' identityField '{identity_field}' is not one of "
                f"its fields (an entered identity must be a collected field)"
            )


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


def load_overlay(locale: str) -> Optional[dict]:
    """Return the parsed translation overlay for ``locale``, or None if absent.

    A missing overlay file is not an error: it means the locale has no
    translations yet, and :func:`localize_catalog` falls back to the English
    source. Only a present-but-unparseable file is surfaced (as a warning +
    None) so a broken overlay degrades to English rather than crashing the menu.
    """
    path = _TRANSLATIONS_DIR / f"{locale}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        log.warning("menu translation overlay %s is not valid JSON: %s", path, exc)
        return None


def localize_catalog(menu_data: dict, locale: str, *, overlay: Optional[dict] = None) -> dict:
    """Return a copy of ``menu_data`` with ``locale``'s translations applied.

    English (:data:`_SOURCE_LOCALE`) is the authored source, so it is returned
    unchanged (the same object) with no copy. For any other locale the overlay
    is applied over a deep copy so the shared English catalog is never mutated:

    - ``overlay["nodes"][<id>]`` may carry ``label``/``help``/``boardLabel`` and
      the user-facing ``label_in_progress``/``valueDefault``; only the present
      keys are overwritten (a node absent from the overlay, or a key it omits,
      keeps its English text).
    - ``overlay["optionSets"][<name>]`` maps an option ``value`` to its
      translated label; values not listed keep their English label.
    - ``overlay["sections"][<id>]`` and ``overlay["accountTypes"]`` translate the
      web tab labels and the account-form chrome the same way.

    A missing overlay (no file for the locale) leaves the catalog in English so
    an untranslated language degrades gracefully rather than rendering blank.

    Args:
        menu_data: The parsed English ``menu.json`` (as from
            :meth:`MenuCatalog.raw_menu`).
        locale: Target locale code (e.g. ``"es"``).
        overlay: Explicit overlay dict (tests); when None the packaged
            ``translations/<locale>.json`` is loaded.
    """
    if locale == _SOURCE_LOCALE:
        return menu_data
    if overlay is None:
        overlay = load_overlay(locale)
    if not overlay:
        log.warning("no menu translation overlay for locale %r; using English", locale)
        return menu_data

    localized = copy.deepcopy(menu_data)

    node_overlay = overlay.get("nodes", {})
    for node in localized.get("nodes", []):
        strings = node_overlay.get(node.get("id"))
        if not strings:
            continue
        # ``label_in_progress`` (the board's RESUME text) and ``valueDefault``
        # (the placeholder shown for an unset bound value, e.g. an unnamed human)
        # both render to the user, so they are translated alongside the label.
        for key in ("label", "boardLabel", "help", "label_in_progress", "valueDefault"):
            if key in strings:
                node[key] = strings[key]

    option_overlay = overlay.get("optionSets", {})
    for name, options in localized.get("optionSets", {}).items():
        value_labels = option_overlay.get(name)
        if not value_labels:
            continue
        for option in options:
            translated = value_labels.get(str(option.get("value")))
            if translated is None:
                continue
            # A plain string remains a label-only translation (every existing
            # overlay). An object may also carry ``description`` so a select can
            # show a per-mode blurb (USB gadget Off/Auto/Client/Shared) in the same
            # locale as its label.
            if isinstance(translated, str):
                option["label"] = translated
            elif isinstance(translated, dict):
                if "label" in translated:
                    option["label"] = translated["label"]
                if "description" in translated:
                    option["description"] = translated["description"]

    section_overlay = overlay.get("sections", {})
    for section in localized.get("sections", []):
        translated = section_overlay.get(section.get("id"))
        if translated is not None:
            section["label"] = translated

    account_overlay = overlay.get("accountTypes", {})
    for account in localized.get("accountTypes", []):
        strings = account_overlay.get(account.get("id"))
        if not strings:
            continue
        if "label" in strings:
            account["label"] = strings["label"]
        field_labels = strings.get("fields", {})
        for field_def in account.get("fields", []):
            field_strings = field_labels.get(field_def.get("key"))
            if not field_strings:
                continue
            for key in ("label", "help", "placeholder"):
                if key in field_strings:
                    field_def[key] = field_strings[key]

    return localized


_base_catalog: Optional[MenuCatalog] = None
_localized_catalogs: Dict[str, MenuCatalog] = {}
_active_locale: Optional[str] = None


def _base() -> MenuCatalog:
    """Return the validated English base catalog, loading it once."""
    global _base_catalog
    if _base_catalog is None:
        _base_catalog = load_catalog()
    return _base_catalog


def get_localized_catalog(locale: str) -> MenuCatalog:
    """Return the catalog localized to ``locale``, cached per locale.

    The English base is validated once; each localized view is derived from it by
    applying the overlay's string replacements (structure is untouched, so no
    re-validation is needed) and cached so repeated renders don't re-copy. Callers
    that need a specific locale regardless of the device setting (e.g. the web
    ``/api/menu-schema`` serving the requested device locale) use this directly.
    """
    base = _base()
    if locale == _SOURCE_LOCALE:
        return base
    cached = _localized_catalogs.get(locale)
    if cached is None:
        localized_menu = localize_catalog(base.raw_menu(), locale)
        cached = MenuCatalog(localized_menu, base._icons)  # noqa: SLF001 - same-module access to the base's icon data
        _localized_catalogs[locale] = cached
    return cached


def _read_active_locale() -> str:
    """Read the device UI locale from the language service, defaulting to English.

    Isolated so :func:`get_catalog` reads the setting at most once per process
    (cached in ``_active_locale``) rather than hitting the ini on every menu row,
    and so a refresh has a single source. Any failure to resolve the service
    degrades to the source locale rather than breaking menu rendering.
    """
    try:
        from universalchess.services.language_service import get_language
        return get_language()
    except Exception:  # noqa: BLE001 - locale resolution must never break the menu; fall back to English
        log.warning("could not resolve UI language; using %r", _SOURCE_LOCALE, exc_info=True)
        return _SOURCE_LOCALE


def refresh_active_language() -> str:
    """Re-read the device UI locale and return it (call after a language change).

    The board's settings hot-reload path invokes this so the next
    :func:`get_catalog` (and thus the next menu render) picks up the new locale
    without restarting the process.
    """
    global _active_locale
    _active_locale = _read_active_locale()
    return _active_locale


def get_catalog() -> MenuCatalog:
    """Return the catalog localized to the active device UI locale.

    The active locale is resolved from the language service on first use and
    cached (see :func:`_read_active_locale`); :func:`refresh_active_language`
    updates it after a change. The English base is a single validated instance
    shared across locales. Tests that need a fresh/overridden catalog should call
    :func:`load_catalog` directly rather than this cached accessor.
    """
    global _active_locale
    if _active_locale is None:
        _active_locale = _read_active_locale()
    return get_localized_catalog(_active_locale)
