"""Main menu helper.

Entry chrome (labels, icons, e-paper styling) lives in the shared menu catalog
under the ``main`` container; this builder reads it and applies the two runtime
variations the catalog cannot express on its own: the PLAY/RESUME relabel and
hiding the Centaur entry when the original software is unavailable.
"""

from typing import List

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.menus.catalog import get_catalog
from universalchess.menus.catalog.entry_builder import build_menu_entries


def create_main_menu_entries(
    centaur_available: bool = True,
    game_in_progress: bool = False,
) -> List[IconMenuEntry]:
    """Create the standard main menu entry configuration.

    Args:
        centaur_available: Whether to include the "Original Centaur" entry.
        game_in_progress: When True the top entry reads "RESUME" because PLAY
            will resume the suspended game; otherwise it reads "PLAY" and starts
            a new one. The entry key is unchanged so the main loop's routing is
            independent of the label.
    """
    catalog = get_catalog()
    play_node = catalog.get_node("main.play")
    play_label = play_node["label_in_progress"] if game_in_progress else play_node["label"]

    skip_keys = set() if centaur_available else {"Centaur"}
    return build_menu_entries(
        "main",
        overrides={"Universal": {"label": play_label}},
        skip_keys=skip_keys,
        catalog=catalog,
    )

