"""Board engine-list row for app-wide Hash and Threads.

Why these tests exist
---------------------
Shared engine defaults sit on the engine list so the board matches the web
Engines tab. The row must not use an engine name as its key.

How a regression manifests
--------------------------
The list has no Engine defaults row, or the row key is an engine id.
"""

from universalchess.menus.engine_manager_menu import (
    ENGINE_DEFAULTS_MENU_KEY,
    engine_defaults_list_entry,
)


def test_engine_defaults_row_key_is_not_an_engine_name():
    """The list row must open the defaults screen, not an engine named hash.

    Why: handle_engine_manager_menu dispatches on result.key. How a regression
    manifests: the key is an engine lookup, or the label omits Hash.
    """
    entry = engine_defaults_list_entry({"hash": 16, "threads": 1})
    assert entry.key == ENGINE_DEFAULTS_MENU_KEY
    assert "Engine defaults" in entry.label
    assert "16" in entry.label
    assert "1" in entry.label
    assert entry.selectable is True
