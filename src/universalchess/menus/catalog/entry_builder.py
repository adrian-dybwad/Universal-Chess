"""Build e-paper IconMenuEntry lists from catalog nodes.

Bridges the shared menu catalog (data) to the board renderer (IconMenuEntry).
Static menu structure, labels, icons, and per-entry e-paper styling live in the
catalog; this module turns a catalog container's children into the entry objects
the board's IconMenuWidget renders.

Dynamic menus (player summary, time control, sleep timer, analysis toggle) keep
their runtime logic but pass per-key overrides here so the catalog still owns the
default chrome and the dynamic builder only supplies the parts that change.
"""

from typing import Dict, List, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.menus.catalog.loader import MenuCatalog, get_catalog


def node_to_entry(
    node: dict,
    *,
    label: Optional[str] = None,
    icon: Optional[str] = None,
    enabled: Optional[bool] = None,
    description: Optional[str] = None,
    trailing_icon: Optional[str] = None,
    icon_image=None,
    icon_mask=None,
) -> IconMenuEntry:
    """Convert a single catalog node into an IconMenuEntry.

    Args:
        node: Catalog node dict (must carry at least a label and icon for a
            renderable entry; ``key`` defaults to the node id).
        label: Override for the node's static label (dynamic summaries).
        icon: Override for the node's static icon (dynamic state icons).
        enabled: Override for the entry's enabled flag.
        description: Optional secondary line (e.g. the merged status button's
            Enabled/Disabled state) rendered below the icon+label.
        trailing_icon: Optional trailing icon name (e.g. a checkbox glyph paired
            with ``description`` to signal a toggle).
        icon_image: Optional pre-rendered main-icon image (provider previews).
        icon_mask: Optional transparency mask for ``icon_image``.

    Returns:
        An IconMenuEntry with e-paper style taken from the node's ``epaper``
        block, falling back to the IconMenuEntry defaults. ``epaper.icon_only``
        blanks ``label`` so the board draws a glyph; catalog ``label`` and
        ``help`` are left for the web and the HELP dialog.
    """
    style = node.get("epaper", {})
    # border_width is forwarded only when the node declares it, so entries that
    # omit it are constructed exactly as before (IconMenuEntry's own default
    # applies) rather than being pinned to a guessed value here.
    extra = {"border_width": style["border_width"]} if "border_width" in style else {}
    if "description_font_size" in style:
        extra["description_font_size"] = style["description_font_size"]
    # icon_only wins over a resolved/overridden label: _row_to_entry always
    # forwards the engine's board text, and emptying catalog ``label`` would
    # hide the name on the web. Help and key stay so HELP and dispatch still work.
    if style.get("icon_only"):
        resolved_label = ""
    else:
        resolved_label = label if label is not None else node.get("label", "")
    return IconMenuEntry(
        key=node.get("key", node["id"]),
        label=resolved_label,
        icon_name=icon if icon is not None else node.get("icon", ""),
        enabled=enabled if enabled is not None else node.get("enabled", True),
        selectable=style.get("selectable", True),
        height_ratio=style.get("height_ratio", 1.0),
        max_height=style.get("max_height", None),
        icon_size=style.get("icon_size", None),
        layout=style.get("layout", "horizontal"),
        font_size=style.get("font_size", 16),
        bold=style.get("bold", False),
        help=node.get("help"),
        description=description,
        trailing_icon_name=trailing_icon,
        icon_image=icon_image,
        icon_mask=icon_mask,
        row=style.get("row"),
        **extra,
    )


def build_menu_entries(
    container_id: str,
    *,
    overrides: Optional[Dict[str, dict]] = None,
    skip_keys: Optional[set] = None,
    catalog: Optional[MenuCatalog] = None,
) -> List[IconMenuEntry]:
    """Build the entry list for a catalog container's children, in order.

    Args:
        container_id: Catalog id of the container node (e.g. ``"settings"``).
        overrides: Optional ``{key: {"label"/"icon"/"enabled": ...}}`` map used
            by dynamic menus to substitute computed labels/icons for specific
            entries while keeping all other chrome from the catalog.
        skip_keys: Optional set of entry keys to omit entirely (e.g. the
            "Centaur" entry when the original software is unavailable).
        catalog: Catalog to read from; defaults to the shared cached catalog.

    Returns:
        Ordered list of IconMenuEntry for the container's children.
    """
    catalog = catalog or get_catalog()
    overrides = overrides or {}
    skip_keys = skip_keys or set()

    entries: List[IconMenuEntry] = []
    for child in catalog.children(container_id):
        key = child.get("key", child["id"])
        if key in skip_keys:
            continue
        override = overrides.get(key, {})
        entries.append(
            node_to_entry(
                child,
                label=override.get("label"),
                icon=override.get("icon"),
                enabled=override.get("enabled"),
            )
        )
    return entries


def help_for_key(container_id: str, key: str, catalog: Optional[MenuCatalog] = None) -> Optional[str]:
    """Return the help tip for a child entry of a container, or None.

    Used by the board's help dialog to show the focused entry's tip. Looks up the
    child of ``container_id`` whose selection key matches ``key``.
    """
    catalog = catalog or get_catalog()
    for child in catalog.children(container_id):
        if child.get("key", child["id"]) == key:
            return child.get("help")
    return None
