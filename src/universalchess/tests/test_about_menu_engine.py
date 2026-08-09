"""Tests for the data-driven About menu (the ``about`` catalog container).

Background / why these tests exist
----------------------------------
The About screen was migrated off the bespoke ``handle_about_menu`` builder onto
the shared engine: the ``about`` container declares a display-only Version row
and a dynamic telemetry list. main.py supplies a read-only ``about`` store and
the ``system_telemetry`` provider. These tests build from the *real* catalog with
a fake context, pinning the structure, the non-selectable readouts, and the
Version default the deleted builder guaranteed.

About also carried an actionable Updates row until the board's System menu took
it over, so that the board and the web agree on where updates live. What remains
is exactly what the web's system-info card shows: version and telemetry. The row
itself, and the guard against it returning here, live in
``test_updates_menu_placement``.
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import MenuRow, build_rows


def _about_ctx(*, version="1.2.3", telemetry=None):
    """Board context mirroring main._build_about_context.

    The read-only ``about`` store reports the version text; ``system_telemetry``
    yields the (non-selectable) readout rows.
    """
    telemetry = telemetry if telemetry is not None else []

    def about_get(key):
        if key == "version":
            return version
        raise KeyError(key)

    ctx = BoardMenuContext()
    ctx.register_store("about", about_get, lambda k, v: (_ for _ in ()).throw(NotImplementedError(k)))
    ctx.register_provider("system_telemetry", lambda: list(telemetry))
    return ctx


def _about_rows(**ctx_kwargs):
    return build_rows("about", _about_ctx(**ctx_kwargs), platform="board", catalog=load_catalog())


def test_about_lists_version_then_telemetry():
    """About renders Version followed by the dynamic telemetry rows, in order.

    Why this test exists: the bespoke builder appended telemetry *after* the
    fixed rows; the data-driven build must reproduce that order by expanding the
    ``system_telemetry`` provider in place of the dynamic node. How a regression
    manifests: telemetry is prepended, or Version is dropped, changing this key
    sequence.
    """
    telemetry = [
        MenuRow(key="SysCpu", label="CPU\n10%", icon="engine", selectable=False),
        MenuRow(key="SysUptime", label="Uptime\n1d", icon="timer", selectable=False),
    ]
    keys = [r.key for r in _about_rows(telemetry=telemetry)]
    assert keys == ["Version", "SysCpu", "SysUptime"]


def test_about_is_entirely_read_only():
    """No About row is focusable; the screen is a pure readout.

    Why this test exists: these rows must not be focusable, matching the old
    ``selectable=False`` info rows. Since Updates moved to the System menu,
    nothing on this screen is actionable, so a focusable row here means either a
    readout became selectable or an action row was reintroduced -- the exact
    regression that would re-split updates across two places.

    How a regression manifests: the cursor stops on a readout, implying it can be
    activated when selecting it does nothing.
    """
    rows = _about_rows(
        telemetry=[MenuRow(key="SysCpu", label="CPU\n10%", icon="engine", selectable=False)]
    )
    assert rows, "About rendered no rows at all"
    assert [r.key for r in rows if r.selectable] == []


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
