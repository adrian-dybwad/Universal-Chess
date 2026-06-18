"""Tests for the System / Power / Reset menus, driven by the shared menu engine.

Background / why these tests exist
----------------------------------
The System subtree (Engine Manager, Analysis Engine, Sleep Timer, Reset, About,
Power) and its nested Power and Reset-confirm menus were migrated off the bespoke
``system_menu.py`` builder/dispatcher onto the data-driven engine. main.py now
supplies only the board glue: a read-only ``system`` store (analysis-mode and
sleep-timer state used for the row icon/label), the computed Sleep Timer label,
and the actions that open the still-dynamic sub-menus or perform an effect
(reset, shutdown, reboot, cancel). These tests build from the *real* catalog with
a fake context, pinning the structure, the dynamic label/icon, and the dispatch
effects the deleted module used to guarantee.
"""

from universalchess.managers.menu import MenuResult, MenuSelection
from universalchess.menus.board_context import BoardMenuContext, run_engine_menu
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows, dispatch

_EXIT_RESULTS = {MenuResult.BACK, MenuResult.SHUTDOWN, MenuResult.HELP}


class _FakeMenuManager:
    """Drives run_menu_loop from a scripted list of selection keys.

    Mirrors MenuManager.run_menu_loop (break/exit short-circuit, then handler)
    so the adapter is tested without a display, recording the entries shown on
    each iteration for assertions.
    """

    def __init__(self, script):
        self._script = list(script)
        self.shown = []

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0, track_selection=True):
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


def _system_ctx(*, analysis_mode=False, timeout_seconds=0, calls=None):
    """Board context for the System subtree, mirroring main._build_system_context.

    The read-only ``system`` store reports analysis-mode and whether the sleep
    timer is enabled; ``sleep_timer`` computes the timer label; every action is
    recorded (and returns None unless noted) so dispatch effects are observable
    without a display. ``reset_confirm`` and ``cancel`` return "BACK" exactly as
    the board does, so the confirm submenu closes after either choice.
    """
    calls = calls if calls is not None else []

    def system_get(key):
        if key == "analysis_mode":
            return analysis_mode
        if key == "sleep_enabled":
            return timeout_seconds != 0
        raise KeyError(key)

    def sleep_timer_label(node):
        return "Disabled" if timeout_seconds == 0 else f"{timeout_seconds // 60} min"

    def record(name, result=None):
        def _fn():
            calls.append(name)
            return result
        return _fn

    ctx = BoardMenuContext()
    ctx.register_store("system", system_get, lambda k, v: (_ for _ in ()).throw(NotImplementedError(k)))
    ctx.register_value("sleep_timer", sleep_timer_label)
    ctx.register_action("engine_manager", record("engine_manager"))
    ctx.register_action("analysis_mode", record("analysis_mode"))
    ctx.register_action("inactivity", record("inactivity"))
    ctx.register_action("about", record("about"))
    ctx.register_action("reset_confirm", record("reset_confirm", "BACK"))
    ctx.register_action("cancel", record("cancel", "BACK"))
    ctx.register_action("shutdown", record("shutdown"))
    ctx.register_action("reboot", record("reboot"))
    ctx._recorded_calls = calls
    return ctx


def _system_rows(**ctx_kwargs):
    return build_rows("system", _system_ctx(**ctx_kwargs), platform="board", catalog=load_catalog())


# -- structure ---------------------------------------------------------------


def test_system_menu_lists_all_rows_in_order():
    """The System menu lists its six rows in declared order.

    Why this test exists: the System container's children define the on-board
    layout; the data-driven build must reproduce the exact order the bespoke
    builder produced. How a regression manifests: a row is dropped, reordered,
    or a new child leaks in, changing this key list.
    """
    keys = [r.key for r in _system_rows()]
    assert keys == ["Engines", "AnalysisMode", "Inactivity", "ResetSettings", "About", "Power"]


def test_analysis_row_labelled_engine_not_mode():
    """The analysis row is labelled 'Analysis Engine', not 'Analysis Mode'.

    Why this test exists: 'Analysis Mode' was ambiguous against the in-game
    'Show Analysis' view toggle; the System row selects the *engine* (not safely
    changeable mid-game), so it is disambiguated to 'Analysis Engine' while the
    dispatch key stays 'AnalysisMode'. How a regression manifests: the label
    reverts to containing 'Mode', so the user again sees two confusingly-named
    'Analysis' controls.
    """
    analysis = {r.key: r for r in _system_rows()}["AnalysisMode"]
    assert analysis.label == "Analysis\nEngine"
    assert "Mode" not in analysis.label


def test_power_submenu_contains_only_shutdown_and_reboot():
    """The Power submenu holds exactly Shutdown then Reboot.

    Why this test exists: the isolated destructive power actions must remain
    reachable and ordered after the migration. How a regression manifests: a
    missing action (keys change) or a reordering flips this list.
    """
    rows = build_rows("power", _system_ctx(), platform="board", catalog=load_catalog())
    assert [r.key for r in rows] == ["Shutdown", "Reboot"]


def test_analysis_row_icon_tracks_analysis_mode():
    """The Analysis Engine row shows a checked box only when analysis is on.

    Why this test exists: the row's icon is a state map bound to
    ``system.analysis_mode`` (replacing the old per-entry icon override). How a
    regression manifests: the icon stops tracking the bound value, so the box is
    always checked/empty regardless of the setting.
    """
    by_key = {r.key: r for r in _system_rows(analysis_mode=True)}
    assert by_key["AnalysisMode"].icon == "checkbox_checked"
    by_key_off = {r.key: r for r in _system_rows(analysis_mode=False)}
    assert by_key_off["AnalysisMode"].icon == "checkbox_empty"


