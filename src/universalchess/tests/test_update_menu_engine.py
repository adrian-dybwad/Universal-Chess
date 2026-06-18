"""Tests for the data-driven Updates menu (the ``updates`` catalog container).

Background / why these tests exist
----------------------------------
The Updates menu was migrated off the bespoke ``handle_update_menu`` builder onto
the shared engine: the ``updates`` container declares the auto-update toggle, the
channel select, and the check/download/install rows, with the Download and
Install-Pending rows gated by ``visibleWhen``. main.py supplies an ``update``
store over the live UpdateService plus the action handlers. These tests build
from the *real* catalog with a fake store, pinning the row structure, the
mutually exclusive Download/Install-Pending gating, the toggle's icon/label, the
channel select wiring, and that each action row dispatches to its handler --
the guarantees the deleted builder used to provide.
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows, dispatch, resolve_icon


def _update_ctx(*, auto_update=False, channel="stable", available="", has_pending=False):
    """Board context mirroring main._build_updates_context over a fake store.

    ``has_download`` is derived exactly as production does (available and not yet
    pending) so the visibility tests exercise the real gating rule. Actions are
    recorded so dispatch wiring can be asserted without running splash flows.
    """
    state = {"auto_update": auto_update, "channel": channel, "available": available,
             "has_pending": has_pending}

    def update_get(key):
        if key == "has_download":
            return (not state["has_pending"]) and bool(state["available"])
        return state[key]

    def update_set(key, value):
        state[key] = value

    ctx = BoardMenuContext()
    ctx.register_store("update", update_get, update_set)
    ctx.register_value("auto_update_state",
                       lambda node: "Enabled" if update_get("auto_update") else "Disabled")
    ctx.calls = []
    for action in ("check_updates", "download_update", "install_pending", "install_local"):
        ctx.register_action(action, (lambda a: (lambda: ctx.calls.append(a) or None))(action))
    ctx._state = state
    return ctx


def _rows(**kwargs):
    ctx = _update_ctx(**kwargs)
    return ctx, build_rows("updates", ctx, platform="board", catalog=load_catalog())


def test_up_to_date_hides_download_and_install_pending():
    """With no available and no pending update, only the steady-state rows show.

    Why this test exists: Download and Install-Pending are conditional; when the
    board is current neither should appear, matching the old builder that
    appended neither row. How a regression manifests: a broken ``visibleWhen``
    leaves a dead Download/Install row that does nothing.
    """
    _, rows = _rows(available="", has_pending=False)
    assert [r.key for r in rows] == ["AutoUpdate", "Channel", "CheckNow", "InstallLocal"]


def test_available_update_shows_download_with_version_label():
    """An available (not-yet-downloaded) update shows Download labelled with it.

    Why this test exists: the Download row is gated on ``has_download`` and its
    label binds the available version ("Download\\nv<ver>"); both must track the
    store. How a regression manifests: Download stays hidden when an update is
    available, or shows a bare "Download" without the version.
    """
    _, rows = _rows(available="2.5.0", has_pending=False)
    by_key = {r.key: r for r in rows}
    assert "DownloadUpdate" in by_key
    assert "InstallPending" not in by_key
    assert by_key["DownloadUpdate"].label == "Download\nv2.5.0"


def test_pending_update_replaces_download_with_install_pending():
    """Once an update is pending, Install-Pending shows and Download hides.

    Why this test exists: Download and Install-Pending are mutually exclusive
    (``has_download`` is false once pending); the user must see exactly one
    install path. How a regression manifests: both rows show at once, or neither,
    after a download completes.
    """
    _, rows = _rows(available="2.5.0", has_pending=True)
    keys = {r.key for r in rows}
    assert "InstallPending" in keys
    assert "DownloadUpdate" not in keys


def test_auto_update_toggle_icon_and_label_track_state():
    """The Auto-Update row's icon and label reflect the bound flag and toggling
    it flips the stored value.

    Why this test exists: the toggle must render checked/Enabled vs empty/Disabled
    consistently and actually mutate the setting, replacing the builder's manual
    icon/label/flip. How a regression manifests: the icon/label desync from the
    flag, or selecting the row no longer changes the setting.
    """
    catalog = load_catalog()
    node = catalog.get_node("updates.auto")

    on_ctx = _update_ctx(auto_update=True)
    on_row = {r.key: r for r in build_rows("updates", on_ctx, platform="board", catalog=catalog)}["AutoUpdate"]
    assert on_row.label == "Auto-Update\nEnabled"
    assert resolve_icon(node, on_ctx) == "checkbox_checked"

    off_ctx = _update_ctx(auto_update=False)
    off_row = {r.key: r for r in build_rows("updates", off_ctx, platform="board", catalog=catalog)}["AutoUpdate"]
    assert off_row.label == "Auto-Update\nDisabled"
    assert resolve_icon(node, off_ctx) == "checkbox_empty"

    dispatch(node, off_ctx)
    assert off_ctx._state["auto_update"] is True


def test_channel_row_is_a_select_over_update_channel_set():
    """The Channel row dispatches a select over the shared update_channel set,
    labelled with the active channel's catalog label.

    Why this test exists: the channel list and labels must come from the shared
    update_channel option set (one source with the web), and selecting routes
    through the engine select carrying the radio icons. How a regression
    manifests: a hardcoded channel list returns, or the select loses its store
    binding / icons.
    """
    catalog = load_catalog()
    ctx = _update_ctx(channel="nightly")
    row = {r.key: r for r in build_rows("updates", ctx, platform="board", catalog=catalog)}["Channel"]
    assert row.label == "Channel\nNightly (Development)"

    outcome = dispatch(catalog.get_node("updates.channel"), ctx)
    assert outcome.kind == "select"
    assert outcome.option_set == "update_channel"
    assert (outcome.store, outcome.key) == ("update", "channel")
    assert outcome.selected_icon == "checkbox_checked"
    assert outcome.unselected_icon == "checkbox_empty"


def test_action_rows_dispatch_to_their_handlers():
    """Each actionable row routes to its registered action handler.

    Why this test exists: the check/download/install rows are pure action nodes;
    selecting one must invoke exactly its handler (the imperative splash flow in
    production), with no cross-wiring. How a regression manifests: an action key
    typo or swap silently runs the wrong flow or nothing.
    """
    catalog = load_catalog()
    cases = {
        "updates.check": "check_updates",
        "updates.download": "download_update",
        "updates.install_pending": "install_pending",
        "updates.install_local": "install_local",
    }
    for node_id, action in cases.items():
        ctx = _update_ctx(available="1.0.0", has_pending=True)
        outcome = dispatch(catalog.get_node(node_id), ctx)
        # dispatch runs the action through the context and reports it; selecting
        # the row must invoke exactly that handler once.
        assert outcome.kind == "action" and outcome.action == action, node_id
        assert ctx.calls == [action], node_id
