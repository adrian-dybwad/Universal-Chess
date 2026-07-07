"""Time Control preset options for the board and web menus.

Derives the selectable preset list from the single source of truth -- the
:data:`universalchess.state.time_control.PRESETS` registry -- so the board menu
and the web Settings page offer identical presets and cannot drift from the
controls the clock actually understands.

One builder serves both platforms, which render the identical list. Each option
is ``{value, label, description}`` where ``label`` is the short preset name (e.g.
``"5|3 Blitz"``) and ``description`` is the full rules sentence. Both platforms
render these through the shared, data-driven menu strategy rather than a
hand-formatted label:

- the board shows ``label`` on the option row and ``description`` via the help
  dialog (``IconMenuEntry.help``); and
- the web shows ``label`` in the dropdown and ``description`` beneath the
  selection.

The list is bracketed by two out-of-registry choices: a leading "Basic" entry
(``value == ""``, i.e. no preset -> resolve the legacy base minutes) and a
trailing "Custom" entry (show the custom clock builder). The preset selector is
the single master control on each platform: Basic reveals the base-minutes
control, Custom reveals the custom builder, and a named preset defines the whole
clock. Because Basic's value is "", the board relies on empty-string selections
being valid (see ``icon_menu``/``MenuManager.show_menu``).
"""

from typing import Dict, List

from universalchess.state.time_control import CUSTOM_PRESET_KEY, list_presets

# Description shown for the two out-of-registry choices. Kept here (not in the
# web layer) so the single source of truth for preset text stays in Python.
_BASIC_DESCRIPTION = (
    "No preset. Use the Base Minutes control to set a simple sudden-death clock "
    "(the same time for both sides, no increment or delay)."
)
_CUSTOM_DESCRIPTION = (
    "Build your own clock: base time, Fischer increment, delay (simple or "
    "Bronstein), and optionally different times for each side (time odds)."
)


def preset_options() -> List[Dict[str, str]]:
    """Return ``{value, label, description}`` options for the preset selector.

    A leading Basic entry (``value == ""``), then one entry per registered preset
    (in registry order), then a trailing Custom entry. ``value`` is what persists
    to ``game.time_control_preset``; ``label`` is the short name; ``description``
    is the full rules sentence for the platform to surface (board help dialog /
    web description block).

    Both platforms render this identical list -- the preset selector is the
    single master control on each -- so the board and web cannot drift.

    Returns:
        Ordered list of ``{"value", "label", "description"}`` dicts.
    """
    options: List[Dict[str, str]] = [
        {"value": "", "label": "Basic", "description": _BASIC_DESCRIPTION}
    ]
    for preset in list_presets():
        options.append(
            {"value": preset.key, "label": preset.label, "description": preset.description}
        )
    options.append(
        {"value": CUSTOM_PRESET_KEY, "label": "Custom", "description": _CUSTOM_DESCRIPTION}
    )
    return options
