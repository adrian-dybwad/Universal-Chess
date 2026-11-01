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
    handle_engine_defaults_menu,
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


class _ScriptedMenu:
    """Captures the first build_entries() paint then exits."""

    def __init__(self):
        self.built = []

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0):
        self.built.append(build_entries())
        return None


def test_engine_defaults_warns_when_hash_is_high_on_a_constrained_board():
    """Hash above 16 MB on a low-RAM board must show the memory warning row.

    Why: the constrained ceiling is 64 so users can reach Claudia's min; NNUE
    plus a large hash can thrash a ~512 MB board. How a regression manifests:
    no hash_warn row at Hash 64 when constrained, or a warning while Hash is
    still 16.
    """
    menu = _ScriptedMenu()
    handle_engine_defaults_menu(
        menu,
        log=None,
        read_status=lambda: {
            "hash": 64,
            "threads": 1,
            "move_overhead": 100,
            "hash_max_mb": 64,
            "ram_mb": 415,
            "constrained": True,
        },
        set_values=lambda _payload: True,
    )
    keys = [entry.key for entry in menu.built[0]]
    assert "hash_warn" in keys

    quiet = _ScriptedMenu()
    handle_engine_defaults_menu(
        quiet,
        log=None,
        read_status=lambda: {
            "hash": 16,
            "threads": 1,
            "move_overhead": 100,
            "hash_max_mb": 64,
            "ram_mb": 415,
            "constrained": True,
        },
        set_values=lambda _payload: True,
    )
    assert "hash_warn" not in [entry.key for entry in quiet.built[0]]
