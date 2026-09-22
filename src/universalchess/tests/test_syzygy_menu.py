"""Board engine-list row for the shared Syzygy folder.

Why these tests exist
---------------------
Tablebases sit on the engine list so the board matches the web Engines tab.
The row must not use an engine name as its key (an engine called syzygy would
open the wrong screen) and must show presence, not the enable toggle.

How a regression manifests
--------------------------
The list has no Tablebases row, or the row key is an engine id so selecting
it opens an engine detail screen.
"""

from universalchess.menus.engine_manager_menu import (
    SYZYGY_MENU_KEY,
    tablebases_list_entry,
)


def test_tablebases_row_key_is_not_an_engine_name():
    """The list row must open the tablebase screen, not an engine named syzygy.

    Why: handle_engine_manager_menu dispatches on result.key. Using an engine
    id would collide the day someone installs a custom engine with that name.
    How a regression manifests: the key is ``syzygy`` as an engine lookup, or
    the label does not contain Tablebases.
    """
    entry = tablebases_list_entry(
        {
            "ready": False,
            "present": 0,
            "expected": 145,
            "enabled": False,
        }
    )
    assert entry.key == SYZYGY_MENU_KEY
    assert "Tablebases" in entry.label
    assert "Not installed" in entry.label
    assert entry.selectable is True


def test_tablebases_row_shows_partial_counts():
    """A half-downloaded set must not read as installed.

    Why: the list is what a user scans; calling a 10/145 folder Installed would
    send them into play thinking probing works. How a regression manifests:
    the subtitle is ``Installed 3-5 piece`` while present < expected.
    """
    entry = tablebases_list_entry(
        {"ready": False, "present": 10, "expected": 145, "enabled": True}
    )
    assert "10/145" in entry.label
    assert "Installed 3-5 piece" not in entry.label
