"""Tests for the Sleep Timer (inactivity) menu entry composition.

The board Sleep Timer menu and the web Sleep Timer select draw their choices
from one shared catalog option set (``sleep_timer``). These tests pin that the
board menu renders exactly those choices (seconds + labels), keys them by the
seconds value, and marks the configured timeout as selected -- so the board and
web offer identical options and the saved value round-trips.
"""

from universalchess.managers.menu import MenuSelection
from universalchess.menus.catalog.loader import get_catalog
from universalchess.menus.inactivity_menu import handle_inactivity_timeout


class _Log:
    def info(self, *a, **k):
        pass


class _Board:
    def __init__(self, current):
        self._current = current
        self.set_calls = []

    def get_inactivity_timeout(self):
        return self._current

    def set_inactivity_timeout(self, seconds):
        self.set_calls.append(seconds)


class _CapturingMenuManager:
    """Captures the entries shown and returns BACK (so no value is written)."""

    def __init__(self):
        self.entries = None

    def show_menu(self, entries):
        self.entries = entries
        return MenuSelection.from_key("BACK")


def test_inactivity_options_match_catalog_sleep_timer():
    """The menu must render exactly the catalog sleep_timer options, keyed by seconds.

    Why this test exists: the board and web Sleep Timer share the catalog option
    set; if the board stopped reading it (or transformed the values) the two
    surfaces would drift. Asserts keys/labels equal the catalog, in order.

    How a regression manifests: a hardcoded list returns (keys become minutes, or
    an option is added/removed/reordered), so this list no longer matches the
    catalog option set.
    """
    board = _Board(current=900)
    mm = _CapturingMenuManager()

    handle_inactivity_timeout(board=board, log=_Log(), menu_manager=mm)

    catalog_options = get_catalog().option_set("sleep_timer")
    assert [e.key for e in mm.entries] == [o["value"] for o in catalog_options]
    assert [e.label for e in mm.entries] == [o["label"] for o in catalog_options]
    # Keys are the seconds value (so result.key round-trips into set_inactivity_timeout).
    assert [e.key for e in mm.entries] == ["0", "300", "600", "900", "1800", "3600"]


def test_inactivity_marks_current_timeout_checked():
    """Only the configured timeout gets the checked icon; the rest get 'timer'.

    Why this test exists: the selected-state icon tells the on-board user which
    timeout is active. With 900s configured, exactly the 15-min entry must show
    'timer_checked'.

    How a regression manifests: no entry (or the wrong/multiple entries) is
    checked, so the menu misrepresents the active timeout.
    """
    board = _Board(current=900)
    mm = _CapturingMenuManager()

    handle_inactivity_timeout(board=board, log=_Log(), menu_manager=mm)

    icons = {e.key: e.icon_name for e in mm.entries}
    assert icons["900"] == "timer_checked"
    assert all(icons[k] == "timer" for k in icons if k != "900")


def test_inactivity_writes_selected_seconds_on_choice():
    """Choosing an option writes its seconds value via set_inactivity_timeout.

    Why this test exists: the menu's job is to persist the chosen seconds (the
    same key the web saves); this guards that selecting an entry forwards its
    integer seconds, not minutes or a label.

    How a regression manifests: set_inactivity_timeout receives the wrong value
    (e.g. minutes), so the board sleeps after the wrong interval.
    """
    board = _Board(current=0)

    class _ChooseFiveMin:
        def show_menu(self, entries):
            # "300" == 5 minutes; the entry key is the seconds value.
            return MenuSelection(key="300")

    handle_inactivity_timeout(board=board, log=_Log(), menu_manager=_ChooseFiveMin())

    assert board.set_calls == [300]
