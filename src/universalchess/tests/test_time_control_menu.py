"""Tests for the Time Control menu, now driven by the shared menu engine.

Background / why these tests exist
----------------------------------
Time Control was migrated onto the data-driven engine: ``settings.timecontrol``
is a ``select`` node bound to ``game.time_control`` over the shared
``time_control`` option set, opened via ``run_node`` from the (still
string-dispatched) board Settings menu. These tests drive that node through the
adapter with a fake menu manager and dict-backed game store, pinning the
catalog-sourced options/labels, the timer checked/unchecked icons, and that a
selection persists the chosen minutes.
"""

from universalchess.managers.menu import MenuResult, MenuSelection
from universalchess.menus.board_context import BoardMenuContext, run_node
from universalchess.menus.catalog.loader import load_catalog

_EXIT_RESULTS = {MenuResult.BACK, MenuResult.SHUTDOWN, MenuResult.HELP}


class _FakeMenuManager:
    """Drives run_menu_loop from a scripted list of selection keys.

    Mirrors MenuManager.run_menu_loop (break/exit short-circuit, then handler)
    and records the entries shown so the option list can be asserted.
    """

    def __init__(self, script):
        self._script = list(script)
        self.shown = []

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0, track_selection=True, on_index_change=None):
        while True:
            self.shown.append(build_entries())
            selection = MenuSelection.from_key(self._script.pop(0))
            if selection.key == "REFRESH":
                continue
            if selection.is_break or selection.result_type in _EXIT_RESULTS:
                return selection
            result = handle_selection(selection)
            if result is not None:
                return result


def _ctx(time_control):
    state = {"time_control": time_control}
    ctx = BoardMenuContext()
    ctx.register_store("game", lambda k: state[k], lambda k, v: state.__setitem__(k, v))
    return ctx, state


def _time_node():
    return load_catalog().get_node("settings.timecontrol")


def test_options_and_labels_come_from_catalog_time_control_set():
    """The opened list must mirror the catalog time_control set exactly.

    How a regression manifests: if the node lost its optionSet/bind or reverted
    to a hardcoded list, the keys (minutes-as-string) and labels diverge from the
    catalog ("0"/"Untimed", "5"/"5 min (Blitz)"). The key is what dispatch
    persists, so a key drift is also a save bug.
    """
    ctx, _ = _ctx(0)
    mm = _FakeMenuManager(["BACK"])  # open then leave without choosing

    run_node(_time_node(), ctx, mm, catalog=load_catalog())

    options = load_catalog().option_set("time_control")
    shown = mm.shown[0]
    assert [e.key for e in shown] == [str(o["value"]) for o in options]
    assert [e.label for e in shown] == [o["label"] for o in options]


def test_current_time_marked_with_timer_checked_icon():
    """The configured time shows timer_checked; the rest show timer.

    How a regression manifests: a broken current-value comparison (int 5 vs
    str "5" after the catalog migration) leaves no row checked, so the active
    selection is invisible. Custom select icons (selectedIcon/unselectedIcon)
    must also be honored rather than defaulting to radio glyphs.
    """
    ctx, _ = _ctx(5)
    mm = _FakeMenuManager(["BACK"])

    run_node(_time_node(), ctx, mm, catalog=load_catalog())

    by_key = {e.key: e for e in mm.shown[0]}
    assert by_key["5"].icon_name == "timer_checked"
    assert by_key["0"].icon_name == "timer"


def test_selecting_a_time_persists_minutes():
    """Choosing a row writes the selected minutes to the game store.

    How a regression manifests: if dispatch/select stopped writing the chosen
    value, time_control would not update. The value persists as the option's
    string value ("10"), matching the settings store's existing contract.
    """
    ctx, state = _ctx(0)
    mm = _FakeMenuManager(["10"])  # pick 10 minutes

    run_node(_time_node(), ctx, mm, catalog=load_catalog())

    assert state["time_control"] == "10"
