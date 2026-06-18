"""Tests for the data-driven About menu (the ``about`` catalog container).

Background / why these tests exist
----------------------------------
The About screen was migrated off the bespoke ``handle_about_menu`` builder onto
the shared engine: the ``about`` container declares a display-only Version row,
an Updates row (state-mapped icon + computed summary), and a dynamic telemetry
list. main.py supplies a read-only ``about`` store, the ``updates_status``
compute, and the ``system_telemetry`` provider. These tests build from the *real*
catalog with a fake context, pinning the structure, the non-selectable readouts,
the Version default, and the Updates icon/label the deleted builder guaranteed.
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import MenuRow, build_rows, dispatch, resolve_icon


def _about_ctx(*, version="1.2.3", update_state="manual", update_label="Manual", telemetry=None):
    """Board context mirroring main._build_about_context.

    The read-only ``about`` store reports the version text and the Updates icon
    state; ``updates_status`` supplies the Updates label; ``system_telemetry``
    yields the (non-selectable) readout rows. ``open_updates`` is recorded.
    """
    telemetry = telemetry if telemetry is not None else []

    def about_get(key):
        if key == "version":
            return version
        if key == "update_state":
            return update_state
        raise KeyError(key)

    ctx = BoardMenuContext()
    ctx.register_store("about", about_get, lambda k, v: (_ for _ in ()).throw(NotImplementedError(k)))
    ctx.register_value("updates_status", lambda node: update_label)
    ctx.register_provider("system_telemetry", lambda: list(telemetry))
    ctx.opened = []
    ctx.register_action("open_updates", lambda: ctx.opened.append("open_updates") or None)
    return ctx


def _about_rows(**ctx_kwargs):
    return build_rows("about", _about_ctx(**ctx_kwargs), platform="board", catalog=load_catalog())


def test_about_lists_version_updates_then_telemetry():
    """About renders Version, Updates, then the dynamic telemetry rows in order.

    Why this test exists: the bespoke builder appended telemetry *after*
    Version/Updates; the data-driven build must reproduce that exact order by
    expanding the ``system_telemetry`` provider in place of the dynamic node. How
    a regression manifests: telemetry is prepended, or Version/Updates are
    dropped/reordered, changing this key sequence.
    """
    telemetry = [
        MenuRow(key="SysCpu", label="CPU\n10%", icon="engine", selectable=False),
        MenuRow(key="SysUptime", label="Uptime\n1d", icon="timer", selectable=False),
    ]
    keys = [r.key for r in _about_rows(telemetry=telemetry)]
    assert keys == ["Version", "Updates", "SysCpu", "SysUptime"]


def test_version_and_telemetry_rows_are_non_selectable():
    """Version and telemetry are display-only; only Updates can be selected.

    Why this test exists: these readouts must not be focusable, matching the old
    ``selectable=False`` info rows, so the cursor lands only on the actionable
    Updates row. How a regression manifests: a readout becomes selectable (the
    cursor stops on it / it can be "clicked") or Updates loses selectability.
    """
    by_key = {r.key: r for r in _about_rows(
        telemetry=[MenuRow(key="SysCpu", label="CPU\n10%", icon="engine", selectable=False)]
    )}
    assert by_key["Version"].selectable is False
    assert by_key["SysCpu"].selectable is False
    assert by_key["Updates"].selectable is True


def test_version_row_shows_value_then_unknown_default():
    """The Version row shows the installed version, or "Unknown" when unset.

    Why this test exists: the version is a bound value with a declarative
    ``valueDefault`` (so an empty version reads "Unknown" without faking it in
    the store). How a regression manifests: the row shows a literal '{value}', a
    blank version, or the wrong default.
    """
    shown = {r.key: r for r in _about_rows(version="2.0.0")}["Version"]
    assert shown.label == "Version\n2.0.0"
    blank = {r.key: r for r in _about_rows(version="")}["Version"]
    assert blank.label == "Version\nUnknown"


def test_updates_row_icon_and_label_track_update_state():
    """The Updates row's icon (state map) and label (compute) reflect the status.

    Why this test exists: the bespoke builder chose the icon and label together
    from one update-service status (Ready!/vX -> update glyph, Auto -> checked,
    Manual -> empty); the data-driven row must keep them consistent via the
    state-mapped icon and the ``updates_status`` compute. How a regression
    manifests: the icon stops tracking the state, or the label desyncs from it.
    """
    catalog = load_catalog()
    cases = {
        "ready": ("update", "Ready!"),
        "available": ("update", "v9.9.9"),
        "auto": ("checkbox_checked", "Auto"),
        "manual": ("checkbox_empty", "Manual"),
    }
    for state, (icon, label) in cases.items():
        ctx = _about_ctx(update_state=state, update_label=label)
        node = catalog.get_node("about.updates")
        assert resolve_icon(node, ctx) == icon, state
        row = {r.key: r for r in build_rows("about", ctx, platform="board", catalog=catalog)}["Updates"]
        assert row.label == f"Updates\n{label}", state


def test_selecting_updates_opens_the_update_menu():
    """Selecting Updates invokes the open_updates action.

    Why this test exists: Updates is the one actionable About row; it must reach
    the Update menu through the registered action rather than the deleted bespoke
    dispatch. How a regression manifests: the action wiring is lost, so selecting
    Updates does nothing.
    """
    ctx = _about_ctx()
    node = load_catalog().get_node("about.updates")
    outcome = dispatch(node, ctx)
    assert outcome.kind == "action" and outcome.action == "open_updates"
    assert ctx.opened == ["open_updates"]