def test_sleep_timer_row_label_and_icon_reflect_timeout():
    """The Sleep Timer row shows the configured timeout and a matching icon.

    Why this test exists: the label is a ``{fn:sleep_timer}`` computed token and
    the icon is bound to ``system.sleep_enabled``; both must reflect the live
    timeout (Disabled + plain timer when 0, "N min" + checked timer otherwise).
    How a regression manifests: the label reverts to a static "Sleep Timer" or
    the icon stops tracking the enabled state.
    """
    disabled = {r.key: r for r in _system_rows(timeout_seconds=0)}["Inactivity"]
    assert disabled.label == "Sleep Timer\nDisabled"
    assert disabled.icon == "timer"

    enabled = {r.key: r for r in _system_rows(timeout_seconds=600)}["Inactivity"]
    assert enabled.label == "Sleep Timer\n10 min"
    assert enabled.icon == "timer_checked"


def test_reset_is_submenu_and_about_is_action_and_power_is_submenu():
    """Reset/Power open submenus; About/Engines/Analysis/Sleep are actions.

    Why this test exists: the dispatch kind per row determines what selecting it
    does -- Reset and Power recurse into nested containers while the dynamic
    sub-menus are invoked as actions. How a regression manifests: e.g. Reset
    reverts to a bare action (skipping the confirmation submenu) or Power loses
    its target.
    """
    catalog = load_catalog()
    ctx = _system_ctx()
    by_id = {r.node["id"]: r.node for r in _system_rows()}

    reset = dispatch(by_id["system.reset"], ctx)
    assert reset.kind == "submenu" and reset.target == "system.reset.confirm"
    power = dispatch(by_id["system.power"], ctx)
    assert power.kind == "submenu" and power.target == "power"
    about = dispatch(by_id["system.about"], ctx)
    assert about.kind == "action" and about.action == "about"


# -- reset confirm -----------------------------------------------------------


def test_reset_confirm_container_has_confirm_and_cancel():
    """The Reset-confirm menu offers exactly Confirm then Cancel.

    Why this test exists: this is the destructive-action gate that replaced the
    bespoke confirm dialog; both choices must be present and ordered so a user
    cannot trigger a reset without an explicit confirm. How a regression
    manifests: a missing/extra row or a swapped order changes this key list.
    """
    rows = build_rows("system.reset.confirm", _system_ctx(), platform="board", catalog=load_catalog())
    assert [r.key for r in rows] == ["confirm", "cancel"]
    assert rows[0].label == "Reset All\nSettings?"


def test_confirm_runs_reset_then_closes_and_cancel_only_closes():
    """Confirm runs the reset (and closes); Cancel closes without resetting.

    Why this test exists: the confirmation gate is the only thing standing
    between a stray press and wiping all settings, so selecting Confirm must call
    ``reset_confirm`` exactly once and selecting Cancel must call it zero times --
    both returning BACK so the submenu closes. How a regression manifests: Cancel
    triggers the reset, or Confirm fails to run it / fails to close.
    """
    calls = []
    ctx = _system_ctx(calls=calls)
    confirm_node = load_catalog().get_node("system.reset.confirm.yes")
    cancel_node = load_catalog().get_node("system.reset.confirm.no")

    cancel = dispatch(cancel_node, ctx)
    assert cancel.kind == "action" and cancel.signal == "BACK"
    assert "reset_confirm" not in calls  # cancel must not reset

    confirmed = dispatch(confirm_node, ctx)
    assert confirmed.kind == "action" and confirmed.signal == "BACK"
    assert calls.count("reset_confirm") == 1  # exactly one reset


# -- run loop integration ----------------------------------------------------


def test_selecting_reset_then_confirm_runs_reset_and_returns_to_system():
    """Driving the loop: Reset -> Confirm runs the reset and lands back in System.

    Why this test exists: this exercises the full nested dispatch the board uses
    (System submenu -> Reset submenu -> confirm action returning BACK), proving
    the confirm submenu closes and the System menu redraws rather than unwinding
    further. How a regression manifests: the BACK from the confirm action
    propagates as a break (collapsing the whole menu) or the reset never runs.
    """
    calls = []
    ctx = _system_ctx(calls=calls)
    # System: pick ResetSettings; Reset-confirm: pick confirm; System again: BACK out.
    mm = _FakeMenuManager(["ResetSettings", "confirm", "BACK"])

    result = run_engine_menu("system", ctx, mm, catalog=load_catalog())

    assert calls == ["reset_confirm"]
    assert result.result_type == MenuResult.BACK
    # The confirm list and the System menu (twice) were all rendered.
    assert any(any(e.key == "confirm" for e in shown) for shown in mm.shown)


def test_power_submenu_shutdown_dispatches_shutdown_action():
    """Selecting Power -> Shutdown invokes the shutdown action.

    Why this test exists: Power is a nested submenu whose leaves are actions; the
    board must reach ``shutdown``/``reboot`` through the engine rather than the
    deleted bespoke power handler. How a regression manifests: the Power leaves
    lose their action wiring, so selecting Shutdown does nothing.
    """
    calls = []
    ctx = _system_ctx(calls=calls)
    # System: pick Power; Power: pick Shutdown (action returns None -> redraw);
    # Power: BACK; System: BACK.
    mm = _FakeMenuManager(["Power", "Shutdown", "BACK", "BACK"])

    run_engine_menu("system", ctx, mm, catalog=load_catalog())

    assert calls == ["shutdown"]
